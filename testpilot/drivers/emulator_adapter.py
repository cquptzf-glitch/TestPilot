"""设备模拟器适配 — WebSocket 带外控制通道（设计 8.5 / 附录 A.3）。

定案：模拟器为自研配套件（Qt+QWebView），主通道 = 带外接口直控模拟核心，
绕过 WebView 界面层（QWebView 内 DOM 元素不映射 UIA 树，界面级控制不可靠）；
UIA 仅窗口级兜底；MCP 不进主链路（M5+ 集成 agent 时可将本接口包成 MCP server）。

协议：一请求一回执（``{id, cmd, args, ver}`` -> ``{id, ok, data|err, elapsed_ms}``），
``hello`` 握手取能力清单（指令表/场景/术语），供 LLM 注入白名单使用（设计 11.2）。
"""
import json
import logging
import time

from testpilot.core.contracts import DriverManifest
from testpilot.core.errors import AbortRequested, DeviceError

log = logging.getLogger("testpilot.emulator")

# 模拟器是"设备类"驱动：不走交互驱动路由（Router 只处理 interaction），
# 由 Executor 的 device.* 步骤直接调用；manifest 供注册表与一致性门禁使用。
MANIFEST = DriverManifest(
    kind="device",
    name="emulator",
    version="1.1.0",
    capabilities=frozenset({"call", "load_scene", "query_state", "history"}),
    config_schema={"type": "object", "properties": {
        "endpoint": {"type": "string", "default": "ws://127.0.0.1:9300"},
        "connect_timeout_s": {"type": "number", "default": 5},
        "call_timeout_s": {"type": "number", "default": 10},
        "protocol_ver": {"type": "string", "default": "1.0"},
    }},
    entry="testpilot.drivers.emulator_adapter:create",
    priority=100,
)


class EmulatorAdapter:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.endpoint = self.cfg.get("endpoint", "ws://127.0.0.1:9300")
        self.connect_timeout = float(self.cfg.get("connect_timeout_s", 5))
        self.call_timeout = float(self.cfg.get("call_timeout_s", 10))
        self.ver = self.cfg.get("protocol_ver", "1.0")
        self._ws = None
        self._caps: dict | None = None   # hello 回执的能力清单缓存（A.3.2）

    # ---------- 连接与握手 ----------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _ensure_connected(self, timeout_s=None, abort=None) -> None:
        if self._ws is not None:
            return
        import websocket
        budget = self.connect_timeout if timeout_s is None else min(self.connect_timeout, timeout_s)
        deadline = time.monotonic() + budget
        if abort is not None:
            abort.check()
        try:
            self._ws = websocket.create_connection(self.endpoint, timeout=budget)
        except Exception as e:
            raise DeviceError(f"模拟器连接失败: {self.endpoint} {e}") from e
        try:
            # hello 往返也吃 connect_timeout：try_capabilities 的短超时只压
            # TCP connect 的话，"端口通但不回 hello"的端点仍会把解析卡满 call_timeout
            resp = self._roundtrip("hello",
                                   {"client": "testpilot", "ver": self.ver},
                                   timeout_s=max(0.001, deadline - time.monotonic()),
                                   abort=abort)
            self._caps = resp.get("data") or {}
            log.info(f"模拟器已连接: {self.endpoint} "
                     f"protocol={self._caps.get('protocol')}")
        except DeviceError:
            self.close()          # 握手失败 -> 连接作废
            raise

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # 关闭异常无需上报
                pass
            finally:
                self._ws = None

    @property
    def capabilities(self) -> dict:
        """能力清单（指令表/场景/术语）— LLM 注入与 DSL 白名单校验共用（11.2）。"""
        if self._caps is None:
            self._ensure_connected()
        return self._caps or {}

    def try_capabilities(self, timeout_s: float = 1.0) -> dict:
        """尽力取能力清单：未连接时用较短超时尝试一次，失败返回 ``{}`` 不抛错。

        用例解析（``case parse``）需要能力白名单，但模拟器没起时不应让解析卡 5 秒。
        """
        if self._caps is not None:
            return self._caps
        original = self.connect_timeout
        self.connect_timeout = min(original, float(timeout_s))
        try:
            return self.capabilities
        except DeviceError:
            return {}
        finally:
            self.connect_timeout = original

    def protocol_version(self) -> str:
        """协议版本（env_snapshot 归因可比性字段，M2-5）。"""
        return str(self.capabilities.get("protocol", self.ver))

    # ---------- 指令 ----------

    def _roundtrip(self, cmd: str, args: dict,
                   timeout_s: float | None = None, abort=None) -> dict:
        """一请求一回执（信封见 A.3.1）：发送 -> 阻塞 recv -> 校验 id。"""
        if self._ws is None:
            raise DeviceError("尚未连接模拟器")
        rid = _next_id()
        payload = json.dumps({"id": rid, "cmd": cmd, "args": args, "ver": self.ver})
        t0 = time.monotonic()
        try:
            self._ws.settimeout(timeout_s if timeout_s is not None
                                else self.call_timeout)
            self._ws.send(payload)
            if abort is None:
                resp = json.loads(self._ws.recv())
            else:
                import websocket
                deadline = t0 + (timeout_s if timeout_s is not None else self.call_timeout)
                while True:
                    abort.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("deadline exceeded")
                    self._ws.settimeout(min(0.1, remaining))
                    try:
                        resp = json.loads(self._ws.recv())
                        break
                    except (websocket.WebSocketTimeoutException, TimeoutError):
                        continue
        except AbortRequested:
            self.close()
            raise
        except Exception as e:
            self.close()          # 连接不可信，下次重连
            raise DeviceError(f"模拟器指令失败/超时: cmd={cmd} err={e}") from e
        if not isinstance(resp, dict) or resp.get("id") != rid:
            self.close()
            raise DeviceError(f"回执 id 不匹配: cmd={cmd} resp={resp!r}")
        ms = int((time.monotonic() - t0) * 1000)
        resp.setdefault("elapsed_ms", ms)
        if not resp.get("ok"):
            err = resp.get("err") or {}
            raise DeviceError(
                f"模拟器返回错误: cmd={cmd} code={err.get('code')} "
                f"msg={err.get('msg')}")
        log.info("模拟器指令完成", extra={"cmd": cmd, "locate_ms": ms})
        return resp

    def call(self, cmd: str, args: dict, timeout_s: float | None = None) -> dict:
        self._ensure_connected()
        return self._roundtrip(cmd, args, timeout_s)

    def load_scene(self, scene_id: str) -> None:
        self.call("load_scene", {"scene_id": scene_id})

    def query_state(self, path: str) -> object:
        return self.call("query_state", {"path": path}).get("data")

    def query_state_bounded(self, path: str, *, timeout_s: float, abort=None) -> object:
        """Use one budget for connect, hello and receipt; cancellation checks every 100ms."""
        deadline = time.monotonic() + timeout_s
        self._ensure_connected(timeout_s, abort)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise DeviceError("状态查询超时（连接/握手已耗尽预算）")
        return self._roundtrip("query_state", {"path": path}, remaining, abort).get("data")

    def history(self, limit: int = 100) -> list:
        # `get("data", [])` 在键存在且值为 null 时返回 None，调用方迭代即 TypeError
        return self.call("history", {"limit": limit}).get("data") or []


_REQ_ID = 0


def _next_id() -> int:
    global _REQ_ID
    _REQ_ID += 1
    return _REQ_ID


def create(cfg: dict) -> EmulatorAdapter:
    return EmulatorAdapter(cfg)

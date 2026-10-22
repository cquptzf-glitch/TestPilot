"""TestPilot Explore MCP server.

协议：JSON-RPC 2.0 over stdio，覆盖 initialize / initialized / ping /
tools/list / tools/call，并增加 notifications/turn-ended 作为会话边界。

和旧实现相比：
- 默认真正装配可用的 UIA / Vision 驱动，而不再只暴露模拟器；
- screen.grab / ui.observe 的截图以 MCP image block 返回，不把大段 base64 塞进文本；
- turn-ended / EOF 会清理会话 element_index，避免跨回合复用旧 UI 句柄。
"""
import json
import logging
import sys

from testpilot.core.errors import TestPilotError

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "testpilot-emulator"   # 保持历史兼容；能力已扩展为完整 automation toolbox
SERVER_VERSION = "0.2.0"

log = logging.getLogger("testpilot.explore.mcp")


class McpServer:
    """MCP 基础子集实现（与 ToolBox 解耦，单测可喂内存流）。"""

    def __init__(self, toolbox, *, name: str = SERVER_NAME,
                 version: str = SERVER_VERSION):
        self.toolbox = toolbox
        self.name = name
        self.version = version

    def handle(self, message: dict):
        """处理一条 JSON-RPC 消息；通知类返回 None。"""
        method = message.get("method")
        msg_id = message.get("id")

        if method == "initialize":
            return _ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method in ("notifications/turn-ended", "turn-ended"):
            reset = getattr(self.toolbox, "reset_session", None)
            if callable(reset):
                reset()
            return None
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": self.toolbox.list_tools()})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            try:
                result = self.toolbox.invoke(name, params.get("arguments") or {})
            except TestPilotError as e:
                return _ok(msg_id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True})
            except Exception as e:
                # 常驻 server 的最后防线：单次非法调用不能击穿整个 agent 会话。
                return _ok(msg_id, {
                    "content": [{"type": "text",
                                 "text": f"{type(e).__name__}: {e}"}],
                    "isError": True})
            return _ok(msg_id, {
                "content": _content_blocks(result),
                "isError": False})
        if msg_id is None:
            return None
        return _err(msg_id, -32601, f"method not found: {method}")

    def serve_stdio(self, reader, writer) -> int:
        """逐行读取 JSON-RPC 消息直到 EOF；退出时清理短生命周期 UI 状态。"""
        try:
            for line in reader:
                text = line.strip() if isinstance(line, str) else line.decode(
                    "utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    writer.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
                    writer.flush()
                    continue
                if not isinstance(message, dict):
                    writer.write(json.dumps(_err(
                        None, -32600, "请求必须是对象"), ensure_ascii=False) + "\n")
                    writer.flush()
                    continue
                response = self.handle(message)
                if response is not None:
                    writer.write(json.dumps(response, ensure_ascii=False) + "\n")
                    writer.flush()
            return 0
        finally:
            reset = getattr(self.toolbox, "reset_session", None)
            if callable(reset):
                reset()


def build_server(cfg: dict | None = None) -> McpServer:
    """按运行环境装配 Explore 自动化工具箱。

    UI 驱动按“能创建且 health 通过”加入；缺 pywinauto/cv2 时只跳过对应驱动，
    不影响模拟器 MCP 能力。这样 tools/list 与当前机器真实能力一致。
    """
    from testpilot.adapters.llm_gateway import LlmGateway
    from testpilot.app.cli import build_registry
    from testpilot.config.settings import load_settings
    from testpilot.drivers.emulator_adapter import EmulatorAdapter
    from testpilot.drivers.vision_driver import VisionDriver
    from testpilot.engine.verifier import Verifier
    from testpilot.explore.toolbox import ToolBox

    cfg = cfg or load_settings()
    registry = build_registry()

    llm = LlmGateway({
        **cfg.get("llm", {}),
        "vision": cfg.get("vision", {}).get("vlm", {}),
    })
    if not registry.has_factory("vision"):
        registry.bind_factory("vision", lambda dcfg: VisionDriver(dcfg, llm))

    drivers = {}
    for name in registry.names(kind="interaction"):
        try:
            driver = registry.create(name, cfg.get(name, {}))
            health = driver.health()
            if not health.ok:
                log.warning("Explore MCP 跳过不可用驱动 %s: %s",
                            name, health.detail)
                continue
            drivers[name] = driver
        except Exception as e:
            log.warning("Explore MCP 驱动装配跳过 %s: %s", name, e)

    try:
        emulator = EmulatorAdapter(cfg["emulator"])
    except Exception as e:
        log.warning("Explore MCP 模拟器装配失败（UI 能力仍可用）: %s", e)
        emulator = None

    verifier = Verifier(drivers, emulator, llm, cfg.get("driver", {}))
    return McpServer(ToolBox(drivers, emulator=emulator, verifier=verifier))


def _content_blocks(result) -> list[dict]:
    """把工具结果转为 MCP content；图片与结构化文本分离。"""
    if not isinstance(result, dict):
        return [{"type": "text",
                 "text": json.dumps(result, ensure_ascii=False, default=str)}]

    payload = dict(result)
    png = payload.pop("png_base64", None)
    blocks = [{
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False, default=str),
    }]
    if png:
        blocks.append({
            "type": "image",
            "data": str(png),
            "mimeType": "image/png",
        })
    return blocks


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def main(argv=None) -> int:
    """stdio 入口。"""
    from testpilot.config.settings import load_settings
    from testpilot.infra.logsys import setup_logging

    cfg = load_settings()
    # stdout 是 MCP 协议通道；结构化日志只能走 stderr。
    setup_logging(cfg.get("log", {}))
    server = build_server(cfg)
    return server.serve_stdio(sys.stdin, sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""模拟器适配单测 — hello 握手 / 一请求一回执 / 错误语义 / 断线重连（设计 8.5、附录 A.3）。

用假 ``websocket`` 模块覆盖协议层：信封字段（id/cmd/args/ver）、回执 id 校验、
``ok=false`` 错误码、超时/断连后的连接作废与下次重连、能力清单缓存。
"""
import json
import sys
import types

import pytest

from testpilot.core.errors import DeviceError


class FakeWS:
    """假 WebSocket：按脚本应答，并记录发出的报文与超时设置。

    ``script`` 是**跨连接共享的应答队列**（按发送顺序消费）——重连后继续用剩下的
    应答，这正是"断线->重连->重握手->继续指令"这条链路要验证的语义。
    """

    def __init__(self, script=None):
        self.script = script if isinstance(script, list) else list(script or [])
        self.sent: list[str] = []
        self.timeouts: list = []
        self.closed = 0

    # --- websocket-client 的最小 API ---
    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        if not self.script:
            raise AssertionError("未预设回执")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        req_id = json.loads(self.sent[-1])["id"]
        if callable(item):
            return json.dumps(item(req_id))
        if item == "echo_id":
            return json.dumps({"id": req_id, "ok": True, "data": {}})
        return json.dumps(item)

    def close(self):
        self.closed += 1


class FakeWebsocket:
    """假 ``websocket`` 模块：所有连接共享同一应答队列。

    传入的 ``scripts`` 允许再包一层列表（用例里写成 ``[[hello], [receipt]]`` 更直观）；
    这里统一展平成一条应答队列，跨连接继续消费 —— 正好覆盖"断线重连"链路。
    """

    def __init__(self, scripts=None, connect_error=None):
        self.queue = _flatten(scripts or [])
        self.connect_error = connect_error
        self.connections: list[FakeWS] = []
        self.urls: list[tuple] = []

    def create_connection(self, url, timeout=None):
        self.urls.append((url, timeout))
        if isinstance(self.connect_error, list):
            err = self.connect_error.pop(0) if len(self.connect_error) > 1 \
                else self.connect_error[0]
            if err is not None:
                raise err
        elif self.connect_error is not None:
            raise self.connect_error
        ws = FakeWS(self.queue)
        self.connections.append(ws)
        return ws


def _flatten(items: list) -> list:
    out = []
    for item in items:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return out


CAPS = {"protocol": "1.0", "commands": ["load_scene", "query_state", "set_pressure"],
        "scenes": ["normal_sample_a"], "terms": {"压力": "pressure"}}


def _hello(req_id):
    return {"id": req_id, "ok": True, "data": CAPS, "elapsed_ms": 1}


@pytest.fixture()
def mk(monkeypatch):
    """构造 EmulatorAdapter 并注入假 websocket 模块。"""
    from testpilot.drivers.emulator_adapter import EmulatorAdapter

    def _make(scripts=None, connect_error=None, cfg=None):
        fake = FakeWebsocket(scripts, connect_error)
        module = types.ModuleType("websocket")
        module.create_connection = fake.create_connection
        monkeypatch.setitem(sys.modules, "websocket", module)
        adapter = EmulatorAdapter(cfg or {"endpoint": "ws://127.0.0.1:9300",
                                          "connect_timeout_s": 1,
                                          "call_timeout_s": 2,
                                          "protocol_ver": "1.0"})
        return adapter, fake

    return _make


# ---------------------------------------------------------------- 握手 / 能力

def test_handshake_populates_capabilities_and_protocol(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)]])
    assert adapter.connected is False
    assert adapter.capabilities == CAPS
    assert adapter.connected is True
    assert adapter.protocol_version() == "1.0"
    assert fake.urls == [("ws://127.0.0.1:9300", 1)]

    hello = json.loads(fake.connections[0].sent[0])
    assert hello["cmd"] == "hello"
    assert hello["args"] == {"client": "testpilot", "ver": "1.0"}
    assert hello["ver"] == "1.0"


def test_handshake_failure_invalidates_connection(mk):
    adapter, fake = mk([[lambda rid: {"id": rid, "ok": False,
                                     "err": {"code": "E_HELLO",
                                             "msg": "版本不兼容"}}]])
    with pytest.raises(DeviceError) as e:
        _ = adapter.capabilities      # 读能力即触发握手，握手失败必须抛
    assert "版本不兼容" in str(e.value)
    assert adapter.connected is False
    assert fake.connections[0].closed == 1


def test_connect_error_is_device_error(mk):
    adapter, _ = mk(connect_error=OSError("connection refused"))
    with pytest.raises(DeviceError) as e:
        adapter.call("query_state", {"path": "/"})
    assert "模拟器连接失败" in str(e.value)


def test_protocol_version_falls_back_to_config(mk):
    adapter, _ = mk([[lambda rid: {"id": rid, "ok": True, "data": {}}]])
    assert adapter.protocol_version() == "1.0"


# ---------------------------------------------------------------- 指令回执

def test_call_returns_receipt_and_records_elapsed(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": {"run": "idle"}}]])
    resp = adapter.call("query_state", {"path": "/state/run"})
    assert resp["ok"] is True
    assert resp["data"] == {"run": "idle"}
    assert isinstance(resp["elapsed_ms"], int)

    req = json.loads(fake.connections[0].sent[-1])
    assert req["cmd"] == "query_state"
    assert req["args"] == {"path": "/state/run"}
    assert fake.connections[0].timeouts[-1] == 2          # call_timeout_s


def test_call_with_custom_timeout(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": {}}]])
    adapter.call("load_scene", {"scene_id": "a"}, timeout_s=0.5)
    assert fake.connections[0].timeouts[-1] == 0.5


def test_reply_id_mismatch_invalidates_connection(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        {"id": 999999, "ok": True, "data": {}}])
    with pytest.raises(DeviceError) as e:
        adapter.call("query_state", {"path": "/"})
    assert "回执 id 不匹配" in str(e.value)
    assert adapter.connected is False


def test_error_receipt_surfaces_code_and_message(mk):
    adapter, _ = mk([[lambda rid: _hello(rid)],
                     [lambda rid: {"id": rid, "ok": False,
                                   "err": {"code": "E_ARG", "msg": "场景不存在"}}]])
    with pytest.raises(DeviceError) as e:
        adapter.load_scene("nope")
    assert "E_ARG" in str(e.value) and "场景不存在" in str(e.value)
    assert adapter.connected is True          # 业务错误不销毁连接


def test_send_exception_invalidates_connection(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)], [TimeoutError("recv timeout")]])
    with pytest.raises(DeviceError) as e:
        adapter.call("query_state", {"path": "/"})
    assert "指令失败/超时" in str(e.value)
    assert adapter.connected is False
    assert fake.connections[0].closed == 1


def test_reconnects_after_previous_failure(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [TimeoutError("boom")],
                        [lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": "ok"}]])
    with pytest.raises(DeviceError):
        adapter.call("query_state", {"path": "/"})
    assert adapter.call("query_state", {"path": "/"})["data"] == "ok"
    assert len(fake.connections) == 2          # 第二次调用重新握手
    assert adapter.capabilities == CAPS


def test_connect_is_lazy_and_cached(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": 1}],
                        [lambda rid: {"id": rid, "ok": True, "data": 2}]])
    adapter.call("query_state", {"path": "/a"})
    adapter.call("query_state", {"path": "/b"})
    assert len(fake.connections) == 1          # 复用同一连接，不再握手


# ---------------------------------------------------------------- 便捷方法

def test_query_state_returns_data(mk):
    adapter, _ = mk([[lambda rid: _hello(rid)],
                     [lambda rid: {"id": rid, "ok": True,
                                   "data": {"state": {"run": "completed"}}}]])
    assert adapter.query_state("/state/run") == {"state": {"run": "completed"}}


def test_query_state_returns_none_without_data(mk):
    adapter, _ = mk([[lambda rid: _hello(rid)],
                     [lambda rid: {"id": rid, "ok": True}]])
    assert adapter.query_state("/x") is None


def test_history_returns_list_and_defaults_empty(mk):
    adapter, _ = mk([[lambda rid: _hello(rid)],
                     [lambda rid: {"id": rid, "ok": True, "data": [{"cmd": "a"}]}],
                     [lambda rid: {"id": rid, "ok": True}]])
    assert adapter.history(5) == [{"cmd": "a"}]
    assert adapter.history() == []


def test_load_scene_sends_scene_id(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": {}}]])
    adapter.load_scene("normal_sample_a")
    req = json.loads(fake.connections[0].sent[-1])
    assert req == {"id": req["id"], "cmd": "load_scene",
                   "args": {"scene_id": "normal_sample_a"}, "ver": "1.0"}


# ---------------------------------------------------------------- try_capabilities

def test_try_capabilities_returns_empty_on_failure(mk):
    adapter, _ = mk(connect_error=OSError("down"))
    assert adapter.try_capabilities() == {}
    assert adapter.connected is False


def test_try_capabilities_returns_caps_and_restores_timeout(mk):
    adapter, _ = mk([[lambda rid: _hello(rid)]])
    assert adapter.try_capabilities(timeout_s=0.2) == CAPS
    assert adapter.connect_timeout == 1        # 临时超时已还原
    assert adapter.try_capabilities() == CAPS  # 已缓存，不再连接


# ---------------------------------------------------------------- 生命周期

def test_close_is_idempotent(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)]])
    _ = adapter.capabilities                   # 先建立连接
    adapter.close()
    adapter.close()
    assert adapter.connected is False
    assert fake.connections[0].closed == 1


def test_close_without_connection_is_noop(mk):
    adapter, _ = mk()
    adapter.close()                            # 不抛异常


def test_create_factory(mk):
    from testpilot.drivers.emulator_adapter import create
    assert create({"endpoint": "ws://x:1"}).endpoint == "ws://x:1"


def test_request_ids_increase(mk):
    adapter, fake = mk([[lambda rid: _hello(rid)],
                        [lambda rid: {"id": rid, "ok": True, "data": 1}],
                        [lambda rid: {"id": rid, "ok": True, "data": 2}]])
    adapter.call("a", {})
    adapter.call("b", {})
    ids = [json.loads(p)["id"] for p in fake.connections[0].sent]
    assert ids == sorted(ids) and len(set(ids)) == 3


def test_manifest_is_device_kind():
    from testpilot.drivers.emulator_adapter import MANIFEST
    assert MANIFEST.kind == "device"
    assert MANIFEST.name == "emulator"
    assert {"call", "load_scene", "query_state", "history"} <= set(
        MANIFEST.capabilities)

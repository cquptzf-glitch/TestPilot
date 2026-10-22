"""探索域单测（M5+ piagent）— 工具层、会话固化回 DSL、MCP server（设计 11.1）。

主链路确定性不受影响：本组测试都不启动真实 GUI / 网络。
"""
import io
import json

import pytest

from testpilot.core.dsl import TestCase as DslCase
from testpilot.core.errors import LocateError
from testpilot.explore.mcp_server import McpServer
from testpilot.explore.session import ExplorationSession
from testpilot.explore.toolbox import ToolBox, ToolError
from tests.conftest import FakeDriver, FakeEmulator, FakeVisionDriver


@pytest.fixture()
def toolbox():
    return ToolBox({"uia": FakeDriver("uia"), "vision": FakeDriver("vision")},
                   emulator=FakeEmulator())


# ---------- 工具层 ----------

def test_list_tools_is_capability_whitelist(toolbox):
    names = [t["name"] for t in toolbox.list_tools()]
    assert {"app.launch", "ui.click", "ui.input", "device.call",
            "device.scene", "device.state", "screen.grab"} <= set(names)
    # 每个工具都带头描述与 JSON Schema（MCP tools/list 形态）
    click = next(t for t in toolbox.list_tools() if t["name"] == "ui.click")
    # key 只是给会话记录/固化回 DSL 的可选语义键，定位本身不消费（不应强制 agent 填）
    assert click["inputSchema"]["required"] == []
    ui_input = next(t for t in toolbox.list_tools() if t["name"] == "ui.input")
    assert ui_input["inputSchema"]["required"] == ["text"]

    # 只装配模拟器时，UI/应用工具不暴露（防 agent 调用必然失败的动作）
    emu_only = ToolBox({}, emulator=FakeEmulator())
    assert [t["name"] for t in emu_only.list_tools()] == [
        "device.scene", "device.call", "device.state"]
    assert ToolBox({}).list_tools() == []


def test_invoke_unknown_tool_and_missing_param(toolbox):
    with pytest.raises(ToolError):
        toolbox.invoke("nope.nope", {})
    with pytest.raises(ToolError):
        toolbox.invoke("device.call", {})          # 缺 cmd
    with pytest.raises(ToolError):
        toolbox.invoke("ui.click", {"key": "k"})   # 缺定位字段


def test_ui_click_and_input_use_existing_drivers(toolbox):
    out = toolbox.invoke("ui.click", {"key": "btn.start", "name": "开始检测",
                                      "control_type": "Button"})
    assert out["driver"] == "uia"
    assert out["rect"] == [10, 20, 100, 30]
    assert ("click", None) in toolbox.drivers["uia"].calls

    toolbox.invoke("ui.input", {"key": "edit.pressure", "name": "压力",
                                "text": "80", "clear": True})
    calls = toolbox.drivers["uia"].calls
    assert ("clear", None) in calls
    assert ("input", "80") in calls


def test_ui_input_clear_needs_uia_capability():
    box = ToolBox({"vision": FakeVisionDriver()}, emulator=FakeEmulator())
    with pytest.raises(ToolError) as e:
        box.invoke("ui.input", {"key": "k", "desc": "输入框", "text": "1",
                                "clear": True})
    assert "不支持 clear" in str(e.value)


def test_locator_field_requirements(toolbox):
    with pytest.raises(ToolError):
        toolbox.invoke("ui.click", {"key": "k"})               # uia 缺 name
    with pytest.raises(ToolError):
        toolbox.invoke("ui.click", {"key": "k", "driver": "vision"})  # 缺 desc
    out = toolbox.invoke("ui.click", {"key": "k", "driver": "vision",
                                      "desc": "开始检测按钮"})
    assert out["driver"] == "vision"


def test_device_tools_and_screen_grab(toolbox):
    emu = toolbox.emulator
    assert toolbox.invoke("device.scene", {"scene_id": "normal_sample_a"})["ok"]
    assert emu.scenes == ["normal_sample_a"]

    resp = toolbox.invoke("device.call", {"cmd": "set_sample",
                                          "args": {"id": "S001"}})
    assert resp["ok"] is True

    state = toolbox.invoke("device.state", {"path": "/state/run"})
    assert state["state"] == "completed"

    shot = toolbox.invoke("screen.grab", {})
    assert shot["bytes"] > 0 and shot["png_base64"]


# ---------- 会话固化 ----------

def _recorded_session(toolbox):
    s = ExplorationSession(toolbox, case_id="TC-9001", title="报警联动探索",
                           preconditions=["模拟器场景: normal_sample_a"])
    s.call("app.launch", {"exe": "target.exe", "title_re": ".*主窗口.*"})
    s.call("app.wait_ready", {"timeout_ms": 8000})
    s.call("ui.click", {"key": "btn.start_detect", "name": "开始检测",
                        "control_type": "Button", "desc": "底部开始检测按钮"})
    s.call("ui.input", {"key": "edit.pressure", "name": "压力", "text": "80"})
    s.call("device.scene", {"scene_id": "alarm_scene"})
    s.call("device.call", {"cmd": "set_sample", "args": {"id": "S001"}})
    s.call("device.state", {"path": "/state/run"})     # 观测步骤：不进 DSL
    return s


def test_session_records_failures_and_reraises(toolbox):
    toolbox.drivers["uia"].fail_locate = True
    s = ExplorationSession(toolbox)
    with pytest.raises(LocateError):
        s.call("ui.click", {"key": "btn.x", "name": "X"})
    assert len(s.steps) == 1
    assert s.steps[0].ok is False
    assert "locate" in (s.steps[0].error or "").lower()


def test_session_to_case_maps_tools_and_passes_dsl_validation(toolbox):
    s = _recorded_session(toolbox)
    case = s.to_case()
    assert case["case_id"] == "TC-9001"
    assert [st["action"] for st in case["steps"]] == [
        "app.launch", "app.wait_ready", "ui.click", "ui.input",
        "device.scene", "device.call"]
    # 固化产物必须是合法 DSL（走 pydantic 校验才允许进人审链路）
    validated = DslCase.model_validate(case)
    assert [st.id for st in validated.steps] == ["s1", "s2", "s3", "s4", "s5", "s6"]

    click = case["steps"][2]
    assert click["target"]["key"] == "btn.start_detect"
    assert click["target"]["primary"]["type"] == "uia"
    assert click["target"]["fallback"] == {"type": "visual",
                                           "desc": "底部开始检测按钮"}
    assert case["steps"][3]["value"] == "80"
    assert case["steps"][5]["args"]["cmd"] == "set_sample"
    assert "跳过 1 步" in case["notes"]          # device.state 是观测步骤


def test_session_roundtrip_and_draft_text(toolbox):
    s = _recorded_session(toolbox)
    restored = ExplorationSession.from_dict(json.loads(json.dumps(s.to_dict())))
    assert [st.tool for st in restored.steps] == [st.tool for st in s.steps]
    draft = json.loads(restored.to_draft_text("json"))
    assert len(draft["steps"]) == 6
    assert "TC-9001" in restored.to_draft_text("yaml")


def test_session_imports_as_draft_then_human_accepts(db_conn, orch_factory):
    """探索路径固化的落点：**待审草稿**，人审 accept 后才进 active 主链路。"""
    from testpilot.storage import repos

    orch = orch_factory()
    s = _recorded_session(ToolBox({"uia": FakeDriver("uia")},
                                  emulator=FakeEmulator()))
    out = s.import_draft(orch.cases)
    assert out["case_id"] == "TC-9001"
    assert repos.get_case(db_conn, "TC-9001")["status"] == "draft"

    orch.cases.accept("TC-9001")
    assert repos.get_case(db_conn, "TC-9001")["status"] == "active"


# ---------- MCP server ----------

def test_mcp_initialize_list_and_call(toolbox):
    server = McpServer(toolbox)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"]
    assert init["result"]["serverInfo"]["name"] == "testpilot-emulator"
    assert server.handle({"jsonrpc": "2.0",
                          "method": "notifications/initialized"}) is None

    tools = server.handle({"jsonrpc": "2.0", "id": 2,
                           "method": "tools/list"})["result"]["tools"]
    assert any(t["name"] == "device.call" for t in tools)

    ok = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "device.call",
                                   "arguments": {"cmd": "query_state"}}})
    text = ok["result"]["content"][0]["text"]
    assert json.loads(text)["ok"] is True
    assert ok["result"]["isError"] is False

    # 工具失败按 MCP 约定走 isError 结果，agent 可继续探索
    bad = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "nope", "arguments": {}}})
    assert bad["result"]["isError"] is True

    missing = server.handle({"jsonrpc": "2.0", "id": 5, "method": "no.such"})
    assert missing["error"]["code"] == -32601


def test_mcp_stdio_loop(toolbox):
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "not-a-json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    out = io.StringIO()
    assert McpServer(toolbox).serve_stdio(io.StringIO(lines), out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(responses) == 3                     # 通知不回包
    assert responses[1]["error"]["code"] == -32700  # 解析失败
    assert responses[2]["id"] == 2                  # 后续消息不受影响

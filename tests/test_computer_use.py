"""ComputerUseSession / Explore MCP 自动化增强测试。

全部使用内存假 UIA 树，不接触真实桌面；真实 Qt/UIA 冒烟仍需在 Windows 开发机执行。
"""
from types import SimpleNamespace

import pytest

from testpilot.core.contracts import DriverManifest, ExecContext, Screenshot
from testpilot.core.errors import ActionError
from testpilot.explore.computer_use import ComputerUseError, ComputerUseSession
from testpilot.explore.mcp_server import McpServer
from testpilot.explore.session import ExplorationSession
from testpilot.explore.toolbox import ToolBox


class Node:
    def __init__(self, name, control_type="Button", auto_id="",
                 value="", rect=(10, 10, 100, 30), children=None):
        self.element_info = SimpleNamespace(
            name=name,
            automation_id=auto_id,
            control_type=control_type,
            class_name=f"Fake{control_type}",
            framework_id="Qt",
        )
        self._value = value
        self.iface_value = SimpleNamespace(CurrentValue=value)
        self._rect = rect
        self._children = list(children or [])
        self._exists = True
        self._visible = True
        self.clicks = 0
        self.selects = []

    def children(self):
        return list(self._children)

    def exists(self, timeout=0):
        return self._exists

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def rectangle(self):
        x, y, w, h = self._rect
        return SimpleNamespace(left=x, top=y, right=x + w, bottom=y + h)

    def click_input(self):
        self.clicks += 1

    def set_edit_text(self, value):
        self._value = str(value)
        self.iface_value.CurrentValue = self._value

    def window_text(self):
        return self._value or self.element_info.name

    def select(self, value):
        self.selects.append(value)


class IgnoredWriteNode(Node):
    def set_edit_text(self, value):
        # 模拟 controlled React/特殊 Qt 控件：调用成功，但界面值根本没变化。
        return None


class FakeUiaDriver:
    manifest = DriverManifest(
        kind="interaction",
        name="uia",
        version="test",
        capabilities=frozenset({
            "click", "input", "select", "grab", "wait_ready", "wait_gone",
            "launch", "close", "read_text",
        }),
        entry="",
    )

    def __init__(self, top):
        self._top = top
        self.clicked = []

    def _rect_of(self, wrapper):
        r = wrapper.rectangle()
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def health(self):
        return SimpleNamespace(ok=True, detail="fake")

    def grab(self, rect, ctx):
        return Screenshot(png=b"fake-png", rect=rect or self._rect_of(self._top))

    def read_text(self, el):
        return el.handle._value

    def click(self, el, ctx):
        ctx.abort.check()
        el.handle.click_input()
        self.clicked.append(el.handle.element_info.name)

    def input_text(self, el, text, ctx):
        el.handle.set_edit_text(text)

    def select(self, el, value, ctx):
        el.handle.select(value)

    def locate(self, loc, ctx, timeout_ms=None):
        raise AssertionError("本组测试只走 element_index，不应重新 locator")

    def wait_ready(self, ctx, timeout_ms):
        return True

    def wait_gone(self, el, ctx, timeout_ms):
        return True


def _tree(edit_cls=Node):
    edit = edit_cls("样本号", "Edit", "sampleId", "S001",
                    rect=(20, 80, 180, 32))
    button = Node("开始检测", "Button", "startButton",
                  rect=(20, 140, 120, 36))
    root = Node("主窗口", "Window", "mainWindow", rect=(0, 0, 800, 600),
                children=[edit, button])
    return root, edit, button


def _session(edit_cls=Node):
    root, edit, button = _tree(edit_cls)
    driver = FakeUiaDriver(root)
    ctx = ExecContext(run_id=0, case_exec_id=0, window_rect=(0, 0, 800, 600))
    return ComputerUseSession({"uia": driver}, ctx), driver, edit, button


def test_observe_builds_bounded_ax_snapshot_and_indexes():
    session, _, _, _ = _session()
    state = session.observe(include_ax=True, include_screenshot=False,
                            max_nodes=20, max_depth=8)

    assert state["snapshot_id"] == 1
    assert state["window_rect"] == [0, 0, 800, 600]
    assert [e["name"] for e in state["elements"]] == ["主窗口", "样本号", "开始检测"]
    assert state["elements"][1]["automation_id"] == "sampleId"
    assert state["elements"][1]["selector"] == {
        "type": "uia", "automation_id": "sampleId", "control_type": "Edit"}
    assert state["elements"][1]["stable_key"] == "edit.sampleId"
    assert state["elements"][1]["selector_quality"] == "high"
    assert state["elements"][1]["selector_score"] >= 0.95
    assert "AutomationId" in state["elements"][1]["selector_reason"]
    assert state["window_title"] == "主窗口"
    assert "set_value" in state["elements"][1]["actions"]
    assert state["truncated"] is False

    el, meta = session.resolve(3, snapshot_id=state["snapshot_id"])
    assert el.rect == (20, 140, 120, 36)
    assert meta["name"] == "开始检测"


def test_duplicate_accessible_names_get_lower_selector_score():
    a = Node("确认", "Button", "")
    b = Node("确认", "Button", "")
    root = Node("主窗口", "Window", "mainWindow", children=[a, b])
    session = ComputerUseSession(
        {"uia": FakeUiaDriver(root)},
        ExecContext(run_id=0, case_exec_id=0, window_rect=(0, 0, 800, 600)),
    )
    state = session.observe()
    buttons = [item for item in state["elements"] if item["name"] == "确认"]
    assert len(buttons) == 2
    assert all(item["selector_quality"] == "low" for item in buttons)
    assert all(item["selector_score"] < 0.65 for item in buttons)
    assert all("重复" in item["selector_reason"] for item in buttons)


def test_query_registers_matches_without_screenshot():
    session, _, _, _ = _session()
    result = session.query(text="开始", control_type="Button")
    assert result["matches"][0]["name"] == "开始检测"
    index = result["matches"][0]["element_index"]
    _, meta = session.resolve(index, snapshot_id=result["snapshot_id"])
    assert meta["automation_id"] == "startButton"


def test_screenshot_only_observation_invalidates_old_element_indexes():
    session, _, _, _ = _session()
    state = session.observe(include_ax=True)
    old = state["elements"][1]["element_index"]

    shot = session.observe(include_ax=False, include_screenshot=True)
    assert shot["screenshot_bytes"] == len(b"fake-png")
    assert shot["snapshot_id"] == state["snapshot_id"] + 1

    with pytest.raises(ComputerUseError, match="失效"):
        session.resolve(old)


def test_resolve_fails_closed_when_element_identity_changes():
    session, _, edit, _ = _session()
    state = session.observe()
    index = state["elements"][1]["element_index"]
    edit.element_info.automation_id = "anotherControl"

    with pytest.raises(ComputerUseError, match="身份已变化"):
        session.resolve(index, snapshot_id=state["snapshot_id"])


def test_set_value_reads_back_real_ui_state():
    session, _, _, _ = _session()
    state = session.observe()
    el, _ = session.resolve(state["elements"][1]["element_index"])

    out = session.set_value(el, "S002", timeout_ms=100)
    assert out["verified"] is True
    assert out["actual"] == "S002"
    assert out["changed"] is True


def test_set_value_rejects_silent_write_that_did_not_change_control():
    session, _, _, _ = _session(IgnoredWriteNode)
    state = session.observe()
    el, _ = session.resolve(state["elements"][1]["element_index"])

    with pytest.raises(ActionError, match="写入未生效"):
        session.set_value(el, "S999", timeout_ms=60)


def test_toolbox_actions_can_reuse_observed_element_index():
    root, edit, button = _tree()
    driver = FakeUiaDriver(root)
    box = ToolBox({"uia": driver},
                  ctx=ExecContext(run_id=0, case_exec_id=0,
                                  window_rect=(0, 0, 800, 600)))

    state = box.invoke("ui.observe", {"include_ax": True})
    btn_index = next(e["element_index"] for e in state["elements"]
                     if e["automation_id"] == "startButton")
    edit_index = next(e["element_index"] for e in state["elements"]
                      if e["automation_id"] == "sampleId")

    clicked = box.invoke("ui.click", {
        "element_index": btn_index,
        "snapshot_id": state["snapshot_id"],
    })
    assert clicked["target"]["automation_id"] == "startButton"
    assert button.clicks == 1

    changed = box.invoke("ui.set_value", {
        "element_index": edit_index,
        "snapshot_id": state["snapshot_id"],
        "value": "S008",
        "verify_timeout_ms": 100,
    })
    assert changed["verified"] is True
    assert edit._value == "S008"


def test_mcp_returns_image_block_and_turn_end_invalidates_index():
    root, _, _ = _tree()
    driver = FakeUiaDriver(root)
    box = ToolBox({"uia": driver},
                  ctx=ExecContext(run_id=0, case_exec_id=0,
                                  window_rect=(0, 0, 800, 600)))
    server = McpServer(box)

    observed = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "ui.observe",
            "arguments": {"include_ax": True, "include_screenshot": True},
        },
    })
    blocks = observed["result"]["content"]
    assert [block["type"] for block in blocks] == ["text", "image"]

    import json
    payload = json.loads(blocks[0]["text"])
    index = payload["elements"][1]["element_index"]
    snapshot_id = payload["snapshot_id"]

    assert server.handle({
        "jsonrpc": "2.0", "method": "notifications/turn-ended"
    }) is None

    stale = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "ui.click",
            "arguments": {"element_index": index, "snapshot_id": snapshot_id},
        },
    })
    assert stale["result"]["isError"] is True


def test_element_index_action_solidifies_to_stable_dsl_locator():
    root, _, button = _tree()
    driver = FakeUiaDriver(root)
    box = ToolBox({"uia": driver},
                  ctx=ExecContext(run_id=0, case_exec_id=0,
                                  window_rect=(0, 0, 800, 600)))
    session = ExplorationSession(box, case_id="TC-AUTO", title="自动探索")
    state = session.call("ui.observe", {"include_ax": True})
    index = next(e["element_index"] for e in state["elements"]
                 if e["automation_id"] == "startButton")

    session.call("ui.click", {
        "element_index": index,
        "snapshot_id": state["snapshot_id"],
    })
    case = session.to_case()
    assert len(case["steps"]) == 1
    step = case["steps"][0]
    assert step["action"] == "ui.click"
    assert step["target"]["key"] == "button.startButton"
    assert step["target"]["primary"] == {
        "type": "uia",
        "automation_id": "startButton",
        "control_type": "Button",
    }
    assert "element_index" not in str(step)
    assert button.clicks == 1

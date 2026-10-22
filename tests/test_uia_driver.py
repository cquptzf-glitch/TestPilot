"""UIA 驱动单测 — 用假 ``pywinauto`` 模块覆盖定位/操作/就绪/关闭全分支。

为什么这样测：真机 UIA 需要被测软件与桌面会话，无法进 CI；
但驱动的逻辑（criteria 组装、超时轮询、异常分类、动作分派）**完全可以离线覆盖**。
假模块在 ``UIADriver.__init__`` 之前注入 ``sys.modules``，因此 import 期惰性生效。
"""
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from testpilot.core.contracts import ExecContext, LocatedElement
from testpilot.core.dsl import Locator
from testpilot.core.errors import ActionError, AbortRequested, AppError, LocateError
from testpilot.infra.abort import AbortSignal

# ---------------------------------------------------------------- 假 pywinauto

DEFAULT_RECT = (10, 20, 110, 50)      # left, top, right, bottom


class FakeWindow:
    """模拟 pywinauto 的 wrapper：只实现驱动真正用到的那部分 API。"""

    def __init__(self, *, exists=True, text="", rect=DEFAULT_RECT,
                 child=None, child_sequence=None):
        self._exists = exists
        self._text = text
        self._rect = rect
        self._child = child                     # 固定返回的子控件
        self._child_seq = list(child_sequence) if child_sequence else None
        self.crits: list[dict] = []
        self.clicks = 0
        self.focuses = 0
        self.keys: list[str] = []
        self.selects: list = []
        self.closed = 0
        self.selected_ok_text = "A"              # select(str) 能成功的值
        self.child_error: Exception | None = None

    # --- wrapper API ---

    def set_focus(self) -> None:
        self.focuses += 1

    def exists(self, timeout=0):
        return self._exists

    def wait(self, spec, timeout=None):
        return self

    def window_text(self):
        return self._text

    def rectangle(self):
        left, top, right, bottom = self._rect
        return SimpleNamespace(left=left, top=top, right=right, bottom=bottom,
                               width=lambda: right - left,
                               height=lambda: bottom - top)

    def child_window(self, **crit):
        self.crits.append(crit)
        if self._child_seq is not None:
            item = self._child_seq.pop(0) if len(self._child_seq) > 1 \
                else self._child_seq[0]
            if isinstance(item, Exception):
                raise item
            return item
        if self._child is None:
            return FakeWindow(exists=False)
        return self._child

    def wrapper_object(self):
        return self

    def click_input(self, **kw):
        self.clicks += 1

    def type_keys(self, keys, pause=None, with_spaces=False, **kw):
        self.keys.append(keys)

    def set_edit_text(self, text):
        self.keys.append(f"<set>{text}")

    def select(self, value):
        if isinstance(value, int) or value == self.selected_ok_text:
            self.selects.append(value)
            return
        raise ValueError(f"no such item: {value}")

    def close(self):
        self.closed += 1


class FakeApplication:
    instances: list["FakeApplication"] = []

    def __init__(self, backend="uia"):
        self.backend = backend
        self.process = 101
        self.started: list[tuple] = []
        self.killed = 0
        self._top = FakeWindow(text="主窗口")
        self.window_calls: list = []
        FakeApplication.instances.append(self)

    def start(self, cmd, timeout=None):
        self.started.append((cmd, timeout))
        return self

    def top_window(self):
        return self._top

    def window(self, title_re=None, **kw):
        self.window_calls.append(title_re)
        return self._top

    def kill(self):
        self.killed += 1


class FakeDesktop:
    def windows(self, **criteria):
        return [self._window]

    def __init__(self, backend="uia"):
        self.backend = backend
        self._window = FakeWindow(text="已运行窗口")
        self._window.handle = 12345
        self.window_calls: list = []

    def window(self, title_re=None, **kw):
        self.window_calls.append(title_re)
        return self._window


@pytest.fixture()
def uia(monkeypatch):
    """注入假 pywinauto，并返回 (driver, 工厂句柄) 便于断言。"""
    mod = types.ModuleType("pywinauto")
    mod.Application = FakeApplication
    mod.Desktop = FakeDesktop
    mod.__version__ = "fake-0.6.8"
    monkeypatch.setitem(sys.modules, "pywinauto", mod)
    FakeApplication.instances.clear()

    from testpilot.drivers.uia_driver import UIADriver

    driver = UIADriver({"backend": "uia", "uia_timeout_ms": 200,
                        "ready_poll_ms": 5, "input_mode": "keys"})
    return driver


def _locator(**kw):
    data = {"type": "uia", "name": "开始检测", "control_type": "Button"}
    data.update(kw)
    return Locator.model_validate(data)


def _ctx(abort=None):
    return ExecContext(run_id=1, case_exec_id=1, abort=abort or AbortSignal())


# ---------------------------------------------------------------- 基础

def test_health_reports_pywinauto_version(uia):
    rep = uia.health()
    assert rep.ok is True
    assert "fake-0.6.8" in rep.detail


def test_manifest_is_module_level_and_complete():
    from testpilot.drivers.uia_driver import MANIFEST
    assert MANIFEST.name == "uia"
    assert {"click", "input", "select", "grab", "wait_ready", "wait_gone",
            "launch", "close", "read_text"} <= set(MANIFEST.capabilities)
    assert MANIFEST.entry == "testpilot.drivers.uia_driver:create"


# ---------------------------------------------------------------- 启动 / 关闭

def test_list_windows_returns_visible_native_windows(uia):
    rows = uia.list_windows(limit=10)
    assert len(rows) == 1
    assert rows[0]["title"] == "已运行窗口"
    assert rows[0]["handle"] == 12345
    assert rows[0]["rect"] == [10, 20, 100, 30]


def test_locate_walks_ancestor_scope_chain(uia):
    button = FakeWindow(text="确认")
    panel = FakeWindow(text="结果面板", child=button)
    root = FakeWindow(text="主窗口", child=panel)
    uia._top = root

    loc = Locator.model_validate({
        "type": "uia",
        "name": "确认",
        "control_type": "Button",
        "ancestors": [
            {"automation_id": "resultPanel", "control_type": "Pane"},
        ],
    })
    out = uia.locate(loc, _ctx(), timeout_ms=50)

    assert out.handle is button
    assert root.crits[0]["auto_id"] == "resultPanel"
    assert root.crits[0]["control_type"] == "Pane"
    assert panel.crits[0]["control_type"] == "Button"


def test_attach_handle_uses_exact_native_window(uia):
    rect = uia.attach_handle(12345, timeout_s=0.2)
    assert rect == (10, 20, 100, 30)
    assert uia._native_top.handle == 12345


def test_launch_with_exe_and_args_returns_physical_rect(uia):
    rect = uia.launch({"exe": "target.exe", "args": "--test-mode"}, timeout_s=5)
    assert rect == (10, 20, 100, 30)          # width/height 由边界相减得出
    app = FakeApplication.instances[-1]
    assert app.started == [("target.exe --test-mode", 5)]


def test_launch_with_title_re_only_attaches(uia):
    rect = uia.launch({"title_re": ".*主窗口.*"})
    assert rect[2:] == (100, 30)
    assert FakeApplication.instances == []    # 未新建进程


def test_launch_requires_exe_or_title_re(uia):
    with pytest.raises(AppError):
        uia.launch({})


def test_close_graceful_uses_window_close(uia):
    uia.launch({"exe": "a.exe"})
    top = uia._top
    uia.close("graceful")
    assert top.closed == 1
    assert uia._app is None and uia._top is None


def test_close_kill_uses_application_kill(uia):
    uia.launch({"exe": "a.exe"})
    app = FakeApplication.instances[-1]
    uia.close("kill")
    assert app.killed == 1


def test_close_without_launch_is_noop(uia):
    uia.close()                                # 不抛异常


def test_close_tolerates_wrapper_exception(uia):
    uia.launch({"exe": "a.exe"})

    class Boom(FakeWindow):
        def close(self):
            raise RuntimeError("window gone")

    uia._top = Boom()
    uia.close()                                # 只告警，不抛
    assert uia._top is None


# ---------------------------------------------------------------- 定位

def test_locate_returns_element_and_rect(uia):
    uia.launch({"exe": "a.exe"})
    uia._top._child = FakeWindow(text="开始检测")
    el = uia.locate(_locator(), _ctx(), timeout_ms=100)
    assert el.rect == (10, 20, 100, 30)
    assert el.confidence == 1.0
    criteria = uia._top.crits[0]
    assert criteria["control_type"] == "Button"
    assert criteria["predicate_func"](SimpleNamespace(name="开始检测"))
    assert not criteria["predicate_func"](SimpleNamespace(name="其他按钮"))


def test_locate_uses_automation_id_and_found_index(uia):
    uia.launch({"exe": "a.exe"})
    uia._top._child = FakeWindow()
    uia.locate(_locator(name=None, automation_id="btnStart", index=2), _ctx())
    assert uia._top.crits[0] == {"auto_id": "btnStart", "control_type": "Button",
                                "found_index": 2}


def test_locate_rejects_non_uia_locator(uia):
    uia.launch({"exe": "a.exe"})
    with pytest.raises(LocateError):
        uia.locate(Locator.model_validate({"type": "template", "template": "a.png"}),
                   _ctx())


def test_locate_without_window_is_app_error(uia):
    with pytest.raises(AppError):
        uia.locate(_locator(), _ctx())


def test_locate_without_any_criteria_raises(uia):
    uia.launch({"exe": "a.exe"})
    with pytest.raises(LocateError):
        uia.locate(Locator.model_validate({"type": "uia"}), _ctx())


def test_locate_timeout_reports_searched_criteria(uia):
    uia.launch({"exe": "a.exe"})          # child 默认不存在
    with pytest.raises(LocateError) as e:
        uia.locate(_locator(), _ctx(), timeout_ms=60)
    assert "name" in e.value.searched and "开始检测" in e.value.searched
    assert e.value.confidence is None


def test_locate_retries_after_query_exception(uia):
    uia.launch({"exe": "a.exe"})
    uia._top._child_seq = [RuntimeError("UIA busy"), FakeWindow()]
    el = uia.locate(_locator(), _ctx(), timeout_ms=200)
    assert el.rect[2] == 100
    assert len(uia._top.crits) == 2


def test_locate_propagates_abort(uia):
    uia.launch({"exe": "a.exe"})
    event = threading.Event()
    event.set()
    with pytest.raises(AbortRequested):
        uia.locate(_locator(), _ctx(AbortSignal(event)), timeout_ms=100)


# ---------------------------------------------------------------- 就绪等待

def test_wait_ready_true_when_window_exists(uia):
    uia.launch({"exe": "a.exe"})
    assert uia.wait_ready(_ctx(), 50) is True


def test_wait_ready_false_without_window(uia):
    assert uia.wait_ready(_ctx(), 10) is False


def test_wait_ready_timeout_when_window_missing(uia):
    uia.launch({"exe": "a.exe"})
    uia._top._exists = False
    assert uia.wait_ready(_ctx(), 30) is False


def test_wait_ready_requires_ready_control(uia):
    from testpilot.drivers.uia_driver import UIADriver

    driver = UIADriver({"ready_poll_ms": 5,
                        "ready_control": {"control_type": "StatusBar"}})
    driver.launch({"exe": "a.exe"})
    assert driver.wait_ready(_ctx(), 30) is False          # 判据控件不存在

    driver._top._child = FakeWindow()
    assert driver.wait_ready(_ctx(), 30) is True           # 控件出现 -> 就绪


def test_wait_gone_true_and_false(uia):
    uia.launch({"exe": "a.exe"})
    uia._top._child = FakeWindow()          # 先让 _locator() 能定位成功
    el = LocatedElement(driver=uia.locate(_locator(), _ctx()).driver,
                        handle=FakeWindow(exists=False), rect=(0, 0, 1, 1))
    assert uia.wait_gone(el, _ctx(), 10) is True

    el2 = LocatedElement(driver=el.driver, handle=FakeWindow(exists=True),
                         rect=(0, 0, 1, 1))
    assert uia.wait_gone(el2, _ctx(), 20) is False


def test_wait_gone_true_when_query_raises(uia):
    class Boom(FakeWindow):
        def exists(self, timeout=0):
            raise RuntimeError("destroyed")

    el = LocatedElement(driver=None, handle=Boom(), rect=(0, 0, 1, 1))
    assert uia.wait_gone(el, _ctx(), 10) is True


# ---------------------------------------------------------------- 操作

def test_click_uses_real_mouse_input(uia):
    uia.launch({"exe": "a.exe"})
    win = FakeWindow()
    uia.click(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)), _ctx())
    # 点击前必须先激活窗口：物理点击永远落在坐标最上层窗口，遮挡时会点空
    assert win.focuses == 1
    assert win.clicks == 1


def test_click_failure_is_action_error(uia):
    class Boom(FakeWindow):
        def click_input(self, **kw):
            raise RuntimeError("blocked")

    with pytest.raises(ActionError):
        uia.click(LocatedElement(driver=None, handle=Boom(), rect=(0, 0, 1, 1)),
                  _ctx())


def test_input_text_keys_mode(uia):
    win = FakeWindow()
    uia.input_text(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)),
                   "S001", _ctx())
    assert win.keys == ["S001"]


def test_input_text_set_text_mode(monkeypatch):
    mod = types.ModuleType("pywinauto")
    mod.Application = FakeApplication
    mod.Desktop = FakeDesktop
    monkeypatch.setitem(sys.modules, "pywinauto", mod)
    from testpilot.drivers.uia_driver import UIADriver

    driver = UIADriver({"input_mode": "set_text"})
    win = FakeWindow()
    driver.input_text(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)),
                      "S002", _ctx())
    assert win.keys == ["<set>S002"]


def test_select_all_and_delete(uia):
    win = FakeWindow()
    uia.select_all_and_delete(
        LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)), _ctx())
    assert win.keys == ["^a{BACKSPACE}"]


def test_select_by_text(uia):
    win = FakeWindow()
    uia.select(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)), "A", _ctx())
    assert win.selects == ["A"]


def test_select_falls_back_to_index(uia):
    win = FakeWindow()
    uia.select(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)), "1", _ctx())
    assert win.selects == [1]


def test_select_reports_action_error(uia):
    win = FakeWindow()
    with pytest.raises(ActionError) as e:
        uia.select(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)),
                   "不存在", _ctx())
    assert "选择失败" in str(e.value)


def test_read_text_strips(uia):
    win = FakeWindow(text="  S001  ")
    assert uia.read_text(LocatedElement(driver=None, handle=win,
                                        rect=(0, 0, 1, 1))) == "S001"


def test_read_text_error_is_action_error(uia):
    class Boom(FakeWindow):
        def window_text(self):
            raise RuntimeError("gone")

    with pytest.raises(ActionError):
        uia.read_text(LocatedElement(driver=None, handle=Boom(), rect=(0, 0, 1, 1)))


def test_action_propagates_abort(uia):
    event = threading.Event()
    event.set()
    win = FakeWindow()
    with pytest.raises(AbortRequested):
        uia.click(LocatedElement(driver=None, handle=win, rect=(0, 0, 1, 1)),
                  _ctx(AbortSignal(event)))


# ---------------------------------------------------------------- 截图委托

def test_grab_delegates_to_shared_grab_screen(uia, monkeypatch):
    from testpilot.core.contracts import Screenshot
    from testpilot.drivers import vision_driver

    seen = {}

    def fake_grab(rect, backend="mss"):
        seen["rect"] = rect
        return Screenshot(png=b"PNG", rect=rect)

    monkeypatch.setattr(vision_driver, "grab_screen", fake_grab)
    shot = uia.grab((1, 2, 3, 4), _ctx())
    assert shot.png == b"PNG"
    assert seen["rect"] == (1, 2, 3, 4)


def test_attach_uses_desktop_window(uia):
    rect = uia.attach(".*主窗口.*", timeout_s=1)
    assert rect == (10, 20, 100, 30)


def test_create_factory_returns_driver(monkeypatch):
    mod = types.ModuleType("pywinauto")
    mod.Application = FakeApplication
    mod.Desktop = FakeDesktop
    monkeypatch.setitem(sys.modules, "pywinauto", mod)
    from testpilot.drivers.uia_driver import create

    assert create({"backend": "uia"}).manifest.name == "uia"


def test_attach_resolves_native_handle_before_uia_query(uia, monkeypatch):
    seen = {}
    def resolve(**criteria):
        seen.update(criteria)
        return uia._desktop._window
    monkeypatch.setattr(uia._desktop, "window", resolve)
    uia.attach("^Mock$", timeout_s=0.1)
    assert seen == {"handle": 12345}


def test_attach_rejects_ambiguous_native_windows(uia, monkeypatch):
    monkeypatch.setattr(FakeDesktop, "windows", lambda *a, **k: [FakeWindow(), FakeWindow()])
    with pytest.raises(AppError, match="多个"):
        uia.attach("Mock", timeout_s=0.1)


def test_attach_missing_window_has_a_deadline(uia, monkeypatch):
    monkeypatch.setattr(FakeDesktop, "windows", lambda *a, **k: [])
    with pytest.raises(AppError, match="未出现"):
        uia.attach("Mock", timeout_s=0)


def test_locator_name_matches_accessible_name_instead_of_qt_edit_value():
    from testpilot.drivers.uia_driver import UIADriver
    criteria = UIADriver._crit(Locator(type="uia", name="Sample ID", control_type="Edit"))
    qt_edit = SimpleNamespace(name="Sample ID", rich_text="SAMPLE-001")
    assert criteria["predicate_func"](qt_edit)
    assert "title" not in criteria


class FakeQtCombo(FakeWindow):
    def __init__(self):
        super().__init__()
        self.element_info = SimpleNamespace(framework_id="Qt")
        self.options = ["Standard", "Precision", "Fast"]
        self.position = 2
        self.queries = 0

    def select(self, _):
        raise RuntimeError("Qt generic selection path unavailable")

    def expand(self):
        return self

    def descendants(self, **kwargs):
        self.queries += 1
        if self.queries == 1:
            return []  # Qt creates its accessible popup asynchronously.
        items = []
        for index, text in enumerate(self.options):
            item = SimpleNamespace(element_info=SimpleNamespace(name=text))
            item.click_input = lambda index=index: setattr(self, "position", index)
            items.append(item)
        return items

    def selected_text(self):
        return self.options[self.position]


@pytest.mark.parametrize("value,expected", [("Precision", 1), ("0", 0), ("2", 2)])
def test_qt_combo_waits_for_popup_and_checks_selected_value(uia, value, expected):
    control = FakeQtCombo()
    uia.select(LocatedElement(driver=None, handle=control, rect=(0, 0, 1, 1)), value, _ctx())
    assert control.position == expected
    assert control.queries >= 2
    assert control.focuses == 0  # Refocusing here would close the real Qt popup.


@pytest.mark.parametrize("value", ["Prec", "Missing", "3", "9"])
def test_qt_combo_rejects_prefix_and_out_of_range_index(uia, value):
    control = FakeQtCombo()
    with pytest.raises(ActionError, match="选择失败"):
        uia.select(LocatedElement(driver=None, handle=control, rect=(0, 0, 1, 1)), value, _ctx())
    assert control.position == 2


def test_qt_popup_wait_preserves_cancellation(uia):
    class CancelDuringPopup(FakeQtCombo):
        def descendants(self, **kwargs):
            signal.set()
            return []
    signal = threading.Event()
    control = CancelDuringPopup()
    with pytest.raises(AbortRequested):
        uia.select(LocatedElement(driver=None, handle=control, rect=(0, 0, 1, 1)),
                   "Fast", _ctx(AbortSignal(signal)))


def test_click_activates_native_window_before_a_qt_child(uia):
    uia.attach("^Mock$")
    native = uia._native_top
    control = FakeWindow()
    uia.click(LocatedElement(driver=None, handle=control, rect=(0, 0, 1, 1)), _ctx())
    assert native.focuses == 1
    assert control.focuses == 1 and control.clicks == 1


def test_launch_replaces_previously_attached_native_window(uia):
    uia.attach("previous")
    old = uia._native_top
    uia.launch({"exe": "new.exe"})
    assert uia._native_top is not old
    uia._focus_window()
    assert old.focuses == 0 and uia._native_top.focuses == 1


def test_attach_drops_previous_process_ownership(uia):
    uia.launch({"exe": "owned.exe"})
    owned = uia._app
    uia.attach("external")
    uia.close("kill")
    assert owned.killed == 0


def test_cell_locator_resolves_grid_coordinates(uia):
    cell = FakeWindow(text="42.5")
    table = FakeWindow()
    table.get_item = lambda row, col: cell if (row, col) == (79, 1) else None
    uia._top = FakeWindow(child=table)
    el = uia.locate(_locator(name="table", control_type="Table", row=79, column=1), _ctx())
    assert el.handle is cell and uia.read_text(el) == "42.5"


def test_toggle_is_idempotent_and_checks_result(uia):
    from unittest.mock import Mock
    control = FakeWindow()
    state = [0]
    control.get_toggle_state = lambda: state[0]
    def click(**_):
        control.clicks += 1
        state[0] = 1 - state[0]
    control.click_input = click
    el = LocatedElement("uia", control, DEFAULT_RECT)
    uia.toggle(el, True, _ctx())
    uia.toggle(el, True, _ctx())
    assert state[0] == 1 and control.clicks == 1
    uia.toggle(el, False, _ctx())
    assert state[0] == 0 and control.clicks == 2
    control.get_toggle_state = Mock(return_value=2)
    with pytest.raises(ActionError, match="不确定"):
        uia.toggle(el, True, _ctx())


def test_read_text_uses_value_and_range_patterns(uia):
    control = FakeWindow(text="Replicates")
    control.element_info = SimpleNamespace(control_type="Spinner")
    control.iface_range_value = SimpleNamespace(CurrentValue=17)
    el = LocatedElement("uia", control, DEFAULT_RECT)
    assert uia.read_text(el) == "17"
    control.element_info.control_type = "Edit"
    control.iface_value = SimpleNamespace(CurrentValue="literal +^{}")
    assert uia.read_text(el) == "literal +^{}"


def test_literal_input_escapes_keyboard_commands(uia):
    control = FakeWindow()
    el = LocatedElement("uia", control, DEFAULT_RECT)
    uia.input_text(el, "+^%{}~()", _ctx())
    assert control.keys == ["{+}{^}{%}{{}{}}{~}{(}{)}"]
    uia.keypress(el, "{F2}{ENTER}", _ctx())
    assert control.keys[-1] == "{F2}{ENTER}"


def test_qt_grid_scrolls_cells_even_when_provider_claims_visible(uia, monkeypatch):
    cell = FakeWindow(text="SAMPLE-080", rect=(10, 500, 110, 530))
    cell.is_visible = lambda: True
    table = FakeWindow(rect=(0, 0, 200, 200))
    table.element_info = SimpleNamespace(framework_id="Qt")
    table.iface_grid = SimpleNamespace(CurrentRowCount=80, CurrentColumnCount=3,
                                      GetItem=lambda r, c: cell)
    def keyboard(keys, **kw):
        table.keys.append(keys)
        cell._rect = (10, 150, 110, 180)
    table.type_keys = keyboard
    wrapper_module = types.ModuleType("pywinauto.controls.uiawrapper")
    wrapper_module.UIAWrapper = lambda info: info
    info_module = types.ModuleType("pywinauto.uia_element_info")
    info_module.UIAElementInfo = lambda item: item
    monkeypatch.setitem(sys.modules, "pywinauto.controls.uiawrapper", wrapper_module)
    monkeypatch.setitem(sys.modules, "pywinauto.uia_element_info", info_module)
    assert uia._grid_cell(table, 79, 0, _ctx()) is cell
    assert table.keys == ["^{END}{HOME}"]
    with pytest.raises(LocateError, match="越界"):
        uia._grid_cell(table, 80, 0, _ctx())


def test_click_refuses_coordinates_outside_attached_window(uia):
    uia._top = FakeWindow(rect=(0, 0, 200, 200))
    control = FakeWindow(rect=(0, 1000, 100, 1030))
    with pytest.raises(ActionError, match="不在当前窗口"):
        uia.click(LocatedElement("uia", control, (0, 1000, 100, 30)), _ctx())
    assert control.clicks == 0


def test_qt_uses_value_pattern_for_literal_unicode_text(uia):
    control = FakeWindow()
    control.element_info = SimpleNamespace(framework_id="Qt")
    el = LocatedElement("uia", control, DEFAULT_RECT)
    uia.input_text(el, "CONFIRM-17 +^{} 中文", _ctx())
    assert control.keys == ["<set>CONFIRM-17 +^{} 中文"]


def test_qml_combo_reads_inner_edit_value_instead_of_accessible_name(uia):
    control = FakeWindow(text="QML Mode")
    control.element_info = SimpleNamespace(control_type="ComboBox")
    inner = SimpleNamespace(iface_value=SimpleNamespace(CurrentValue="Rapid"))
    control.descendants = lambda **kw: [inner] if kw["control_type"] == "Edit" else []
    control.selected_text = lambda: "QML Mode"
    assert uia.read_text(LocatedElement("uia", control, DEFAULT_RECT)) == "Rapid"


def test_qml_combo_opens_by_click_and_searches_overlay(uia):
    control = FakeQtCombo()
    control.element_info.class_name = "ComboBox_QMLTYPE_1"
    control.position = 0
    control.descendants = lambda **kw: []
    items = []
    for index, text in enumerate(control.options):
        item = FakeWindow(text=text)
        item.element_info = SimpleNamespace(name=text)
        item.is_visible = lambda: True
        item.click_input = lambda index=index: setattr(control, "position", index)
        items.append(item)
    uia._top = FakeWindow()
    uia._top.descendants = lambda **kw: items
    uia.select(LocatedElement("uia", control, DEFAULT_RECT), "Precision", _ctx())
    assert control.clicks == 1 and control.position == 1


def test_qml_static_text_reads_accessible_display_text(uia):
    control = FakeWindow(text="")
    control.element_info = SimpleNamespace(control_type="Text", name="Canvas count: 1")
    assert uia.read_text(LocatedElement("uia", control, DEFAULT_RECT)) == "Canvas count: 1"

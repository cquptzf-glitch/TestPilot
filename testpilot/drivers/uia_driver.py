"""UIA 驱动 — pywinauto(uia backend) 封装（设计 8.3）。

工程约定（本文件遵守）：
- **pywinauto 惰性导入**：`MANIFEST` 是模块级常量，注册表与驱动一致性门禁
  不需要 GUI 运行时；未装 pywinauto 的环境仍可 import 本模块（创建实例才报错）。
- 定位自带超时（可被 ``timeout_ms`` 覆盖）；**不负责重试**（重试是引擎/路由职责）。
- 坐标基于物理像素；截图能力委托 mss（与视觉驱动共用实现）。
"""
import logging
import subprocess
import time

from testpilot.core.contracts import (DriverKind, DriverManifest, ExecContext,
                                      HealthReport, LocatedElement, Rect,
                                      Screenshot)
from testpilot.core.dsl import Locator, LocatorType
from testpilot.core.errors import AbortRequested, ActionError, AppError, LocateError
from testpilot.infra import dpi

log = logging.getLogger("testpilot.uia")

MANIFEST = DriverManifest(
    kind="interaction",
    name="uia",
    version="1.1.0",
    capabilities=frozenset({"click", "input", "select", "grab", "wait_ready",
                            "wait_gone", "launch", "attach", "close", "read_text",
                            "toggle", "keypress", "list_windows"}),
    config_schema={"type": "object", "properties": {
        "backend": {"type": "string", "default": "uia"},
        "uia_timeout_ms": {"type": "integer", "default": 3000},
        "ready_poll_ms": {"type": "integer", "default": 200},
        "input_mode": {"type": "string", "enum": ["keys", "set_text"],
                       "default": "keys"},
        "qt_text_input": {"type": "string", "enum": ["value", "keys"], "default": "value",
                          "description": "Qt 默认使用可访问值输入；keys 显式测试物理键盘"},
        "ready_control": {"type": "object",
                          "description": "可选：就绪判据控件（UIA crit 字典）"},
    }},
    entry="testpilot.drivers.uia_driver:create",
    priority=10,
)


class UIADriver:
    def __init__(self, cfg: dict):
        from pywinauto import Desktop  # 惰性导入：无 pywinauto 环境仍可 import 本模块

        dpi.ensure_dpi_aware()          # 进程级 DPI 感知：UIA 矩形与截图坐标一致性前提
        self.cfg = cfg or {}
        self.backend = self.cfg.get("backend", "uia")
        self._desktop = Desktop(backend=self.backend)
        self._app = None       # pywinauto.Application
        self._top = None       # 当前被测主窗口 wrapper
        self._native_top = None  # Native HWND wrapper for reliable Qt foreground activation

    manifest = MANIFEST

    # ---------- 生命周期 ----------

    def health(self) -> HealthReport:
        try:
            import pywinauto
            return HealthReport(ok=True,
                                detail=f"pywinauto {pywinauto.__version__} "
                                       f"backend={self.backend}")
        except Exception as e:  # pragma: no cover - 依赖缺失路径
            return HealthReport(ok=False, detail=f"pywinauto 不可用: {e}")

    def list_windows(self, limit: int = 100) -> list[dict]:
        """列出当前桌面可见顶层窗口，供 Explore/Agent 选择附加目标。

        这里刻意使用 win32 顶层窗口枚举，不做 Desktop-wide UIA 树搜索：
        某个第三方 UIA provider 卡住时不应拖死整个窗口列表。
        """
        try:
            from pywinauto import Desktop
            native = Desktop(backend="win32")
            rows = []
            for win in native.windows(visible_only=True)[:max(1, min(int(limit), 500))]:
                try:
                    title = str(win.window_text() or "").strip()
                except Exception:
                    title = ""
                try:
                    rect = self._rect_of(win)
                except Exception:
                    rect = None
                try:
                    pid = int(win.process_id())
                except Exception:
                    pid = None
                rows.append({
                    "title": title,
                    "handle": int(getattr(win, "handle", 0) or 0),
                    "pid": pid,
                    "rect": list(rect) if rect else None,
                })
            return rows
        except Exception as e:
            raise AppError(f"枚举桌面窗口失败: {e}") from e

    def launch(self, spec: dict, timeout_s: float = 30) -> Rect:
        """启动被测程序并等待主窗口；spec: {exe, args, title_re}。

        若只给 ``title_re``（不给 exe），则附加到已在运行的窗口（手工启动场景）。
        pywinauto 的启动/等待异常（AppStartError / TimeoutError 等）必须在这里
        转译成 AppError——引擎的失败策略只认工程错误类型，裸异常会击穿整个任务。
        """
        try:
            from pywinauto import Application

            title_re = spec.get("title_re")
            self._app, self._top, self._native_top = None, None, None
            if spec.get("exe"):
                cmd = _build_command(str(spec["exe"]), spec.get("args"))
                log.info(f"启动被测程序: {cmd}")
                self._app = Application(backend=self.backend).start(cmd,
                                                                   timeout=timeout_s)
                self._top = self._find_window(title_re, timeout_s,
                                              process=self._app.process)
            else:
                if not title_re:
                    raise AppError("app.launch 需要 exe 或 title_re")
                log.info(f"附加到已运行窗口: {title_re}")
                self._top = self._find_window(title_re, timeout_s)

            self._top.wait("visible ready", timeout=timeout_s)
            return self._rect_of(self._top)
        except AppError:
            raise
        except Exception as e:
            raise AppError(f"启动/附加被测程序失败: {e}") from e

    def attach(self, title_re: str, timeout_s: float = 10) -> Rect:
        """附加到已运行的窗口（异常转译口径与 launch 一致）。"""
        try:
            self._app, self._top, self._native_top = None, None, None
            self._top = self._find_window(title_re, timeout_s)
            self._top.wait("visible ready", timeout=timeout_s)
            return self._rect_of(self._top)
        except Exception as e:
            raise AppError(f"附加窗口失败: title_re={title_re!r} ({e})") from e

    def attach_handle(self, handle: int, timeout_s: float = 10) -> Rect:
        """按 HWND 精确附加，解决多个窗口标题相同导致的歧义。"""
        try:
            from pywinauto import Desktop
            wanted = int(handle)
            native = Desktop(backend="win32")
            deadline = time.monotonic() + timeout_s
            while True:
                matches = [w for w in native.windows(visible_only=True)
                           if int(getattr(w, "handle", 0) or 0) == wanted]
                if matches:
                    self._app = None
                    self._native_top = matches[0]
                    self._top = self._desktop.window(handle=wanted)
                    self._top.wait("visible ready", timeout=max(
                        0.1, deadline - time.monotonic()))
                    return self._rect_of(self._top)
                if time.monotonic() >= deadline:
                    raise AppError(f"被测窗口句柄不存在或不可见: {wanted}")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except AppError:
            raise
        except Exception as e:
            raise AppError(f"按句柄附加窗口失败: handle={handle!r} ({e})") from e

    def _find_window(self, title_re: str | None, timeout_s: float, process=None):
        """Resolve a native HWND before querying its UIA subtree.

        Desktop-wide UIA FindAll may block in an unrelated application's
        provider and bypass pywinauto's polling timeout.
        """
        from pywinauto import Desktop
        deadline = time.monotonic() + timeout_s
        native = Desktop(backend="win32")
        while True:
            criteria = {"visible_only": True}
            if title_re:
                criteria["title_re"] = title_re
            if process is not None:
                criteria["process"] = process
            windows = native.windows(**criteria)
            if len(windows) > 1:
                raise AppError(f"匹配到多个被测窗口: {title_re!r}")
            if windows:
                self._native_top = windows[0]
                return self._desktop.window(handle=windows[0].handle)
            if time.monotonic() >= deadline:
                raise AppError(f"被测窗口未出现: {title_re!r}")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def close(self, mode: str = "graceful") -> None:
        if self._app is None and self._top is None:
            return
        try:
            if mode in ("kill", "force"):     # force 是探索工具层的同义枚举
                if self._app is not None:
                    self._app.kill()
                elif self._top is not None:
                    self._top.close()
            elif self._top is not None:
                self._top.close()
            elif self._app is not None:
                self._app.kill()
        except Exception as e:  # 关闭失败只告警：用例已结束
            log.warning(f"关闭被测程序失败: {e}")
        finally:
            self._app, self._top, self._native_top = None, None, None

    # ---------- 就绪等待 ----------

    def wait_ready(self, ctx: ExecContext, timeout_ms: int) -> bool:
        """主窗口存在且可选就绪判据控件出现。

        被测程序实现测试端口（P1 适配的 busy/idle 信号）时优先走端口；
        此处为 UIA 兜底实现：窗口存在 + （可选）`ready_control` 出现。
        """
        if self._top is None:
            return False
        poll = float(self.cfg.get("ready_poll_ms", 200)) / 1000
        crit = self.cfg.get("ready_control")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            ctx.abort.check()
            try:
                if self._top.exists():
                    if not crit:
                        return True
                    if self._top.child_window(**crit).exists(timeout=0.1):
                        return True
            except Exception:
                pass  # 窗口查询异常 -> 继续轮询直到超时
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def wait_gone(self, el: LocatedElement, ctx: ExecContext,
                  timeout_ms: int) -> bool:
        """等待控件消失（``ui.wait state=gone``）。"""
        poll = float(self.cfg.get("ready_poll_ms", 200)) / 1000
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            ctx.abort.check()
            try:
                if not el.handle.exists():
                    return True
            except Exception:
                return True  # 查询异常通常意味着控件已销毁
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    # ---------- 定位 ----------

    def locate(self, loc: Locator, ctx: ExecContext,
               timeout_ms: int | None = None) -> LocatedElement:
        if loc.type != LocatorType.UIA:
            raise LocateError(f"uia 驱动不接受定位类型: {loc.type}")
        if self._top is None:
            raise AppError("尚未附加/启动被测窗口")

        crit = self._crit(loc)
        searched = {key: value for key, value in crit.items() if key != "predicate_func"}
        if loc.name:
            searched["name"] = loc.name
        t0 = time.monotonic()
        effective_ms = timeout_ms if timeout_ms is not None \
            else self.cfg.get("uia_timeout_ms", 3000)
        deadline = t0 + effective_ms / 1000
        last_error = None
        while True:
            ctx.abort.check()
            try:
                el = self._top.child_window(**crit)
                if el.exists(timeout=0):
                    wrapper = el.wrapper_object()
                    if loc.row is not None or loc.column is not None:
                        if loc.row is None or loc.column is None:
                            raise LocateError("表格定位必须同时指定 row 和 column")
                        wrapper = self._grid_cell(wrapper, loc.row, loc.column, ctx)
                    return LocatedElement(
                        driver=DriverKind.UIA, handle=wrapper,
                        rect=self._rect_of(wrapper), confidence=1.0)
            except Exception as e:  # 查询异常不致命：继续轮询直到超时
                last_error = e
                log.debug(f"UIA 查询异常（继续轮询）: {e}")
            if time.monotonic() >= deadline:
                raise LocateError(
                    f"UIA 未找到控件 crit={searched}（{effective_ms}ms）"
                    + (f": {last_error}" if last_error else ""),
                    confidence=None, searched=str(searched))
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _grid_cell(self, table, row: int, column: int, ctx=None):
        # pywinauto maps Qt Table to ListViewWrapper, whose get_item accepts only
        # one row index. Use the provider's Grid pattern to preserve both axes.
        try:
            grid = table.iface_grid
        except AttributeError:
            return table.get_item(row, column)
        if row >= grid.CurrentRowCount or column >= grid.CurrentColumnCount:
            raise LocateError(f"表格单元格越界: ({row}, {column})")
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo
        cell = UIAWrapper(UIAElementInfo(grid.GetItem(row, column)))
        try:
            cell.iface_scroll_item.ScrollIntoView()
        except Exception:
            pass
        def in_view(candidate):
            r, area = candidate.rectangle(), table.rectangle()
            x, y = (r.left + r.right) / 2, (r.top + r.bottom) / 2
            return r.right > r.left and r.bottom > r.top and (
                area.left <= x < area.right and area.top <= y < area.bottom)
        if not in_view(cell):
            try:
                table.iface_scroll.SetScrollPercent(-1, 100 * row / max(1, grid.CurrentRowCount - 1))
            except Exception:
                if getattr(table.element_info, "framework_id", "") != "Qt":
                    raise LocateError("表格未提供可用的滚动接口") from None
                # Qt Table reports offscreen cells as visible and may expose no
                # Scroll pattern. Navigate from the closest end using Qt's normal
                # keyboard behavior. This changes selection, never cell values.
                if ctx is not None:
                    ctx.abort.check()
                self._focus_window()
                count = grid.CurrentRowCount
                keys = "^{HOME}" if row <= count // 2 else "^{END}{HOME}"
                distance = row if row <= count // 2 else count - row - 1
                if distance:
                    keys += "{" + ("DOWN" if row <= count // 2 else "UP") + f" {distance}" + "}"
                if column:
                    keys += "{RIGHT " + str(column) + "}"
                table.type_keys(keys, pause=0.005, set_foreground=False)
            cell = UIAWrapper(UIAElementInfo(grid.GetItem(row, column)))
            if not in_view(cell):
                raise LocateError(f"滚动后单元格仍不在视口内: ({row}, {column})")
        return cell

    @staticmethod
    def _crit(loc: Locator) -> dict:
        crit: dict = {}
        if loc.automation_id:
            crit["auto_id"] = loc.automation_id
        if loc.name:
            # pywinauto's title filter compares rich_text, which is the editable
            # value for Qt QLineEdit. Locator.name means the UIA accessible name.
            name = loc.name
            crit["predicate_func"] = lambda info: info.name == name
        if loc.control_type:
            crit["control_type"] = loc.control_type
        if not crit:
            raise LocateError("uia 定位缺少 name/automation_id/control_type")
        if loc.index:
            crit["found_index"] = loc.index    # 多匹配时取第 N 个
        return crit

    # ---------- 操作 ----------

    def _focus_window(self) -> None:
        # Qt child widgets may have no HWND; their UIA SetFocus does not always
        # raise the containing window above another application.
        if self._native_top is not None:
            import ctypes
            hwnd = self._native_top.handle
            foreground = ctypes.windll.user32.GetForegroundWindow
            if foreground() == hwnd:
                return
            try:
                self._native_top.set_focus()
            except Exception:
                # Windows may report SetForegroundWindow failure after it has
                # already activated the requested HWND. Confirm actual state.
                if foreground() != hwnd:
                    user32 = ctypes.windll.user32
                    current = ctypes.windll.kernel32.GetCurrentThreadId()
                    active_thread = user32.GetWindowThreadProcessId(foreground(), None)
                    attached = bool(active_thread and user32.AttachThreadInput(
                        current, active_thread, True))
                    try:
                        user32.SetForegroundWindow(hwnd)
                    finally:
                        if attached:
                            user32.AttachThreadInput(current, active_thread, False)
                    if foreground() != hwnd:
                        raise ActionError("无法激活被测窗口；请保持桌面可交互") from None
        elif self._top is not None:
            self._top.set_focus()

    def click(self, el: LocatedElement, ctx: ExecContext) -> None:
        ctx.abort.check()
        try:
            # 真实鼠标事件（走完整消息链）；先激活目标窗口——click_input 的
            # 物理点击永远落在**该坐标最上层的窗口**，被其它窗口（如并排打开的
            # 浏览器）遮挡时会静默点空，这是"点击成功但控件没反应"的隐形坑。
            self._focus_window()
            r = el.handle.rectangle()
            if self._top is not None:
                area = self._top.rectangle()
                x, y = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                if not (r.right > r.left and r.bottom > r.top
                        and area.left <= x < area.right and area.top <= y < area.bottom):
                    raise ActionError("目标控件中心不在当前窗口内，拒绝物理点击")
            focusable = getattr(el.handle, "is_keyboard_focusable", lambda: True)
            if focusable():
                el.handle.set_focus()
            el.handle.click_input()  # 真实鼠标事件（走完整消息链）
        except Exception as e:
            raise ActionError(f"点击失败: {e}") from e

    def input_text(self, el: LocatedElement, text: str, ctx: ExecContext) -> None:
        ctx.abort.check()
        try:
            self._focus_window()
            # type_keys 走键盘消息；set_edit_text 直设值（无键盘事件）
            if self.cfg.get("input_mode", "keys") == "keys":
                info = getattr(el.handle, "element_info", None)
                if (getattr(info, "framework_id", "") == "Qt"
                        and self.cfg.get("qt_text_input", "value") == "value"):
                    # Qt can mistranslate VK_PACKET, and native keys depend on
                    # the active IME. Its editable Value pattern is deterministic.
                    el.handle.set_edit_text(text)
                else:
                    el.handle.type_keys(_literal_keys(text), pause=0.05, with_spaces=True,
                                        vk_packet=self.cfg.get("vk_packet", True))
            else:
                el.handle.set_edit_text(text)
        except Exception as e:
            raise ActionError(f"输入失败: {e}") from e

    def select_all_and_delete(self, el: LocatedElement, ctx: ExecContext) -> None:
        ctx.abort.check()
        try:
            self._focus_window()
            el.handle.type_keys("^a{BACKSPACE}", pause=0.05)
        except Exception as e:
            raise ActionError(f"清空输入失败: {e}") from e

    def keypress(self, el: LocatedElement, keys: str, ctx: ExecContext) -> None:
        ctx.abort.check()
        try:
            self._focus_window()
            el.handle.type_keys(keys, pause=0.03, with_spaces=True)
        except Exception as e:
            raise ActionError(f"键盘操作失败: {e}") from e

    def toggle(self, el: LocatedElement, checked: bool, ctx: ExecContext) -> None:
        """Set the requested state once, then verify it; repeated calls are idempotent."""
        ctx.abort.check()
        try:
            expected = int(checked)
            state = el.handle.get_toggle_state()
            if state == expected:
                return
            if state not in (0, 1):
                raise ActionError("复选框处于不确定状态，不能自动勾选")
            self.click(el, ctx)
            deadline = time.monotonic() + 1
            while el.handle.get_toggle_state() != expected:
                ctx.abort.check()
                if time.monotonic() >= deadline:
                    raise ActionError("勾选后状态未达到预期")
                time.sleep(0.02)
        except (ActionError, AbortRequested):
            raise
        except Exception as e:
            raise ActionError(f"勾选失败: {e}") from e

    def select(self, el: LocatedElement, value: str, ctx: ExecContext) -> None:
        """下拉/列表选择：优先按文本，失败回退按序号，仍失败走键盘选位。

        UIA backend 的经典 ComboBox（WinForms DropDownList 等）没有
        ExpandCollapsePattern，pywinauto 的 ``select`` 直接不可用——这正是
        实机才发现的一类缺口。回退路径经 Win32 消息读出**全部选项文本**
        定位目标序号，再用真实键盘事件选位（触发 SelectedIndexChanged，
        语义与人工操作一致）。
        """
        ctx.abort.check()
        self._focus_window()
        primary_err: Exception | None = None
        try:
            el.handle.select(value)
            return
        except Exception as e:      # 注意：`except as` 名字在块结束即被删除，须另存
            primary_err = e
            try:
                el.handle.select(int(value))
                return
            except Exception:
                pass  # 走键盘回退
        try:
            self._select_fallback(el, value, ctx)
        except (ActionError, AbortRequested):
            raise
        except Exception as e:
            raise ActionError(
                f"选择失败: value={value!r} ({primary_err}; {e})") from e

    def _select_fallback(self, el: LocatedElement, value: str, ctx=None) -> None:
        """Choose a fallback compatible with the actual widget framework."""
        info = getattr(el.handle, "element_info", None)
        if getattr(info, "framework_id", None) == "Qt":
            self._select_qt_popup(el, value, ctx)
            return
        self._select_native_by_keyboard(el, value)

    def _select_qt_popup(self, el: LocatedElement, value: str, ctx=None) -> None:
        """Wait for Qt's asynchronous popup, select its exact UIA item and verify."""
        control = el.handle
        keyboard_target = self._native_top or control
        try:
            class_name = getattr(getattr(control, "element_info", None), "class_name", "")
            if "QML" in class_name or "QQuick" in class_name:
                # QQuick ComboBox advertises ExpandCollapse but its expand action
                # can be a no-op; opening it with a real click exposes delegates.
                self._focus_window()
                control.click_input()
            else:
                control.expand()
            deadline = time.monotonic() + 2
            while True:
                if ctx is not None:
                    ctx.abort.check()
                items = control.descendants(control_type="ListItem")
                if not items and self._top is not None:
                    # Qt Quick places popup delegates under the window overlay,
                    # outside the ComboBox subtree. Restrict candidates to the
                    # visible popup aligned with this combo, within this window.
                    combo_rect = control.rectangle()
                    items = []
                    for candidate in self._top.descendants(control_type="ListItem"):
                        rect = candidate.rectangle()
                        if (candidate.is_visible() and rect.right > rect.left
                                and rect.bottom > rect.top
                                and rect.left < combo_rect.right and rect.right > combo_rect.left
                                and combo_rect.top - 4 <= rect.top <= combo_rect.bottom + 600):
                            items.append(candidate)
                if items:
                    break
                if time.monotonic() >= deadline:
                    raise ActionError("选择失败: Qt 下拉列表未出现")
                time.sleep(0.05)
            if value.isdigit():
                index = int(value)
                if index >= len(items):
                    raise ActionError(f"选择失败: Qt 选项序号 {index} 越界（共 {len(items)} 项）")
                item = items[index]
            else:
                matches = [candidate for candidate in items
                           if candidate.element_info.name == value]
                if len(matches) != 1:
                    raise ActionError(f"选择失败: Qt 选项 {value!r} 不存在或不唯一")
                item = matches[0]
            expected = item.element_info.name
            # Refocusing the combo here closes the popup and restores the old
            # focus (often the sample input). Click the visible option directly.
            item.click_input()
            deadline = time.monotonic() + 1
            while self._combo_text(control) != expected:
                if ctx is not None:
                    ctx.abort.check()
                if time.monotonic() >= deadline:
                    raise ActionError(f"选择失败: Qt 未确认选项 {expected!r}")
                time.sleep(0.02)
        finally:
            keyboard_target.type_keys("{ESC}", pause=0.03, set_foreground=False)

    def _select_native_by_keyboard(self, el: LocatedElement, value: str) -> None:
        """Native ComboBox: read its option list, then use HOME + DOWN."""
        import ctypes
        from ctypes import wintypes

        hwnd = el.handle.handle if hasattr(el.handle, "handle") else el.handle
        user32 = ctypes.windll.user32
        # windll 默认把参数转成 32 位 int，64 位缓冲区地址会溢出——显式声明签名
        user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        wintypes.WPARAM, wintypes.LPARAM]
        CB_GETCOUNT, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x0146, 0x0148, 0x0149

        count = int(user32.SendMessageW(hwnd, CB_GETCOUNT, 0, 0))
        if count <= 0:
            raise ActionError("ComboBox 无选项（CB_GETCOUNT=0），键盘选位不可用")
        options: list[str] = []
        for i in range(count):
            length = int(user32.SendMessageW(hwnd, CB_GETLBTEXTLEN, i, 0))
            if length < 0:      # CB_ERR：项被并发移除等异常形态，别按 -1 分配缓冲
                raise ActionError(f"ComboBox 读取第 {i} 项失败（CB_GETLBTEXTLEN 错误）")
            buf = ctypes.create_unicode_buffer(max(1, length + 1))
            if user32.SendMessageW(hwnd, CB_GETLBTEXT, i,
                                   ctypes.addressof(buf)) < 0:
                raise ActionError(f"ComboBox 读取第 {i} 项失败（CB_GETLBTEXT 错误）")
            options.append(buf.value)

        target: int
        if value.isdigit():
            target = int(value)
            if target >= count:
                raise ActionError(
                    f"选择失败: 序号 {target} 越界（共 {count} 项: {options}）")
        else:
            if value not in options:
                raise ActionError(
                    f"选择失败: 选项 {value!r} 不存在（可选: {options}）")
            target = options.index(value)

        # HOME 到第 0 项，再 DOWN 到目标位——真实键盘事件，SelectedIndexChanged 完整触发
        el.handle.set_focus()
        el.handle.type_keys("{HOME}", pause=0.03)
        for _ in range(target):
            el.handle.type_keys("{DOWN}", pause=0.03)

    @staticmethod
    def _combo_text(control) -> str:
        try:
            value = control.iface_value.CurrentValue
            if value:
                return str(value).strip()
        except Exception:
            pass
        # Qt Quick renders its selected value as an inner read-only Edit, while
        # pywinauto.selected_text() may return the combo's accessible name.
        try:
            for child in control.descendants(control_type="Edit"):
                value = child.iface_value.CurrentValue
                if value:
                    return str(value).strip()
        except Exception:
            pass
        return str(control.selected_text()).strip()

    def read_text(self, el: LocatedElement) -> str:
        """读取控件文本（``assert.ui type=text`` 用）。"""
        try:
            control = el.handle
            kind = getattr(getattr(control, "element_info", None), "control_type", "")
            if kind == "CheckBox":
                return {0: "false", 1: "true", 2: "indeterminate"}[control.get_toggle_state()]
            if kind == "ComboBox":
                return self._combo_text(control)
            # Qt Edit/Spinner/Slider and accessible custom controls expose values
            # through UIA patterns; their window_text may only contain the name.
            if kind in ("Edit", "Spinner", "Slider", "ProgressBar"):
                for pattern in ("iface_value", "iface_range_value"):
                    try:
                        return str(getattr(control, pattern).CurrentValue).strip()
                    except Exception:
                        pass
            text = (control.window_text() or "").strip()
            if not text and kind == "Text":
                # Qt Quick StaticText often has empty rich_text, with its actual
                # bound display text available only as UIA Name.
                return str(control.element_info.name or "").strip()
            return text
        except Exception as e:
            raise ActionError(f"读取控件文本失败: {e}") from e

    def grab(self, rect: Rect | None, ctx: ExecContext) -> Screenshot:
        """UIA 驱动截图：委托 mss（与视觉驱动共用实现）。

        ``rect`` 约定为物理像素（与 UIA 矩形同源），抓取前归一化到截图坐标空间，
        否则非 DPI 感知进程在缩放屏上会截错区域（M3-4）。
        """
        from testpilot.drivers.vision_driver import grab_screen
        return grab_screen(dpi.normalize_rect(rect, "physical"))

    # ---------- 内部 ----------

    def _rect_of(self, wrapper) -> Rect:
        """pywinauto 的 RECT 上 width/height 是方法而非属性，统一用边界相减。

        UIA 返回**物理像素**矩形；引擎把它当作 ``ctx.window_rect`` 的唯一来源，
        由消费方（视觉驱动/截图）按需归一到截图空间。
        """
        dpi.ensure_dpi_aware()
        r = wrapper.rectangle()
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _literal_keys(text: str) -> str:
    return "".join("{" + ch + "}" if ch in "{}+^%~()" else ch for ch in text)


def _build_command(exe: str, args) -> str:
    """exe + 参数拼成命令行。

    ``args`` 可能是字符串（DSL），也可能是列表（探索工具层 schema 声明的是
    array of string）——列表直接 f-string 进命令行会变成 ``app.exe ['--flag', 'x']``。
    用 ``subprocess.list2cmdline`` 按 Windows 规则引用（含空格的路径/参数安全）。
    """
    if not args:
        return subprocess.list2cmdline([exe])
    if isinstance(args, (list, tuple)):
        return subprocess.list2cmdline([exe, *[str(a) for a in args]])
    return f"{subprocess.list2cmdline([exe])} {args}"


def create(cfg: dict) -> UIADriver:
    return UIADriver(cfg)

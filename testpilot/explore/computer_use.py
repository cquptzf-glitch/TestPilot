"""Computer-Use 风格的探索自动化会话。

本模块只服务 Explore/MCP 上层，不改变确定性 DSL Executor 的执行语义。
借鉴 open-computer-use 的几个关键原则：

- observation 与 action 分离；
- 会话内使用短生命周期 element_index，动作前重新校验；
- screenshot-only 观察会主动失效旧索引，避免把旧元素错点到新界面；
- set_value 必须写后回读，不把“API 接受调用”误报成“界面真的更新”；
- AX 树采集有节点/深度预算，避免大型 Qt/QML 界面拖垮 agent 上下文。
"""
from __future__ import annotations

import base64
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from testpilot.core.contracts import DriverKind, ExecContext, LocatedElement
from testpilot.core.errors import ActionError, TestPilotError


class ComputerUseError(TestPilotError):
    """探索期 computer-use 会话错误；属于可恢复的业务错误。"""


@dataclass
class _CachedElement:
    snapshot_id: int
    wrapper: Any
    record: dict


class ComputerUseSession:
    """一个 ToolBox 生命周期内的 UI 观察/元素索引会话。

    element_index 只在最近一次 AX 观察或 query 产生的 snapshot_id 下有效。
    这和回归 DSL 的稳定 locator 是两套东西：索引用于 agent 连续交互，
    locator 用于最终固化为可重复执行的测试用例。
    """

    def __init__(self, drivers: dict, ctx: ExecContext):
        self.drivers = drivers
        self.ctx = ctx
        self._snapshot_id = 0
        self._elements: dict[int, _CachedElement] = {}

    @property
    def snapshot_id(self) -> int:
        return self._snapshot_id

    def reset(self) -> None:
        """回合结束/进程收尾时清掉所有短生命周期元素句柄。"""
        self._snapshot_id += 1
        self._elements.clear()

    def observe(
        self,
        *,
        include_ax: bool = True,
        include_screenshot: bool = False,
        max_nodes: int = 300,
        max_depth: int = 12,
    ) -> dict:
        """观察当前被测窗口。

        include_ax / include_screenshot 至少开启一个。每次观察都创建新的 snapshot，
        包括 screenshot-only；后者会清空旧 AX 索引，防止界面变化后继续使用旧句柄。
        """
        if not include_ax and not include_screenshot:
            raise ComputerUseError("include_ax 与 include_screenshot 不能同时为 false")
        max_nodes = _bounded_int(max_nodes, "max_nodes", 1, 5000)
        max_depth = _bounded_int(max_depth, "max_depth", 0, 64)

        self.ctx.abort.check()
        self._snapshot_id += 1
        self._elements.clear()
        result = {
            "snapshot_id": self._snapshot_id,
            "include_ax": bool(include_ax),
            "include_screenshot": bool(include_screenshot),
        }

        if include_ax:
            result.update(self._observe_ax(max_nodes=max_nodes, max_depth=max_depth))

        if include_screenshot:
            shot = self._grab()
            result["png_base64"] = base64.b64encode(shot.png).decode("ascii")
            result["screenshot_bytes"] = len(shot.png)
            if shot.rect is not None:
                result["screenshot_rect"] = list(shot.rect)

        return result

    def query(
        self,
        *,
        text: str | None = None,
        automation_id: str | None = None,
        control_type: str | None = None,
        exact: bool = False,
        limit: int = 20,
        max_nodes: int = 500,
        max_depth: int = 16,
    ) -> dict:
        """不截图地定向查询当前 UIA 树，并把命中项注册为 element_index。"""
        text = str(text or "").strip()
        automation_id = str(automation_id or "").strip()
        control_type = str(control_type or "").strip()
        if not (text or automation_id or control_type):
            raise ComputerUseError("ui.query 至少需要 text / automation_id / control_type 之一")
        limit = _bounded_int(limit, "limit", 1, 100)

        state = self.observe(
            include_ax=True,
            include_screenshot=False,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
        matches = []
        needle = text.casefold()
        for item in state["elements"]:
            if automation_id and str(item.get("automation_id") or "") != automation_id:
                continue
            if control_type and str(item.get("control_type") or "").casefold()                     != control_type.casefold():
                continue
            if text:
                haystacks = [
                    str(item.get("name") or ""),
                    str(item.get("value") or ""),
                    str(item.get("class_name") or ""),
                ]
                if exact:
                    ok = any(h.casefold() == needle for h in haystacks if h)
                else:
                    ok = any(needle in h.casefold() for h in haystacks if h)
                if not ok:
                    continue
            matches.append(item)
            if len(matches) >= limit:
                break

        return {
            "snapshot_id": state["snapshot_id"],
            "matches": matches,
            "truncated": state["truncated"] or len(matches) >= limit,
            "visited_nodes": len(state["elements"]),
            "limit": limit,
        }

    def resolve(
        self,
        element_index: int,
        *,
        snapshot_id: int | None = None,
    ) -> tuple[LocatedElement, dict]:
        """解析 element_index，并对句柄/身份/可见性做 fail-closed 校验。"""
        self.ctx.abort.check()
        try:
            index = int(element_index)
        except (TypeError, ValueError):
            raise ComputerUseError(f"非法 element_index: {element_index!r}") from None
        cached = self._elements.get(index)
        if cached is None:
            raise ComputerUseError(
                f"element_index={index} 已失效或不存在，请重新 ui.observe/ui.query")
        if snapshot_id is not None and int(snapshot_id) != cached.snapshot_id:
            raise ComputerUseError(
                f"snapshot_id={snapshot_id} 已过期，当前为 {cached.snapshot_id}，"
                "请重新观察后再操作")

        wrapper = cached.wrapper
        exists = getattr(wrapper, "exists", None)
        try:
            if callable(exists) and not exists(timeout=0):
                raise ComputerUseError(
                    f"element_index={index} 对应控件已销毁，请重新观察")
        except ComputerUseError:
            raise
        except Exception as e:
            raise ComputerUseError(
                f"element_index={index} 状态无法确认，请重新观察: {e}") from e

        current = self._describe(wrapper, index=index,
                                 depth=cached.record.get("depth", 0),
                                 parent_index=cached.record.get("parent_index"))
        if not self._same_identity(cached.record, current):
            raise ComputerUseError(
                f"element_index={index} 对应控件身份已变化，请重新观察")

        visible = current.get("visible")
        if visible is False:
            raise ComputerUseError(
                f"element_index={index} 当前不可见，请滚动/切换页面后重新观察")

        rect = tuple(current.get("rect") or ())
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
            raise ComputerUseError(
                f"element_index={index} 没有可操作几何区域，请重新观察")

        return LocatedElement(
            driver=DriverKind.UIA,
            handle=wrapper,
            rect=rect,
            confidence=1.0,
        ), current

    def set_value(
        self,
        el: LocatedElement,
        value: str,
        *,
        timeout_ms: int = 800,
    ) -> dict:
        """直设可编辑值并写后回读；控件忽略写入时明确失败。"""
        self.ctx.abort.check()
        handle = el.handle
        setter = getattr(handle, "set_edit_text", None)
        if not callable(setter):
            raise ActionError("目标控件不支持可访问值写入，请改用 ui.input")

        driver = self._uia()
        read_text = getattr(driver, "read_text", None)
        before = self._read_for_verify(el, read_text)
        if _values_equal(before, value):
            return {"before": before, "actual": before, "verified": True,
                    "changed": False}

        try:
            setter(str(value))
        except Exception as e:
            raise ActionError(f"set_value 写入失败: {e}") from e

        deadline = time.monotonic() + max(0.05, int(timeout_ms) / 1000)
        actual = before
        while True:
            self.ctx.abort.check()
            actual = self._read_for_verify(el, read_text)
            if _values_equal(actual, value):
                return {"before": before, "actual": actual, "verified": True,
                        "changed": not _values_equal(before, actual)}
            if time.monotonic() >= deadline:
                raise ActionError(
                    f"set_value 写入未生效: 期望 {value!r}，控件仍返回 {actual!r}")
            time.sleep(0.05)

    # ---------- AX observation ----------

    def _observe_ax(self, *, max_nodes: int, max_depth: int) -> dict:
        driver = self._uia()
        root = getattr(driver, "observation_root", None)
        if not callable(root):
            raise ComputerUseError("UIA 驱动未实现 ObservationDriver")
        try:
            top = root()
        except Exception as e:
            raise ComputerUseError(f"无法读取可访问性根节点: {e}") from e

        root_rect = self._rect(driver, top)
        queue = deque([(top, 0, None)])
        elements: list[dict] = []
        seen: set[int] = set()
        depth_limited = False

        while queue and len(elements) < max_nodes:
            self.ctx.abort.check()
            wrapper, depth, parent_index = queue.popleft()
            marker = id(wrapper)
            if marker in seen:
                continue
            seen.add(marker)

            index = len(elements) + 1
            record = self._describe(
                wrapper, index=index, depth=depth, parent_index=parent_index)
            elements.append(record)
            self._elements[index] = _CachedElement(
                snapshot_id=self._snapshot_id,
                wrapper=wrapper,
                record=record,
            )

            if depth >= max_depth:
                depth_limited = True
                continue
            children = getattr(wrapper, "children", None)
            if not callable(children):
                continue
            try:
                for child in children() or []:
                    queue.append((child, depth + 1, index))
            except Exception:
                # 单个 provider 的 children 失败不应该让整个快照丢失。
                continue

        _annotate_selector_quality(elements)
        return {
            "window_rect": list(root_rect) if root_rect else None,
            "window_title": _safe_window_text(top),
            "window_handle": int(getattr(top, "handle", 0) or 0),
            "elements": elements,
            "truncated": bool(queue) or depth_limited,
            "max_nodes": max_nodes,
            "max_depth": max_depth,
        }

    def _describe(self, wrapper, *, index: int, depth: int,
                  parent_index: int | None) -> dict:
        info = getattr(wrapper, "element_info", None)
        record = {
            "element_index": int(index),
            "depth": int(depth),
            "parent_index": parent_index,
            "name": _safe_attr(info, "name"),
            "automation_id": _safe_attr(info, "automation_id"),
            "control_type": _safe_attr(info, "control_type"),
            "class_name": _safe_attr(info, "class_name"),
            "framework_id": _safe_attr(info, "framework_id"),
            "value": self._safe_value(wrapper),
            "rect": list(self._rect(self._uia(), wrapper) or (0, 0, 0, 0)),
            "visible": _safe_call_bool(wrapper, "is_visible"),
            "enabled": _safe_call_bool(wrapper, "is_enabled"),
        }
        record["actions"] = _actions_for(record, wrapper)
        record["selector"] = _selector_for(record)
        record["stable_key"] = _stable_key(record)
        return record

    @staticmethod
    def _same_identity(old: dict, new: dict) -> bool:
        # AutomationId 最稳定；没有时退到 control/class/name 组合。
        old_id = str(old.get("automation_id") or "")
        new_id = str(new.get("automation_id") or "")
        if old_id or new_id:
            return old_id == new_id and                 str(old.get("control_type") or "") == str(new.get("control_type") or "")
        for key in ("control_type", "class_name", "name"):
            left = str(old.get(key) or "")
            right = str(new.get(key) or "")
            if left and right and left != right:
                return False
        return True

    def _safe_value(self, wrapper) -> str:
        for attr in ("iface_value", "iface_range_value"):
            try:
                value = getattr(wrapper, attr).CurrentValue
                if value is not None:
                    return str(value)
            except Exception:
                pass
        return ""

    def _read_for_verify(self, el: LocatedElement, read_text) -> str:
        if callable(read_text):
            try:
                return str(read_text(el))
            except Exception:
                pass
        value = self._safe_value(el.handle)
        if value:
            return value
        try:
            return str(el.handle.window_text() or "").strip()
        except Exception:
            return ""

    def _grab(self):
        for name in ("vision", "uia"):
            drv = self.drivers.get(name)
            if drv is None:
                continue
            caps = getattr(getattr(drv, "manifest", None),
                           "capabilities", frozenset())
            if "grab" not in caps:
                continue
            try:
                return drv.grab(self.ctx.window_rect, self.ctx)
            except Exception:
                continue
        raise ComputerUseError("没有可用截图驱动")

    def _uia(self):
        drv = self.drivers.get("uia")
        if drv is None:
            raise ComputerUseError("UIA 驱动未装配")
        return drv

    @staticmethod
    def _rect(driver, wrapper):
        helper = getattr(driver, "observation_rect", None)
        try:
            if callable(helper):
                return tuple(int(v) for v in helper(wrapper))
            r = wrapper.rectangle()
            return (int(r.left), int(r.top),
                    int(r.right - r.left), int(r.bottom - r.top))
        except Exception:
            return None


def _safe_attr(obj, name: str) -> str:
    try:
        value = getattr(obj, name, "")
        return "" if value is None else str(value)
    except Exception:
        return ""


def _safe_call_bool(obj, name: str) -> bool | None:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return bool(fn())
    except Exception:
        return None


def _actions_for(record: dict, wrapper) -> list[str]:
    """给 Agent 的轻量操作建议；最终仍以 DriverManifest capability 为门禁。"""
    actions = []
    control_type = str(record.get("control_type") or "")
    clickable = callable(getattr(wrapper, "click_input", None))
    if clickable:
        actions.extend(["click", "right_click"])
    if callable(getattr(wrapper, "set_edit_text", None)) or control_type in (
            "Edit", "Document", "Spinner"):
        actions.extend(["input", "set_value"])
    if callable(getattr(wrapper, "select", None)) or control_type in (
            "ComboBox", "ListItem", "TabItem"):
        actions.append("select")
    if control_type == "CheckBox" or callable(getattr(wrapper, "get_toggle_state", None)):
        actions.append("toggle")
    if control_type in (
            "List", "Tree", "Table", "DataGrid", "Pane", "Document", "ScrollBar"):
        actions.append("scroll")
    if control_type in ("Slider", "ScrollBar", "ListItem", "TreeItem"):
        actions.append("drag")
    return list(dict.fromkeys(actions))


def _bounded_int(value, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ComputerUseError(f"{name} 必须是整数") from None
    if not low <= number <= high:
        raise ComputerUseError(f"{name} 必须在 {low}..{high} 之间")
    return number


def _values_equal(actual: str, expected: str) -> bool:
    left = str(actual or "").strip()
    right = str(expected or "").strip()
    if left == right:
        return True
    bools = {"true": True, "1": True, "false": False, "0": False}
    if left.casefold() in bools and right.casefold() in bools:
        return bools[left.casefold()] == bools[right.casefold()]
    try:
        a = float(left)
        b = float(right)
        return math.isfinite(a) and math.isfinite(b) and a == b
    except (TypeError, ValueError):
        return False


def _selector_for(record: dict) -> dict | None:
    """从观察记录生成可持久化的首选 locator 候选。

    AutomationId 优先；没有时退到 accessibleName + control_type。
    只有 control_type 不足以稳定定位，因此返回 None。
    """
    automation_id = str(record.get("automation_id") or "").strip()
    control_type = str(record.get("control_type") or "").strip()
    name = str(record.get("name") or "").strip()
    if automation_id:
        out = {"type": "uia", "automation_id": automation_id}
        if control_type:
            out["control_type"] = control_type
        return out
    if name:
        out = {"type": "uia", "name": name}
        if control_type:
            out["control_type"] = control_type
        return out
    return None


def _stable_key(record: dict) -> str:
    """生成探索期语义键候选；真正写入正式 DSL 前仍需人审。"""
    automation_id = str(record.get("automation_id") or "").strip()
    control_type = str(record.get("control_type") or "element").strip().lower()
    name = str(record.get("name") or "").strip()
    raw = automation_id or name or f"element_{record.get('element_index', 0)}"
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    cleaned = cleaned.strip("._-") or f"element_{record.get('element_index', 0)}"
    return f"{control_type}.{cleaned}"[:120]


def _safe_window_text(wrapper) -> str:
    try:
        return str(wrapper.window_text() or "").strip()
    except Exception:
        return ""



def _annotate_selector_quality(elements: list[dict]) -> None:
    """给观察结果打 selector 稳定度分，并为重复控件补父级作用域链。

    评分只用于探索期提示，不是执行门禁。重复控件优先通过 ancestors 缩小作用域；
    同一父级下仍重复时再写 index，尽量把脆弱的全局 found_index 降到局部范围。
    """
    by_index = {int(item["element_index"]): item for item in elements}
    auto_counts: dict[tuple[str, str], int] = {}
    name_counts: dict[tuple[str, str], int] = {}
    for item in elements:
        control = str(item.get("control_type") or "").strip()
        auto = str(item.get("automation_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if auto:
            auto_counts[(auto, control)] = auto_counts.get((auto, control), 0) + 1
        if name:
            name_counts[(name, control)] = name_counts.get((name, control), 0) + 1

    def ancestors_of(item: dict) -> list[dict[str, str]]:
        chain: list[dict[str, str]] = []
        parent = item.get("parent_index")
        while parent is not None:
            node = by_index.get(int(parent))
            if node is None:
                break
            # 顶层主窗口已经是 UIADriver.locate() 的起始 scope，不能再作为
            # ancestor 写回，否则执行时会变成“在主窗口里再找主窗口”。
            if node.get("parent_index") is not None:
                segment = _selector_segment(node)
                if segment:
                    chain.append(segment)
            parent = node.get("parent_index")
        chain.reverse()
        return chain

    # 对重复 selector 先判断父级链是否能区分。
    path_signatures: dict[int, tuple] = {}
    for item in elements:
        selector = item.get("selector")
        if not isinstance(selector, dict):
            continue
        chain = ancestors_of(item)
        path_signatures[int(item["element_index"])] = (
            tuple(tuple(sorted(segment.items())) for segment in chain),
            tuple(sorted(selector.items())),
        )

    signature_counts: dict[tuple, int] = {}
    for signature in path_signatures.values():
        signature_counts[signature] = signature_counts.get(signature, 0) + 1

    sibling_groups: dict[tuple, list[dict]] = {}
    for item in elements:
        selector = item.get("selector")
        if not isinstance(selector, dict):
            continue
        group_key = (
            item.get("parent_index"),
            tuple(sorted(selector.items())),
        )
        sibling_groups.setdefault(group_key, []).append(item)

    for item in elements:
        control = str(item.get("control_type") or "").strip()
        auto = str(item.get("automation_id") or "").strip()
        name = str(item.get("name") or "").strip()
        selector = item.get("selector")
        score = 0.2
        reason = "缺少 AutomationId / accessibleName"

        duplicate = False
        if auto:
            duplicate = auto_counts.get((auto, control), 0) > 1
            score = 0.98 if not duplicate else 0.76
            reason = "AutomationId 唯一" if not duplicate else "AutomationId 重复"
        elif name:
            duplicate = name_counts.get((name, control), 0) > 1
            score = 0.82 if not duplicate else 0.56
            reason = "Name + ControlType 唯一" if not duplicate else "Name + ControlType 重复"

        if duplicate and isinstance(selector, dict):
            chain = ancestors_of(item)
            signature = path_signatures.get(int(item["element_index"]))
            path_unique = bool(chain and signature and signature_counts.get(signature, 0) == 1)
            if chain:
                selector["ancestors"] = chain
            if path_unique:
                score = 0.92 if auto else 0.86
                reason += "；父级作用域链可唯一定位"
            else:
                group_key = (item.get("parent_index"),
                             tuple(sorted({k: v for k, v in selector.items()
                                           if k not in ("ancestors", "index")}.items())))
                siblings = sibling_groups.get(group_key, [])
                if len(siblings) > 1:
                    local_index = siblings.index(item)
                    if local_index:
                        selector["index"] = local_index
                    score = 0.68 if chain else 0.58
                    reason += "；同父级仍重复，使用局部 index"
                elif chain:
                    score = 0.72
                    reason += "；已增加父级作用域链"

        if score >= 0.85:
            quality = "high"
        elif score >= 0.65:
            quality = "medium"
        else:
            quality = "low"
        item["selector_score"] = round(score, 2)
        item["selector_quality"] = quality
        item["selector_reason"] = reason


def _selector_segment(record: dict) -> dict[str, str] | None:
    """生成父级作用域段；与最终 target selector 使用同一优先级。"""
    automation_id = str(record.get("automation_id") or "").strip()
    control_type = str(record.get("control_type") or "").strip()
    name = str(record.get("name") or "").strip()
    if automation_id:
        out = {"automation_id": automation_id}
        if control_type:
            out["control_type"] = control_type
        return out
    if name:
        out = {"name": name}
        if control_type:
            out["control_type"] = control_type
        return out
    return None


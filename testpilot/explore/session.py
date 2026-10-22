"""探索会话 — 记录工具调用轨迹并固化回 DSL 草稿（设计 11.1）。

探索负责发现，DSL 引擎负责回归。element_index 等临时句柄绝不写入正式 DSL；
固化时优先消费工具结果里的稳定 selector 候选。
"""
import time
from dataclasses import dataclass

from testpilot.core.errors import TestPilotError
from testpilot.explore.toolbox import ToolBox, ToolError

# 可映射为 DSL 步骤的工具（观察/读取类不进入用例）
DSL_TOOLS = (
    "app.launch", "app.attach", "app.wait_ready", "app.close",
    "ui.locate", "ui.click", "ui.input", "ui.set_value", "ui.select",
    "ui.toggle", "ui.keypress", "assert.ui", "assert.state", "assert.vision",
    "device.scene", "device.call",
)


@dataclass
class ExploreStep:
    index: int
    tool: str
    args: dict
    ok: bool
    ms: int = 0
    driver: str | None = None
    result: dict | None = None
    error: str | None = None
    rerun_of: int | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "tool": self.tool, "args": self.args,
                "ok": self.ok, "ms": self.ms, "driver": self.driver,
                "result": self.result, "error": self.error,
                "rerun_of": self.rerun_of}

    @classmethod
    def from_dict(cls, data: dict) -> "ExploreStep":
        return cls(index=int(data.get("index", 0)),
                   tool=str(data.get("tool", "")),
                   args=dict(data.get("args") or {}),
                   ok=bool(data.get("ok", False)),
                   ms=int(data.get("ms") or 0),
                   driver=data.get("driver"),
                   result=data.get("result"),
                   error=data.get("error"),
                   rerun_of=data.get("rerun_of"))


class ExplorationSession:
    """一次探索会话：调用即留痕；成功后固化为待审 DSL 草稿。"""

    def __init__(self, toolbox: ToolBox | None = None, *,
                 case_id: str = "TC-EXPLORE", title: str = "探索路径",
                 zentao_case_id: int | None = None,
                 preconditions: list[str] | None = None,
                 notes: str = ""):
        self.toolbox = toolbox
        self.case_id = case_id
        self.title = title
        self.zentao_case_id = zentao_case_id
        self.preconditions = list(preconditions or [])
        self.notes = notes
        self.steps: list[ExploreStep] = []

    def call(self, tool: str, args: dict | None = None, *,
             rerun_of: int | None = None) -> dict:
        """调用一个工具并记录；失败同样留痕后抛出，供 agent 重规划。"""
        if self.toolbox is None:
            raise ToolError("会话未装配工具箱，只能用于离线转换")
        args = dict(args or {})
        t0 = time.monotonic()
        try:
            out = self.toolbox.invoke(tool, args)
        except TestPilotError as e:
            self.steps.append(ExploreStep(
                index=len(self.steps) + 1, tool=tool, args=args, ok=False,
                ms=int((time.monotonic() - t0) * 1000),
                driver=args.get("driver"), error=str(e), rerun_of=rerun_of))
            raise
        self.steps.append(ExploreStep(
            index=len(self.steps) + 1, tool=tool, args=args, ok=True,
            ms=int((time.monotonic() - t0) * 1000),
            driver=args.get("driver") or (out or {}).get("driver"),
            result=_history_result(tool, out), rerun_of=rerun_of))
        return out

    def reset(self) -> None:
        self.steps.clear()
        if self.toolbox is not None:
            self.toolbox.reset_session()

    def remove_step(self, index: int) -> ExploreStep:
        """删除一条探索历史并重排索引；用于误操作撤销，不影响当前 UI 快照。"""
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            raise ToolError(f"非法探索步骤序号: {index!r}") from None
        pos = next((i for i, step in enumerate(self.steps) if step.index == wanted), -1)
        if pos < 0:
            raise ToolError(f"探索步骤不存在: {wanted}")
        removed = self.steps.pop(pos)
        for i, step in enumerate(self.steps, start=1):
            step.index = i
        return removed

    def update_step(self, index: int, args: dict) -> ExploreStep:
        """只允许编辑探索步骤参数；tool/result 仍保留原执行证据。

        编辑后的参数用于 DSL 固化，result 用于保留当时实际执行证据。这样既允许
        修正文案/期望值/稳定 key，又不会把“未执行过的新工具”伪装成已执行。
        """
        step = self._get_step(index)
        if not isinstance(args, dict):
            raise ToolError("探索步骤 args 必须是对象")
        step.args = dict(args)
        return step

    def rerun_step(self, index: int, args: dict | None = None) -> dict:
        """按编辑后的参数重新执行，并保留旧步骤作为证据。

        element_index/snapshot_id 是短生命周期句柄；如果旧步骤已有稳定 target.selector，
        重跑时自动把临时句柄替换成该 selector，避免恢复/页面变化后误用旧 wrapper。
        """
        original = self._get_step(index)
        effective = dict(original.args if args is None else args)
        result = original.result if isinstance(original.result, dict) else {}
        target = result.get("target") if isinstance(result.get("target"), dict) else {}
        selector = target.get("selector") if isinstance(target.get("selector"), dict) else None

        if effective.get("element_index") is not None and selector:
            effective.pop("element_index", None)
            effective.pop("snapshot_id", None)
            for key, value in selector.items():
                effective.setdefault(key, value)
            stable_key = target.get("stable_key")
            if stable_key and not effective.get("key"):
                effective["key"] = stable_key

        return self.call(original.tool, effective, rerun_of=original.index)

    def move_step(self, index: int, to_index: int) -> list[ExploreStep]:
        """调整探索历史顺序并重排 1-based index。"""
        step = self._get_step(index)
        try:
            target = int(to_index)
        except (TypeError, ValueError):
            raise ToolError(f"非法目标序号: {to_index!r}") from None
        if not self.steps:
            return []
        target = max(1, min(target, len(self.steps)))
        self.steps.remove(step)
        self.steps.insert(target - 1, step)
        for i, item in enumerate(self.steps, start=1):
            item.index = i
        return list(self.steps)

    def _get_step(self, index: int) -> ExploreStep:
        try:
            wanted = int(index)
        except (TypeError, ValueError):
            raise ToolError(f"非法探索步骤序号: {index!r}") from None
        step = next((item for item in self.steps if item.index == wanted), None)
        if step is None:
            raise ToolError(f"探索步骤不存在: {wanted}")
        return step

    def to_case(self) -> dict:
        """转为 DSL 用例草稿；只有成功且可稳定映射的动作进入用例。"""
        steps = []
        skipped = 0
        for step in self.steps:
            if not step.ok:
                skipped += 1
                continue
            dsl = _to_dsl_step(step)
            if dsl is None:
                skipped += 1
                continue
            dsl["id"] = f"s{len(steps) + 1}"
            steps.append(dsl)

        notes = self.notes or ""
        if self.steps:
            summary = (
                f"由探索会话固化：工具调用 {len(self.steps)} 步，"
                f"可映射 {len(steps)} 步，跳过 {skipped} 步"
                "（失败/观测/无法生成稳定定位的步骤不进入用例）。需人审确认后生效。"
            )
            notes = f"{notes} {summary}".strip()
        return {
            "case_id": self.case_id,
            "zentao_case_id": self.zentao_case_id,
            "title": self.title,
            "preconditions": list(self.preconditions),
            "notes": notes,
            "steps": steps,
        }

    def to_draft_text(self, fmt: str = "json") -> str:
        from testpilot.case_parser import dumps
        return dumps(self.to_case(), fmt)

    def import_draft(self, case_service) -> dict:
        return case_service.import_draft(self.to_draft_text("json"), "json")

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "title": self.title,
                "zentao_case_id": self.zentao_case_id,
                "preconditions": list(self.preconditions),
                "notes": self.notes,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict, toolbox: ToolBox | None = None) -> "ExplorationSession":
        session = cls(toolbox, case_id=str(data.get("case_id") or "TC-EXPLORE"),
                      title=str(data.get("title") or "探索路径"),
                      zentao_case_id=data.get("zentao_case_id"),
                      preconditions=list(data.get("preconditions") or []),
                      notes=str(data.get("notes") or ""))
        session.steps = [ExploreStep.from_dict(s)
                         for s in (data.get("steps") or []) if isinstance(s, dict)]
        for i, step in enumerate(session.steps, start=1):
            step.index = i
        return session


def _to_dsl_step(step: ExploreStep) -> dict | None:
    args = step.args or {}
    result = step.result or {}
    tool = step.tool

    if tool in ("app.launch", "app.attach"):
        spec = {}
        if tool == "app.launch" and args.get("exe"):
            spec["exe"] = args["exe"]
        if args.get("args"):
            spec["args"] = list(args["args"])
        if args.get("title_re"):
            spec["title_re"] = args["title_re"]
        if not spec.get("exe") and not spec.get("title_re"):
            return None
        verb = "启动" if spec.get("exe") else "附加"
        label = spec.get("exe") or spec.get("title_re")
        return {"action": "app.launch", "args": spec,
                "source_text": f"[探索] {verb} {label}"}

    if tool == "app.wait_ready":
        return {"action": "app.wait_ready",
                "timeout_ms": int(args.get("timeout_ms", 10000)),
                "source_text": "[探索] 等待主窗口就绪"}

    if tool == "app.close":
        return {"action": "app.close",
                "args": {"mode": args.get("mode", "graceful")},
                "source_text": "[探索] 关闭被测程序"}

    if tool in ("ui.locate", "ui.click", "ui.input", "ui.set_value",
                "ui.select", "ui.toggle", "ui.keypress"):
        target = _target_of(args, result)
        if target is None:
            return None
        action = {
            "ui.locate": "ui.wait",
            "ui.click": "ui.click",
            "ui.input": "ui.input",
            "ui.set_value": "ui.input",
            "ui.select": "ui.select",
            "ui.toggle": "ui.toggle",
            "ui.keypress": "ui.keypress",
        }[tool]
        driver_name = str(result.get("driver") or args.get("driver") or "")
        if tool == "ui.click" and driver_name == "vision":
            action = "vision.click"
        out = {"action": action, "target": target,
               "source_text": f"[探索] {_describe(tool, args, result)}"}
        if driver_name == "vision":
            out["driver"] = "vision_only"
        if tool == "ui.locate":
            out["args"] = {"state": "exists"}
        elif tool == "ui.input":
            out["value"] = str(args.get("text") or "")
            if args.get("clear"):
                out["args"] = {"clear": True}
        elif tool == "ui.set_value":
            # 正式 DSL 暂无独立 set_value 动作：等价固化为“清空后输入”，
            # 保持可重复性且不把 Explore-only API 泄漏到 Executor。
            out["value"] = str(args.get("value") or "")
            out["args"] = {"clear": True}
        elif tool == "ui.select":
            out["value"] = str(args.get("value") or "")
        elif tool == "ui.toggle":
            out["value"] = "true" if bool(args.get("checked")) else "false"
        elif tool == "ui.keypress":
            out["value"] = str(args.get("keys") or "")

        # 只有探索现场的 before/after 真正验证通过，才把 screenshot_diff
        # 写进正式 DSL；不能只信调用方传入 verify_diff=true。
        if result.get("diff_verified") is True:
            threshold = result.get("diff_threshold", 0.005)
            out["verify"] = {
                "type": "screenshot_diff",
                "expect": str(threshold),
                "timeout_ms": int(args.get("timeout_ms", 5000)),
            }
        return out

    if tool == "assert.ui":
        target = _target_of(args, result)
        if target is None:
            return None
        expect = str(args.get("expect") or "")
        if not expect:
            return None
        return {
            "action": "assert.ui",
            "target": target,
            "verify": {
                "type": "text",
                "expect": expect,
                "op": str(args.get("op") or "equals"),
                "timeout_ms": int(args.get("timeout_ms", 5000)),
            },
            "source_text": f"[探索] 断言 {_describe(tool, args, result)}",
        }

    if tool == "assert.state":
        path = str(args.get("path") or "")
        if not path or "expect" not in args:
            return None
        expect = args.get("expect")
        return {
            "action": "assert.state",
            "args": {"path": path, "expect": expect},
            "verify": {
                "type": "state",
                "expect": expect,
                "op": str(args.get("op") or "equals"),
                "timeout_ms": int(args.get("timeout_ms", 5000)),
            },
            "source_text": (
                f"[探索] 断言设备状态 {path} "
                f"{args.get('op', 'equals')} {expect!r}"
            ),
        }

    if tool == "assert.vision":
        expect = str(args.get("expect") or "")
        if not expect:
            return None
        return {
            "action": "assert.vision",
            "verify": {
                "type": "vision",
                "expect": expect,
                "timeout_ms": int(args.get("timeout_ms", 5000)),
            },
            "source_text": f"[探索] 视觉断言 {expect!r}",
        }

    if tool == "device.scene":
        return {"action": "device.scene",
                "args": {"scene_id": args.get("scene_id")},
                "source_text": f"[探索] 加载模拟器场景 {args.get('scene_id')}"}

    if tool == "device.call":
        return {"action": "device.call",
                "args": _drop_none({"cmd": args.get("cmd"),
                                    "args": args.get("args")}),
                "source_text": f"[探索] 模拟器指令 {args.get('cmd')}"}

    return None


def _target_of(args: dict, result: dict | None = None) -> dict | None:
    """把临时 element_index 还原为稳定 TargetDesc，不把会话句柄写入 DSL。"""
    result = result or {}
    meta = result.get("target") if isinstance(result.get("target"), dict) else {}
    selector = meta.get("selector") if isinstance(meta.get("selector"), dict) else None

    key = str(args.get("key") or meta.get("stable_key") or "").strip()
    if not key:
        key = _key_from_meta(meta)

    if selector:
        primary = dict(selector)
        driver = result.get("driver") or args.get("driver") or "uia"
    else:
        driver = args.get("driver")
        if not driver:
            driver = "uia" if args.get("name") or args.get("automation_id")                 or args.get("control_type") else "vision"
        if driver == "uia":
            primary = _drop_none({
                "type": "uia",
                "name": args.get("name"),
                "automation_id": args.get("automation_id"),
                "control_type": args.get("control_type"),
            })
            if len(primary) == 1:
                return None
        elif args.get("template"):
            primary = {"type": "template", "template": args["template"]}
        elif args.get("desc"):
            primary = {"type": "visual", "desc": args["desc"]}
        else:
            return None

    fallback = None
    if driver == "uia":
        if args.get("desc"):
            fallback = {"type": "visual", "desc": args["desc"]}
        elif args.get("template"):
            fallback = {"type": "template", "template": args["template"]}

    target = {"key": key or "explore.target", "primary": primary}
    if fallback:
        target["fallback"] = fallback
    return target


def _key_from_meta(meta: dict) -> str:
    auto_id = str(meta.get("automation_id") or "").strip()
    name = str(meta.get("name") or "").strip()
    control = str(meta.get("control_type") or "element").strip().lower()
    raw = auto_id or name
    if not raw:
        return "explore.target"
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return f"{control}.{cleaned.strip('._-') or 'target'}"[:120]


def _describe(tool: str, args: dict, result: dict | None = None) -> str:
    meta = (result or {}).get("target") or {}
    key = (args.get("key") or meta.get("stable_key") or
           meta.get("name") or args.get("name") or args.get("desc") or "target")
    if tool in ("ui.input", "ui.set_value"):
        value = args.get("text") if tool == "ui.input" else args.get("value")
        return f"在 {key} 输入 {value!r}"
    if tool == "ui.select":
        return f"在 {key} 选择 {args.get('value')!r}"
    if tool == "ui.toggle":
        return f"设置 {key} 为 {'选中' if args.get('checked') else '未选中'}"
    if tool == "ui.keypress":
        return f"在 {key} 发送按键 {args.get('keys')!r}"
    if tool == "ui.locate":
        return f"等待 {key} 出现"
    if tool == "assert.ui":
        return f"{key} {args.get('op', 'equals')} {args.get('expect')!r}"
    return f"点击 {key}"


def _drop_none(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}



def _history_result(tool: str, result: dict | None) -> dict | None:
    """压缩纯观察类工具的历史证据，避免截图/UI 树把会话 JSON 撑爆。

    工具调用的实时返回保持完整；这里只影响 ExplorationSession.steps 中保存的副本。
    会进入 DSL 的动作结果必须保留 target/selector，因此不做裁剪。
    """
    if not isinstance(result, dict):
        return result
    if tool == "ui.observe":
        keep = (
            "snapshot_id", "window_rect", "window_title", "window_handle",
            "truncated", "max_nodes", "max_depth", "screenshot_bytes",
            "screenshot_rect",
        )
        out = {key: result[key] for key in keep if key in result}
        out["element_count"] = len(result.get("elements") or [])
        return out
    if tool == "ui.query":
        keep = ("snapshot_id", "truncated", "visited_nodes", "limit")
        out = {key: result[key] for key in keep if key in result}
        out["match_count"] = len(result.get("matches") or [])
        return out
    if tool == "app.list":
        return {"count": int(result.get("count") or len(result.get("windows") or []))}
    if tool == "screen.grab":
        return {key: result[key] for key in ("bytes", "rect") if key in result}
    return dict(result)

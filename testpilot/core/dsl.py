"""DSL 数据模型 — 全平台唯一执行语言（设计文档 7.1，v1.3 起 Step 带 source_text）。"""
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DriverMode(str, Enum):
    AUTO = "auto"
    UIA_FIRST = "uia_first"
    VISION_FIRST = "vision_first"
    UIA_ONLY = "uia_only"
    VISION_ONLY = "vision_only"


class LocatorType(str, Enum):
    UIA = "uia"            # UIA 对象定位
    TEMPLATE = "template"  # OpenCV 模板匹配
    VISUAL = "visual"      # VLM 语义定位


class Locator(BaseModel):
    type: LocatorType
    name: str | None = None            # accessibleName / 控件文本
    automation_id: str | None = None   # UIA AutomationId
    control_type: str | None = None    # Button/Edit/Table...
    desc: str | None = None            # 语义描述（visual）
    template: str | None = None        # 模板图相对路径（template）
    region: tuple[int, int, int, int] | None = None  # 限定搜索区域 x,y,w,h
    ancestors: list[dict[str, str]] = Field(default_factory=list)
    # UIA 父级作用域链（根 -> 最近父级），每段支持 name/automation_id/control_type。
    # 用于同名/重复控件稳定定位；视觉 locator 忽略此字段。
    index: int = Field(default=0, ge=0)  # 当前作用域内多匹配时取第几个
    row: int | None = Field(default=None, ge=0)  # 表格行（0 起）
    column: int | None = Field(default=None, ge=0)  # 表格列（0 起）


class TargetDesc(BaseModel):
    key: str                           # 稳定语义键（元素库/router_stats 索引）
    primary: Locator
    fallback: Locator | None = None
    vision_only_hint: bool = False


class VerifySpec(BaseModel):
    type: Literal["text", "state", "vision", "screenshot_diff", "none"] = "none"
    target: Locator | None = None
    expect: str | None = None
    op: Literal["contains", "equals", "regex"] = "contains"
    timeout_ms: int = 5000


class OnFailure(str, Enum):
    ABORT = "abort"        # 终止本用例（默认）
    CONTINUE = "continue"  # 记录失败继续后续步骤
    RETRY = "retry"        # 原计划重试一次


class Step(BaseModel):
    id: str
    action: str
    source_text: str | None = None   # 来源禅道用例的原文步骤说明（轨迹对照用）
    target: TargetDesc | None = None
    value: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    verify: VerifySpec = Field(default_factory=VerifySpec)
    driver: DriverMode | None = None   # 步骤级覆盖
    on_failure: OnFailure = OnFailure.ABORT
    timeout_ms: int = 10000
    uncertain: bool = False            # LLM 解析不确定标记（人审高亮，设计 14.3）


class TestCase(BaseModel):
    case_id: str                        # TC-xxx
    zentao_case_id: int | None = None
    title: str = ""
    version: int = 1
    preconditions: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    notes: str = ""                     # 解析备注（不确定项汇总，人审参考）


KNOWN_ACTIONS = frozenset({
    "app.launch", "app.close", "app.wait_ready", "ui.click", "ui.input",
    "ui.select", "ui.toggle", "ui.keypress", "ui.right_click",
    "ui.scroll", "ui.drag", "ui.wait",
    "vision.click", "device.call", "device.scene",
    "assert.ui", "assert.state", "assert.vision", "sleep",
})


def execution_errors(step: Step) -> list[str]:
    """Semantic checks shared by approval and execution; drafts remain editable."""
    errors = []
    if step.action not in KNOWN_ACTIONS:
        errors.append(f"未知动作: {step.action}")
    if step.timeout_ms <= 0 or step.verify.timeout_ms <= 0:
        errors.append("超时必须大于 0")
    expected = {"assert.ui": "text", "assert.state": "state",
                "assert.vision": "vision"}.get(step.action)
    if expected and step.verify.type != expected:
        errors.append(f"{step.action} 必须配置 verify.type={expected}")
    if step.verify.type in ("text", "vision") and not step.verify.expect:
        errors.append("验证缺少 verify.expect")
    if step.verify.type == "state":
        if step.args.get("expect", step.verify.expect) is None:
            errors.append("状态验证缺少期望值")
        if not step.args.get("path"):
            errors.append("状态验证缺少 args.path")
    if step.action.startswith("ui.") or step.action == "vision.click":
        if step.target is None:
            errors.append("交互动作缺少 target")
    if step.action == "app.launch":
        if not step.args.get("exe") and not step.args.get("title_re"):
            errors.append("app.launch 至少需要 args.exe 或 args.title_re")
        launch_args = step.args.get("args")
        if launch_args is not None and (
                not isinstance(launch_args, list)
                or any(not isinstance(item, str) for item in launch_args)):
            errors.append("app.launch args.args 必须是字符串数组")
    if step.action == "app.close":
        mode = str(step.args.get("mode", "graceful"))
        if mode not in ("graceful", "force", "kill"):
            errors.append("app.close args.mode 必须是 graceful 或 force")
    if step.action == "ui.toggle" and step.value not in ("true", "false"):
        errors.append("ui.toggle 的 value 必须是 true 或 false")
    if step.action == "ui.keypress" and not step.value:
        errors.append("ui.keypress 缺少 value 键序列")
    if step.action == "ui.wait":
        state = str(step.args.get("state", "exists")).lower()
        if state not in ("exists", "gone"):
            errors.append("ui.wait args.state 必须是 exists 或 gone")
    if step.action == "ui.scroll":
        direction = str(step.args.get("direction") or "").lower()
        if direction not in ("up", "down", "left", "right"):
            errors.append("ui.scroll args.direction 必须是 up/down/left/right")
        try:
            amount = int(step.args.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0
        if not 1 <= amount <= 20:
            errors.append("ui.scroll args.amount 必须在 1..20")
    if step.action == "ui.drag":
        try:
            dx = int(step.args.get("dx", 0))
            dy = int(step.args.get("dy", 0))
            duration_ms = int(step.args.get("duration_ms", 300))
        except (TypeError, ValueError):
            dx = dy = 0
            duration_ms = -1
        if dx == 0 and dy == 0:
            errors.append("ui.drag 的 dx/dy 不能同时为 0")
        if not 0 <= duration_ms <= 10000:
            errors.append("ui.drag duration_ms 必须在 0..10000")
    for loc in (step.target.primary if step.target else None, step.verify.target):
        if loc is not None and (loc.row is None) != (loc.column is None):
            errors.append("表格定位必须同时提供 row 和 column")
    if step.action == "device.call" and not step.args.get("cmd"):
        errors.append("device.call 缺少 args.cmd")
    if step.action == "device.scene" and not step.args.get("scene_id"):
        errors.append("device.scene 缺少 args.scene_id")
    if step.action == "sleep":
        raw_ms = step.args.get("ms", step.timeout_ms)
        try:
            wait_ms = int(raw_ms)
        except (TypeError, ValueError):
            errors.append("sleep args.ms 必须是整数毫秒")
        else:
            if wait_ms < 0:
                errors.append("等待时间不能为负数")
    return errors


def validate_execution(case: TestCase) -> None:
    errors = []
    ids = set()
    for step in case.steps:
        if step.id in ids:
            errors.append(f"重复步骤 ID: {step.id}")
        ids.add(step.id)
        errors.extend(f"{step.id}: {error}" for error in execution_errors(step))
    if not case.steps:
        errors.append("用例没有步骤")
    if errors:
        raise ValueError("；".join(errors))

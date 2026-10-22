"""失败签名 — 知识库快速定位的索引键（设计 8.8）。
设计约束：签名必须稳定（同一失败跨版本可命中）且可读（人审时看得懂）。
"""
import hashlib
import json

from testpilot.core.contracts import ExecContext, StepResult


class FailureSignature:
    __slots__ = ("error_type", "action", "target_key", "page_key", "context_key")

    def __init__(self, error_type: str, action: str, target_key: str, page_key: str,
                 context_key: str = ""):
        self.context_key = context_key
        self.error_type = error_type   # locate_timeout|verify_failed|action_failed|device_error|app_error
        self.action = action           # ui.click ...
        self.target_key = target_key   # btn.start_detect
        self.page_key = page_key       # 主窗口/设置页...

    def digest(self) -> str:
        raw = "|".join((self.error_type, self.action, self.target_key, self.page_key))
        if self.context_key:
            raw += "|" + self.context_key
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def __str__(self) -> str:
        return f"{self.error_type}|{self.action}|{self.target_key}@{self.page_key}"

    @classmethod
    def from_result(cls, res: StepResult, ctx: ExecContext) -> "FailureSignature":
        err = res.error or {}
        target_key = ""
        if ctx.case and res.step_no <= len(ctx.case.steps):
            step = ctx.case.steps[res.step_no - 1]
            target_key = step.target.key if step.target else ""
        page_key = ctx.extra.get("page_key", "main")
        context = {}
        if ctx.case and 1 <= res.step_no <= len(ctx.case.steps):
            step = ctx.case.steps[res.step_no - 1]
            if step.action.startswith(("device.", "assert.")) or (
                    res.error or {}).get("type") == "verify_failed":
                context = {"cmd": step.args.get("cmd"), "scene": step.args.get("scene_id"),
                           "path": step.args.get("path"),
                           "expect": step.args.get("expect", step.verify.expect),
                           "verify": step.verify.type, "op": step.verify.op}
        return cls(err.get("type", "unknown"), res.action, target_key, page_key,
                   json.dumps(context, sort_keys=True) if context else "")

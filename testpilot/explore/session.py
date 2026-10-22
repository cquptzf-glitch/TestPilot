"""探索会话 — 记录工具调用轨迹并固化回 DSL 草稿（设计 11.1）。

链路：agent（或人工）逐步调用工具 -> ``ExplorationSession`` 留痕 ->
``to_draft_text()`` 产出 DSL 草稿 -> 走既有 ``case import`` / ``case accept`` 人审链路。
**探索负责"发现"，DSL 引擎负责"回归"**：本模块绝不直接把路径写进 active 用例。

会话可序列化为 JSON（``to_dict`` / ``from_dict``），因此 agent 侧（含 MCP 客户端）
只需产出这份记录，固化与审阅在引擎侧完成。
"""
import time
from dataclasses import dataclass

from testpilot.core.errors import TestPilotError
from testpilot.explore.toolbox import ToolBox, ToolError

# 可映射为 DSL 步骤的工具（其余是观测类，不进入用例）
DSL_TOOLS = ("app.launch", "app.wait_ready", "app.close",
             "ui.locate", "ui.click", "ui.input", "ui.select",
             "device.scene", "device.call")


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

    def to_dict(self) -> dict:
        return {"index": self.index, "tool": self.tool, "args": self.args,
                "ok": self.ok, "ms": self.ms, "driver": self.driver,
                "result": self.result, "error": self.error}

    @classmethod
    def from_dict(cls, data: dict) -> "ExploreStep":
        return cls(index=int(data.get("index", 0)),
                   tool=str(data.get("tool", "")),
                   args=dict(data.get("args") or {}),
                   ok=bool(data.get("ok", False)),
                   ms=int(data.get("ms") or 0),
                   driver=data.get("driver"),
                   result=data.get("result"),
                   error=data.get("error"))


class ExplorationSession:
    """一次探索会话：调用即留痕；成功后固化为**待审** DSL 草稿。"""

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

    # ---------- 执行 ----------

    def call(self, tool: str, args: dict | None = None) -> dict:
        """调用一个工具并记录一步；失败同样留痕后原样抛出（agent 据此重规划）。"""
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
                driver=args.get("driver"), error=str(e)))
            raise
        self.steps.append(ExploreStep(
            index=len(self.steps) + 1, tool=tool, args=args, ok=True,
            ms=int((time.monotonic() - t0) * 1000),
            driver=args.get("driver") or (out or {}).get("driver"), result=out))
        return out

    # ---------- 固化 ----------

    def to_case(self) -> dict:
        """转为 DSL 用例草稿（仅含成功且可映射的步骤）。"""
        steps = []
        skipped = 0
        for s in self.steps:
            if not s.ok:
                skipped += 1
                continue
            dsl = _to_dsl_step(s)
            if dsl is None:
                skipped += 1
                continue
            dsl["id"] = f"s{len(steps) + 1}"
            steps.append(dsl)

        notes = self.notes or ""
        if self.steps:
            summary = (f"由探索会话固化：工具调用 {len(self.steps)} 步，"
                       f"可映射 {len(steps)} 步，跳过 {skipped} 步"
                       f"（失败/观测步骤不进入用例）。需人审确认后生效。")
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
        """把草稿导入本地库（``status=draft``）；后续走 ``case accept`` 人审。"""
        return case_service.import_draft(self.to_draft_text("json"), "json")

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "title": self.title,
                "zentao_case_id": self.zentao_case_id,
                "preconditions": list(self.preconditions),
                "notes": self.notes,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict, toolbox: ToolBox | None = None) -> "ExplorationSession":
        """从 JSON 记录还原会话（不依赖工具箱，可直接转换与导入）。"""
        session = cls(toolbox, case_id=str(data.get("case_id") or "TC-EXPLORE"),
                      title=str(data.get("title") or "探索路径"),
                      zentao_case_id=data.get("zentao_case_id"),
                      preconditions=list(data.get("preconditions") or []),
                      notes=str(data.get("notes") or ""))
        session.steps = [ExploreStep.from_dict(s)
                         for s in (data.get("steps") or []) if isinstance(s, dict)]
        return session


# ---------- 工具 -> DSL 步骤映射 ----------

def _to_dsl_step(step: ExploreStep) -> dict | None:
    args = step.args or {}
    tool = step.tool

    if tool == "app.launch":
        spec = {"exe": args.get("exe")}
        if args.get("args"):
            spec["args"] = list(args["args"])
        if args.get("title_re"):
            spec["title_re"] = args["title_re"]
        return {"action": "app.launch", "args": _drop_none(spec),
                "source_text": f"[探索] 启动 {args.get('exe')}"}

    if tool == "app.wait_ready":
        return {"action": "app.wait_ready",
                "timeout_ms": int(args.get("timeout_ms", 10000)),
                "source_text": "[探索] 等待主窗口就绪"}

    if tool == "app.close":
        return {"action": "app.close",
                "args": {"mode": args.get("mode", "graceful")},
                "source_text": "[探索] 关闭被测程序"}

    if tool in ("ui.locate", "ui.click", "ui.input", "ui.select"):
        target = _target_of(args)
        action = {"ui.locate": "ui.wait", "ui.click": "ui.click",
                  "ui.input": "ui.input",
                  "ui.select": "ui.select"}[tool]
        out = {"action": action, "target": target,
               "source_text": f"[探索] {_describe(tool, args)}"}
        if tool == "ui.locate":
            out["args"] = {"state": "exists"}
        if tool == "ui.input":
            out["value"] = str(args.get("text") or "")
            if args.get("clear"):
                out["args"] = {"clear": True}
        if tool == "ui.select":
            out["value"] = str(args.get("value") or "")
        return out

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


def _target_of(args: dict) -> dict:
    """工具参数 -> DSL TargetDesc（保留实际生效的定位方式为 primary）。"""
    key = str(args.get("key") or "").strip() or "explore.target"
    driver = args.get("driver")
    if not driver:
        driver = "uia" if args.get("name") or args.get("automation_id") else "vision"
    if driver == "uia":
        primary = _drop_none({"type": "uia", "name": args.get("name"),
                              "automation_id": args.get("automation_id"),
                              "control_type": args.get("control_type")})
        fallback = None
        if args.get("desc"):
            fallback = {"type": "visual", "desc": args["desc"]}
        elif args.get("template"):
            fallback = {"type": "template", "template": args["template"]}
    elif args.get("template"):
        primary = {"type": "template", "template": args["template"]}
        fallback = None
    else:
        primary = {"type": "visual", "desc": args.get("desc")}
        fallback = None
    target = {"key": key, "primary": primary}
    if fallback:
        target["fallback"] = fallback
    return target


def _describe(tool: str, args: dict) -> str:
    key = args.get("key") or args.get("name") or args.get("desc") or "target"
    if tool == "ui.input":
        return f"在 {key} 输入 {args.get('text')!r}"
    if tool == "ui.select":
        return f"在 {key} 选择 {args.get('value')!r}"
    if tool == "ui.locate":
        return f"等待 {key} 出现"
    return f"点击 {key}"


def _drop_none(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}

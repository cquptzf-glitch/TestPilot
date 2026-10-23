"""SVN change -> Agent impact analysis -> generated local test cases.

The agent is a planner/researcher only. It cannot write files or the database. Final
JSON is validated here, then TestPilot itself creates draft cases and optionally runs
the existing DSL parser. Human review remains the activation gate.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from testpilot.adapters.svn_adapter import SvnAdapter
from testpilot.adapters.zentao_adapter import steps_to_text
from testpilot.core.errors import AgentError, TestPilotError
from testpilot.storage import repos


class GeneratedStep(BaseModel):
    desc: str = Field(min_length=1, max_length=2000)
    expect: str = Field(default="", max_length=2000)


class GeneratedCase(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    priority: Literal["P0", "P1", "P2", "P3"] = "P1"
    category: Literal[
        "regression", "boundary", "error", "compatibility",
        "integration", "performance", "security",
    ] = "regression"
    rationale: str = Field(default="", max_length=4000)
    preconditions: list[str] = Field(default_factory=list, max_length=30)
    steps: list[GeneratedStep] = Field(min_length=1, max_length=50)
    source_files: list[str] = Field(default_factory=list, max_length=50)
    related_case_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("source_files")
    @classmethod
    def _source_files_are_workspace_relative(cls, values: list[str]) -> list[str]:
        out = []
        for raw in values:
            text = str(raw or "").strip().replace("\\", "/")
            if not text:
                continue
            path = PurePosixPath(text)
            if text.startswith("/") or re.match(r"^[A-Za-z]:/", text) or ".." in path.parts:
                raise ValueError(f"source_files 必须是工作副本内相对路径: {raw!r}")
            if text not in out:
                out.append(text)
        return out


class ChangeAnalysis(BaseModel):
    summary: str = Field(min_length=1, max_length=8000)
    impacted_modules: list[str] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)
    cases: list[GeneratedCase] = Field(min_length=1, max_length=50)


class ChangeAnalysisService:
    def __init__(self, conn, cases, cfg: dict, agent_runner):
        self.conn = conn
        self.cases = cases
        self.cfg = cfg or {}
        self.agent_runner = agent_runner
        self.svn = SvnAdapter((self.cfg.get("svn") or {}))
        self.abort = None
        self.progress = None

    def preview(self, spec: dict) -> dict:
        workspace = self._workspace(spec)
        return self.svn.summarize(
            workspace,
            from_revision=spec.get("fromRevision") or spec.get("from_revision"),
            to_revision=spec.get("toRevision") or spec.get("to_revision"),
        )

    def generate(self, spec: dict) -> dict:
        workspace = self._workspace(spec)
        from_rev = spec.get("fromRevision") or spec.get("from_revision")
        to_rev = spec.get("toRevision") or spec.get("to_revision")
        parse_dsl = bool(spec.get("parse", True))
        max_cases = max(1, min(int(spec.get("maxCases") or 20), 50))

        snapshot = self.svn.summarize(
            workspace, from_revision=from_rev, to_revision=to_rev)
        changed = snapshot.get("changed") or []
        working = snapshot.get("working_status") or []
        if not changed and not working:
            raise TestPilotError("SVN 范围内没有检测到修改，未生成测试用例")
        self._report("snapshot", changed=len(changed), working=len(working))

        prompt = _build_prompt(snapshot, max_cases=max_cases)
        result = self.agent_runner(
            prompt=prompt, workspace=str(workspace),
            tools=[
                "tp_read_file", "tp_find_files", "tp_grep",
                "tp_svn_status", "tp_svn_diff", "tp_svn_log",
                "tp_case_search", "tp_case_detail",
            ],
            abort=self.abort, progress=self._agent_progress,
        )
        try:
            analysis = _parse_analysis(result.get("text"), max_cases=max_cases)
        except AgentError as exc:
            # Agent 进程已正常返回，但最终 JSON/Schema 无效时，也要把主 RPC
            # 工具审计留在后台 Job progress 中，避免失败现场随异常一起丢失。
            self._report(
                "agent_output_invalid",
                tool_calls=result.get("tool_calls", 0),
                agent_error=str(exc)[:500],
                agent_audit=result.get("audit") or [],
            )
            raise
        self._report("agent_done", cases=len(analysis.cases),
                     tool_calls=result.get("tool_calls", 0))

        generated = []
        created_count = 0
        reused_count = 0
        duplicates_dropped = 0
        parse_ids = []
        seen_fingerprints = set()
        revision_label = str(to_rev or snapshot.get("info", {}).get("revision") or "WC")
        existing_by_fingerprint = self._generated_by_fingerprint()
        for index, item in enumerate(analysis.cases, 1):
            fingerprint = self._case_fingerprint(snapshot, revision_label, item)
            if fingerprint in seen_fingerprints:
                duplicates_dropped += 1
                self._report("dedupe", completed=index, total=len(analysis.cases))
                continue
            seen_fingerprints.add(fingerprint)
            existing = existing_by_fingerprint.get(fingerprint)
            if existing is not None:
                case_id = existing["case_id"]
                reused = True
                reused_count += 1
                if (parse_dsl and existing.get("status") != "rejected"
                        and not existing.get("steps")):
                    parse_ids.append(case_id)
            else:
                case_id = self._new_case_id(revision_label, fingerprint)
                reused = False
                raw_steps = [step.model_dump() for step in item.steps]
                raw = {
                    "source": "svn_agent",
                    "case_id": case_id,
                    "zentao_case_id": None,
                    "title": item.title,
                    "steps": raw_steps,
                    "steps_text": steps_to_text(raw_steps),
                    "preconditions": list(dict.fromkeys(item.preconditions)),
                    "product": None,
                    "module": None,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "svn": {
                        "working_copy": str(workspace),
                        "from_revision": snapshot.get("from_revision"),
                        "to_revision": snapshot.get("to_revision"),
                        "repository_root": snapshot.get("info", {}).get("repository_root"),
                        "repository_uuid": snapshot.get("info", {}).get("repository_uuid"),
                        "source_files": item.source_files,
                    },
                    "generation": {
                        "fingerprint": fingerprint,
                        "priority": item.priority,
                        "category": item.category,
                        "rationale": item.rationale,
                        "related_case_ids": item.related_case_ids,
                        "analysis_summary": analysis.summary,
                        "risks": analysis.risks,
                    },
                }
                repos.create_case(
                    self.conn, case_id=case_id, zentao_case_id=None,
                    title=item.title, raw=raw, zentao_updated_at=None)
                created_count += 1
                if parse_dsl:
                    parse_ids.append(case_id)
                existing_by_fingerprint[fingerprint] = repos.get_case(self.conn, case_id)

            display_title = existing.get("title") if existing is not None else item.title
            generated.append({
                "case_id": case_id, "title": display_title, "priority": item.priority,
                "category": item.category, "source_files": item.source_files,
                "related_case_ids": item.related_case_ids, "reused": reused,
            })
            self._report("persist", completed=index, total=len(analysis.cases),
                         case_id=case_id, reused=reused)

        parsed = {}
        if parse_ids:
            parsed = self.cases.parse_many(list(dict.fromkeys(parse_ids)),
                                           only_stale=False)

        return {
            "source": "svn_agent",
            "summary": analysis.summary,
            "impacted_modules": analysis.impacted_modules,
            "risks": analysis.risks,
            "snapshot": snapshot,
            "cases": generated,
            "created": created_count,
            "reused": reused_count,
            "duplicates_dropped": duplicates_dropped,
            "parse": parsed,
            "agent": {
                "runtime": "pi",
                "tool_calls": result.get("tool_calls", 0),
                "elapsed_ms": result.get("elapsed_ms"),
                "audit": result.get("audit") or [],
            },
        }

    def _workspace(self, spec: dict) -> Path:
        raw = str(spec.get("workingCopy") or spec.get("working_copy")
                  or (self.cfg.get("svn") or {}).get("working_copy") or "").strip()
        if not raw:
            raise TestPilotError("未配置 SVN 工作副本（workingCopy / svn.working_copy）")
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise TestPilotError(f"SVN 工作副本不存在: {path}")
        return path

    def _generated_by_fingerprint(self) -> dict[str, dict]:
        """Index previously generated SVN cases for idempotent regeneration."""
        out = {}
        for row in repos.list_cases(self.conn):
            raw = row.get("raw") or {}
            if raw.get("source") != "svn_agent":
                continue
            fingerprint = str((raw.get("generation") or {}).get("fingerprint") or "")
            if fingerprint and fingerprint not in out:
                out[fingerprint] = row
        return out

    @staticmethod
    def _case_fingerprint(snapshot: dict, revision: str, item: GeneratedCase) -> str:
        """Stable semantic fingerprint for the same SVN change + test intent.

        Rationale/priority are deliberately excluded: model wording or triage changes
        should not create duplicate executable intent. Step order remains significant.
        """
        payload = {
            "repository_uuid": (snapshot.get("info") or {}).get("repository_uuid"),
            "repository_root": (snapshot.get("info") or {}).get("repository_root"),
            "from_revision": snapshot.get("from_revision"),
            "to_revision": snapshot.get("to_revision"),
            "effective_revision": revision,
            "title": item.title.strip(),
            "source_files": sorted(set(item.source_files)),
            "preconditions": sorted(set(item.preconditions)),
            "steps": [step.model_dump() for step in item.steps],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _new_case_id(self, revision: str, fingerprint: str) -> str:
        rev = re.sub(r"[^A-Za-z0-9._-]+", "-", revision).strip("-") or "WC"
        base = f"SVN-{rev}-{fingerprint[:10].upper()}"
        candidate = base
        suffix = 2
        while repos.get_case(self.conn, candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _agent_progress(self, value: dict) -> None:
        # Pi runtime 自身会携带 phase=agent_tool。旧代码再次以
        # _report("agent", **value) 传入 phase，会触发 "multiple values for
        # argument 'phase'"，导致真实 Pi 第一次工具调用即中断。
        payload = dict(value or {})
        phase = str(payload.pop("phase", "agent") or "agent")
        self._report(phase, **payload)

    def _report(self, phase: str, **extra) -> None:
        if self.abort is not None:
            self.abort.check()
        if self.progress:
            self.progress({"phase": phase, **extra})


def _build_prompt(snapshot: dict, *, max_cases: int) -> str:
    # Working-copy analysis needs both tracked diff entries and unversioned/status-only
    # paths. De-duplicate by path so a modified tracked file doesn't appear twice.
    changed = []
    seen = set()
    for item in [*(snapshot.get("changed") or []),
                 *(snapshot.get("working_status") or [])]:
        path = str(item.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        changed.append(item)
    changed_text = "\n".join(
        f"- {item.get('status') or item.get('item')}: {item.get('path')}"
        for item in changed[:200])
    return f"""
你是 TestPilot 的变更影响测试 Agent。目标：根据 SVN 修改生成高价值、可执行、去重的测试用例。

安全/工具规则：
1. 你没有本地文件、shell、SVN 的直接权限；所有信息必须通过 tp_* 工具获取。
2. 必须先阅读 SVN diff，再按需要读取相关源码；对关键行为使用 tp_case_search 检查已有回归覆盖。
3. 不要修改任何文件、数据库或测试用例；TestPilot 主进程会验证并落库。
4. 不要臆造未在代码/diff/已有用例中找到依据的具体 UI 控件名、协议字段或数值。

变更范围：
- working_copy: {snapshot.get("working_copy")}
- from_revision: {snapshot.get("from_revision")}
- to_revision: {snapshot.get("to_revision")}
- 当前已知变更：
{changed_text or "- working copy change; use tp_svn_status/tp_svn_diff"}

分析要求：
- 识别直接改动、调用链/数据流级联影响、公共组件波及、配置/错误处理/边界条件风险。
- 优先生成回归、边界、异常、兼容性测试；避免只复述代码 diff。
- 与已有用例高度重复时，优先在 related_case_ids 中引用，并只生成真正新增的覆盖。
- 最多生成 {max_cases} 条。

最终回复必须只有一个 JSON 对象，不要 Markdown 代码块，不要额外解释：
{{
  "summary": "变更与测试策略摘要",
  "impacted_modules": ["模块"],
  "risks": ["风险"],
  "cases": [
    {{
      "title": "用例标题",
      "priority": "P0|P1|P2|P3",
      "category": "regression|boundary|error|compatibility|integration|performance|security",
      "rationale": "为什么需要此用例",
      "preconditions": ["前置条件"],
      "steps": [{{"desc": "操作/输入", "expect": "预期结果"}}],
      "source_files": ["相对工作副本路径"],
      "related_case_ids": ["已有本地用例 ID"]
    }}
  ]
}}
""".strip()


def _parse_analysis(text: str | None, *, max_cases: int) -> ChangeAnalysis:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise AgentError("Pi Agent 最终输出不含 JSON 对象") from None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AgentError(f"Pi Agent JSON 无法解析: {exc}") from exc
    try:
        analysis = ChangeAnalysis.model_validate(data)
    except ValidationError as exc:
        raise AgentError(f"Pi Agent 测试用例输出结构无效: {exc}") from exc
    if len(analysis.cases) > max_cases:
        analysis.cases = analysis.cases[:max_cases]
    return analysis

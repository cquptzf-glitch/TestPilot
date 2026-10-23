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
    source_files: list[str] = Field(min_length=1, max_length=50)
    related_case_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("source_files")
    @classmethod
    def _source_files_are_workspace_relative(cls, values: list[str]) -> list[str]:
        out = []
        for raw in values:
            text = _repo_path(raw)
            if not text:
                continue
            path = PurePosixPath(text)
            if text.startswith("/") or re.match(r"^[A-Za-z]:/", text) or ".." in path.parts:
                raise ValueError(f"source_files 必须是工作副本内相对路径: {raw!r}")
            if text not in out:
                out.append(text)
        if not out:
            raise ValueError("source_files 至少需要 1 条真实代码来源")
        return out

    @field_validator("related_case_ids")
    @classmethod
    def _related_case_ids_are_clean(cls, values: list[str]) -> list[str]:
        out = []
        for raw in values:
            text = str(raw or "").strip()
            if not text:
                continue
            if len(text) > 200:
                raise ValueError(f"related_case_ids 过长: {raw!r}")
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
        parse_dsl = _bool_option(spec, "parse", default=True)
        max_cases = _bounded_int_option(
            spec, "maxCases", default=20, min_value=1, max_value=50)

        snapshot = self.svn.summarize(
            workspace, from_revision=from_rev, to_revision=to_rev)
        changed = snapshot.get("changed") or []
        working = snapshot.get("working_status") or []
        if not changed and not working:
            raise TestPilotError("SVN 范围内没有检测到修改，未生成测试用例")
        snapshot_guard = self._snapshot_guard(
            workspace, snapshot, from_rev=from_rev, to_rev=to_rev)
        self._report(
            "snapshot", changed=len(changed), working=len(working),
            signature=snapshot_guard["signature"][:12])

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
            self._validate_snapshot_stable(
                workspace, snapshot, snapshot_guard,
                from_rev=from_rev, to_rev=to_rev, result=result)
            grounding = self._validate_grounding(
                workspace, snapshot, analysis, result.get("evidence") or {})
        except AgentError as exc:
            # Agent 进程已正常返回，但最终 JSON/Schema 无效时，也要把主 RPC
            # 工具审计留在后台 Job progress 中，避免失败现场随异常一起丢失。
            self._report(
                "agent_output_invalid",
                tool_calls=result.get("tool_calls", 0),
                agent_error=str(exc)[:500],
                agent_audit=result.get("audit") or [],
                agent_evidence=result.get("evidence") or {},
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
                        "grounding": grounding[index - 1],
                        "snapshot_guard": snapshot_guard,
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
            "snapshot_guard": snapshot_guard,
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
                "evidence": result.get("evidence") or {},
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

    def _snapshot_guard(self, workspace: Path, snapshot: dict, *,
                        from_rev=None, to_rev=None) -> dict:
        """Capture a deterministic signature for the exact change set being analyzed."""
        diff = self.svn.diff_digest(
            workspace, from_revision=from_rev, to_revision=to_rev)
        changed = sorted([
            {
                "status": str(item.get("status") or ""),
                "path": _repo_path(item.get("path")),
            }
            for item in (snapshot.get("changed") or [])
        ], key=lambda item: (item["path"], item["status"]))
        working = sorted([
            {
                "path": _repo_path(item.get("path")),
                "item": str(item.get("item") or ""),
                "props": str(item.get("props") or ""),
                "revision": str(item.get("revision") or ""),
                "tree_conflicted": bool(item.get("tree_conflicted")),
            }
            for item in (snapshot.get("working_status") or [])
        ], key=lambda item: (item["path"], item["item"], item["props"]))
        unversioned = []
        for item in working:
            if item["item"] != "unversioned" or not item["path"]:
                continue
            unversioned.append({
                "path": item["path"],
                "sha256": _workspace_path_digest(workspace, item["path"]),
            })
        info = snapshot.get("info") or {}
        payload = {
            "from_revision": snapshot.get("from_revision"),
            "to_revision": snapshot.get("to_revision"),
            "repository_root": info.get("repository_root"),
            "repository_uuid": info.get("repository_uuid"),
            "working_copy_revision": info.get("revision"),
            "changed": changed,
            "working_status": working,
            "diff_sha256": diff.get("sha256"),
            "diff_bytes": diff.get("bytes"),
            "unversioned": unversioned,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        return {
            "signature": hashlib.sha256(encoded).hexdigest(),
            "mode": "revision" if snapshot.get("from_revision")
            or snapshot.get("to_revision") else "working_copy",
            "diff_sha256": str(diff.get("sha256") or ""),
            "diff_bytes": int(diff.get("bytes") or 0),
            "unversioned_count": len(unversioned),
        }

    def _validate_snapshot_stable(self, workspace: Path, snapshot: dict,
                                  initial_guard: dict, *, from_rev=None,
                                  to_rev=None, result: dict) -> None:
        current_snapshot = self.svn.summarize(
            workspace, from_revision=from_rev, to_revision=to_rev)
        current_guard = self._snapshot_guard(
            workspace, current_snapshot, from_rev=from_rev, to_rev=to_rev)
        evidence = result.get("evidence") or {}
        read_drift = _changed_agent_reads(workspace, evidence)
        if (initial_guard.get("signature") == current_guard.get("signature")
                and not read_drift):
            return

        reasons = []
        if initial_guard.get("signature") != current_guard.get("signature"):
            reasons.append("SVN 变更集/工作副本已变化")
        if read_drift:
            reasons.append("Agent 已读取源码在分析期间变化: " + ", ".join(read_drift[:8]))
        message = "；".join(reasons) + "，请重新执行变更分析"
        self._report(
            "snapshot_changed",
            agent_error=message,
            snapshot_before=str(initial_guard.get("signature") or "")[:16],
            snapshot_after=str(current_guard.get("signature") or "")[:16],
            changed_read_paths=read_drift[:20],
            agent_audit=result.get("audit") or [],
            agent_evidence=evidence,
        )
        raise TestPilotError(message)

    def _validate_grounding(self, workspace: Path, snapshot: dict,
                            analysis: ChangeAnalysis, evidence: dict) -> list[dict]:
        """Fail closed before persistence and return durable per-case provenance."""
        evidence = evidence if isinstance(evidence, dict) else {}
        successes = evidence.get("successes")
        successes = successes if isinstance(successes, dict) else {}
        diff_success = successes.get("agent.svn.diff", 0)
        if (isinstance(diff_success, bool) or not isinstance(diff_success, int)
                or diff_success < 1):
            raise AgentError(
                "Pi Agent 未通过主 RPC 成功读取 SVN diff，拒绝生成测试用例")

        diff_changed_paths = {
            _repo_path(item.get("path"))
            for item in (snapshot.get("changed") or [])
            if _repo_path(item.get("path"))
        }
        working_paths = {
            _repo_path(item.get("path"))
            for item in (snapshot.get("working_status") or [])
            if _repo_path(item.get("path"))
        }
        known_change_paths = diff_changed_paths | working_paths
        read_paths = {
            _repo_path(path)
            for path in (evidence.get("read_paths") or [])
            if _repo_path(path)
        }
        observed_case_ids = {
            str(case_id or "").strip()
            for case_id in (evidence.get("case_ids") or [])
            if str(case_id or "").strip()
        }
        diff_paths = {
            _repo_path(path)
            for path in (evidence.get("diff_paths") or [])
            if _repo_path(path)
        }
        full_diff_complete = evidence.get("full_diff_complete") is True
        full_diff_truncated = evidence.get("full_diff_truncated") is True

        problems = []
        grounding = []
        for item in analysis.cases:
            item_sources = []
            for source in item.source_files:
                rel = _repo_path(source)
                bases = []
                if rel in read_paths:
                    candidate = workspace.joinpath(*PurePosixPath(rel).parts)
                    if candidate.is_file():
                        bases.append("agent.fs.read")
                    else:
                        problems.append(
                            f"{item.title}: source_files 当前工作区文件不存在: {rel}")
                if (rel in diff_changed_paths
                        and (full_diff_complete or rel in diff_paths)):
                    bases.append("agent.svn.diff")
                if not bases:
                    if rel in working_paths and rel not in diff_changed_paths:
                        problems.append(
                            f"{item.title}: 工作副本状态路径必须先 agent.fs.read: {rel}")
                    elif rel in diff_changed_paths and full_diff_truncated:
                        problems.append(
                            f"{item.title}: 全量 SVN diff 已截断，需按路径 diff/read: {rel}")
                    elif rel in known_change_paths:
                        problems.append(
                            f"{item.title}: SVN 变更路径未被成功 diff/read 覆盖: {rel}")
                    else:
                        problems.append(
                            f"{item.title}: source_files 无 SVN diff 或 agent.fs.read 证据: "
                            f"{rel}")
                    continue
                item_sources.append({"path": rel, "basis": list(dict.fromkeys(bases))})

            item_related = []
            for case_id in item.related_case_ids:
                if repos.get_case(self.conn, case_id) is None:
                    problems.append(
                        f"{item.title}: related_case_ids 不存在: {case_id}")
                elif case_id not in observed_case_ids:
                    problems.append(
                        f"{item.title}: related_case_ids 无 agent.case.search/detail 证据: "
                        f"{case_id}")
                else:
                    item_related.append({
                        "case_id": case_id,
                        "basis": "agent.case.search/detail",
                    })
            grounding.append({
                "svn_diff_verified": True,
                "source_files": item_sources,
                "related_case_ids": item_related,
            })

        if problems:
            detail = "；".join(problems[:8])
            if len(problems) > 8:
                detail += f"；另有 {len(problems) - 8} 项"
            raise AgentError(f"Pi Agent 生成依据校验失败: {detail}")
        return grounding

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
        # 终态诊断必须先写入 progress；取消信号此时通常已置位，若再次
        # checkpoint 会在写入 agent_audit 前重新抛 AbortRequested。
        terminal = phase in ("agent_failed", "agent_cancelled")
        self._report(phase, check_abort=not terminal, **payload)

    def _report(self, phase: str, *, check_abort: bool = True, **extra) -> None:
        if check_abort and self.abort is not None:
            self.abort.check()
        if self.progress:
            self.progress({"phase": phase, **extra})


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise TestPilotError(f"无法读取 SVN 快照文件 {path}: {exc}") from exc
    return digest.hexdigest()


def _workspace_path_digest(workspace: Path, rel: str) -> str:
    path = workspace.joinpath(*PurePosixPath(rel).parts)
    if path.is_symlink():
        try:
            target = str(path.readlink())
        except OSError as exc:
            raise TestPilotError(f"无法读取符号链接 {rel}: {exc}") from exc
        return hashlib.sha256(("symlink\0" + target).encode("utf-8")).hexdigest()
    if not path.exists():
        return hashlib.sha256(b"missing").hexdigest()
    if path.is_file():
        return _file_sha256(path)
    if not path.is_dir():
        return hashlib.sha256(("other\0" + rel).encode("utf-8")).hexdigest()

    digest = hashlib.sha256()
    base = path
    try:
        children = sorted(
            (child for child in base.rglob("*")
             if ".svn" not in child.relative_to(base).parts),
            key=lambda child: child.relative_to(base).as_posix(),
        )
    except OSError as exc:
        raise TestPilotError(f"无法遍历未版本控制目录 {rel}: {exc}") from exc
    for child in children:
        child_rel = child.relative_to(base).as_posix()
        digest.update(child_rel.encode("utf-8"))
        digest.update(b"\0")
        if child.is_symlink():
            try:
                digest.update(b"L\0" + str(child.readlink()).encode("utf-8"))
            except OSError as exc:
                raise TestPilotError(
                    f"无法读取未版本控制符号链接 {rel}/{child_rel}: {exc}") from exc
        elif child.is_file():
            digest.update(b"F\0" + _file_sha256(child).encode("ascii"))
        elif child.is_dir():
            digest.update(b"D")
        else:
            digest.update(b"O")
        digest.update(b"\0")
    return digest.hexdigest()


def _changed_agent_reads(workspace: Path, evidence: dict) -> list[str]:
    hashes = evidence.get("read_hashes") if isinstance(evidence, dict) else None
    if not isinstance(hashes, dict):
        return []
    changed = []
    for raw_path, expected in hashes.items():
        rel = _repo_path(raw_path)
        if not rel or not isinstance(expected, str) or not expected:
            continue
        path = workspace.joinpath(*PurePosixPath(rel).parts)
        if not path.is_file():
            changed.append(rel)
            continue
        if _file_sha256(path) != expected:
            changed.append(rel)
    return sorted(set(changed))


def _repo_path(value) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text).as_posix() if text else ""


def _bool_option(spec: dict, key: str, *, default: bool) -> bool:
    value = spec.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TestPilotError(
            f"{key} 需为布尔值 true/false（得到 {value!r}）")
    return value


def _bounded_int_option(spec: dict, key: str, *, default: int,
                        min_value: int, max_value: int) -> int:
    value = spec.get(key, default)
    if value is None or value == "":
        value = default
    if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()):
        raise TestPilotError(f"{key} 需为整数（得到 {value!r}）")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise TestPilotError(f"{key} 需为整数（得到 {value!r}）") from None
    if parsed < min_value:
        raise TestPilotError(f"{key} 需 >= {min_value}（得到 {parsed}）")
    return min(parsed, max_value)


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
3. 每条用例的 source_files 必须至少 1 条，只能引用已被成功 SVN diff 覆盖的变更路径，或你确实通过 tp_read_file 成功读取过的工作区文件。
4. 若 tp_svn_diff 返回 truncated=true，相关 source_files 必须再按具体 paths 调 tp_svn_diff，或通过 tp_read_file 读取；unversioned 文件必须使用 tp_read_file。
5. related_case_ids 只能填写 tp_case_search/tp_case_detail 实际返回过且真实存在的本地用例 ID；没有依据就留空.
6. 不要修改任何文件、数据库或测试用例；TestPilot 主进程会验证并落库。
7. 不要臆造未在代码/diff/已有用例中找到依据的具体 UI 控件名、协议字段或数值。

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

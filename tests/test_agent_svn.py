"""SVN change analysis + Pi RPC-only tool boundary tests.

No real SVN/Pi process is required. These tests pin the main-process trust boundary so a
future refactor cannot accidentally give the agent native file/shell/database access.
"""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import testpilot.agent.change_service as change_service_mod
from testpilot.adapters.svn_adapter import SvnAdapter, _revision_args
from testpilot.agent.change_service import ChangeAnalysisService, _build_prompt, _parse_analysis
from testpilot.agent.pi_runtime import PiAgentRuntime
from testpilot.case_parser import CaseService
from testpilot.core.errors import (AbortRequested, AgentError, RpcError, SvnError,
                                   TestPilotError)
from testpilot.rpc import RpcService
from testpilot.storage import repos


@pytest.fixture()
def rpc_svc(orch_factory):
    """Minimal RpcService for Agent capability-boundary tests."""
    return RpcService(orch_factory())


class StubSvn(SvnAdapter):
    def __init__(self, root):
        super().__init__({"executable": "svn"})
        self.root = Path(root)
        self.calls = []

    def executable_path(self):
        return "svn"

    def diff_digest(self, working_copy, *, from_revision=None, to_revision=None):
        text = self._run(
            ["diff", *_revision_args(from_revision, to_revision)],
            cwd=Path(working_copy), limit=1_000_000)
        raw = text.encode("utf-8")
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "from_revision": from_revision,
            "to_revision": to_revision,
        }

    def _run(self, args, *, cwd, limit):
        self.calls.append((list(args), cwd, limit))
        if args[:2] == ["info", "--xml"]:
            return """<?xml version="1.0"?>
<info><entry kind="dir" path="." revision="42">
<url>https://svn.local/product/trunk</url>
<repository><root>https://svn.local/product</root><uuid>u-1</uuid></repository>
</entry></info>"""
        if args[:2] == ["status", "--xml"]:
            return """<?xml version="1.0"?>
<status><target path=".">
<entry path="src/a.cpp"><wc-status item="modified" props="none" revision="41"/></entry>
<entry path="tmp/new.txt"><wc-status item="unversioned" props="none"/></entry>
</target></status>"""
        if args[:2] == ["diff", "--summarize"]:
            return "M       src/a.cpp\nA       src/new.cpp\n"
        if args and args[0] == "diff":
            return "Index: src/a.cpp\n+ changed\n"
        if args[:2] == ["log", "--xml"]:
            return """<log><logentry revision="42"><author>a</author>
<date>2026-10-23T00:00:00Z</date><msg>change</msg></logentry></log>"""
        if args[:2] == ["--version", "--quiet"]:
            return "1.14.5"
        raise AssertionError(args)


def test_svn_adapter_parses_bounded_read_operations(tmp_path):
    svn = StubSvn(tmp_path)
    info = svn.info(tmp_path)
    assert info["revision"] == "42"
    assert info["repository_uuid"] == "u-1"

    status = svn.status(tmp_path)
    assert {item["item"] for item in status} == {"modified", "unversioned"}

    summary = svn.summarize(tmp_path)
    assert [item["path"] for item in summary["changed"]] == [
        "src/a.cpp", "src/new.cpp",
    ]
    assert summary["working_status"][0]["path"] == "src/a.cpp"

    diff = svn.diff(tmp_path, paths=["src/a.cpp"], max_chars=50_000)
    assert "+ changed" in diff["text"]
    assert diff["paths"] == ["src/a.cpp"]


def test_svn_adapter_rejects_revision_and_path_injection(tmp_path):
    assert _revision_args("12", "HEAD") == ["-r", "12:HEAD"]
    with pytest.raises(SvnError, match="非法 SVN revision"):
        _revision_args("12; whoami", "HEAD")

    svn = StubSvn(tmp_path)
    outside = tmp_path.parent / "outside.cpp"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(SvnError, match="不在工作副本"):
        svn.diff(tmp_path, paths=["../outside.cpp"])


def _agent_evidence(*read_paths: str, case_ids: tuple[str, ...] = ()) -> dict:
    return {
        "calls": {
            "agent.svn.diff": 1,
            **({"agent.fs.read": len(read_paths)} if read_paths else {}),
            **({"agent.case.search": 1} if case_ids else {}),
        },
        "successes": {
            "agent.svn.diff": 1,
            **({"agent.fs.read": len(read_paths)} if read_paths else {}),
            **({"agent.case.search": 1} if case_ids else {}),
        },
        "failures": {},
        "read_paths": list(read_paths),
        "read_paths_truncated": False,
        "read_hashes": {},
        "content_hashes": {},
        "content_hashes_truncated": False,
        "case_ids": list(case_ids),
        "case_ids_truncated": False,
        "diff_paths": [],
        "diff_paths_truncated": False,
        "full_diff_complete": True,
        "full_diff_truncated": False,
    }


def _agent_json():
    return json.dumps({
        "summary": "状态机修改会影响启动、完成和异常恢复。",
        "impacted_modules": ["RunController"],
        "risks": ["状态竞争"],
        "cases": [{
            "title": "检测状态机修改回归",
            "priority": "P0",
            "category": "regression",
            "rationale": "状态切换逻辑修改",
            "preconditions": ["程序已启动"],
            "steps": [
                {"desc": "启动检测", "expect": "进入检测中"},
                {"desc": "等待完成", "expect": "进入已完成"},
            ],
            "source_files": ["src/RunController.cpp"],
            "related_case_ids": ["TC-1"],
        }],
    }, ensure_ascii=False)


def test_change_service_agent_only_returns_plan_main_process_persists(
        tmp_path, db_conn, fake_zentao, fake_llm):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "RunController.cpp").write_text("void run(){}", encoding="utf-8")

    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    repos.create_case(
        db_conn, case_id="TC-1", zentao_case_id=None,
        title="已有状态机回归", raw={"steps_text": "已有覆盖"},
        zentao_updated_at=None)
    calls = []

    progress_events = []

    def agent_runner(**kwargs):
        calls.append(kwargs)
        kwargs["progress"]({
            "phase": "agent_tool",
            "tool": "tp_svn_diff",
            "tool_calls": 1,
            "events": 2,
        })
        return {
            "text": _agent_json(), "tool_calls": 4, "elapsed_ms": 20,
            "audit": [
                {
                    "seq": 1, "method": "agent.svn.diff", "params": {},
                    "ok": True, "elapsed_ms": 3,
                    "result": {"chars": 10, "truncated": False, "path_count": 1},
                },
                {
                    "seq": 2, "method": "agent.fs.read",
                    "params": {"path": "src/RunController.cpp"},
                    "ok": True, "elapsed_ms": 1,
                    "result": {"path": "src/RunController.cpp", "lines": 1},
                },
            ],
            "evidence": _agent_evidence(
                "src/RunController.cpp", case_ids=("TC-1",)),
        }

    service = ChangeAnalysisService(
        db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    service.progress = progress_events.append

    out = service.generate({
        "workingCopy": str(tmp_path),
        "parse": False,
        "maxCases": 5,
    })
    assert out["created"] == 1
    assert out["agent"]["tool_calls"] == 4
    assert out["agent"]["audit"][0]["method"] == "agent.svn.diff"
    assert out["agent"]["evidence"]["successes"]["agent.svn.diff"] == 1
    assert out["agent"]["evidence"]["read_paths"] == ["src/RunController.cpp"]
    assert out["agent"]["evidence"]["case_ids"] == ["TC-1"]
    assert any(
        event.get("phase") == "agent_tool"
        and event.get("tool") == "tp_svn_diff"
        for event in progress_events
    )
    assert set(calls[0]["tools"]) == {
        "tp_read_file", "tp_find_files", "tp_grep",
        "tp_svn_status", "tp_svn_diff", "tp_svn_log",
        "tp_case_search", "tp_case_detail",
    }

    generated = repos.get_case(db_conn, out["cases"][0]["case_id"])
    assert generated is not None
    assert generated["status"] == "draft"
    assert generated["zentao_case_id"] is None
    assert generated["raw"]["source"] == "svn_agent"
    assert generated["raw"]["svn"]["source_files"] == ["src/RunController.cpp"]
    assert generated["raw"]["generation"]["fingerprint"]
    guard = generated["raw"]["generation"]["snapshot_guard"]
    assert guard["signature"] == out["snapshot_guard"]["signature"]
    assert guard["mode"] == "working_copy"
    grounding = generated["raw"]["generation"]["grounding"]
    assert grounding["svn_diff_verified"] is True
    assert grounding["source_files"] == [{
        "path": "src/RunController.cpp", "basis": ["agent.fs.read"],
    }]
    assert grounding["related_case_ids"] == [{
        "case_id": "TC-1", "basis": "agent.case.search/detail",
    }]
    assert generated["steps"] == []       # Agent cannot directly activate/write executable DSL.

    # Same change + same test semantics is idempotent: reuse the draft instead of -2/-3.
    again = service.generate({
        "workingCopy": str(tmp_path),
        "parse": True,
        "maxCases": 5,
    })
    assert again["created"] == 0
    assert again["reused"] == 1
    assert again["duplicates_dropped"] == 0
    assert again["cases"][0]["case_id"] == out["cases"][0]["case_id"]
    assert again["cases"][0]["reused"] is True
    assert again["parse"]["parsed"] == 1
    after = repos.get_case(db_conn, out["cases"][0]["case_id"])
    assert len(after["steps"]) > 0

    svn_generated = [
        row for row in repos.list_cases(db_conn)
        if (row.get("raw") or {}).get("source") == "svn_agent"
    ]
    assert len(svn_generated) == 1


def test_change_service_dedupes_duplicate_intents_from_one_agent_turn(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = []
    payload["cases"].append(dict(payload["cases"][0]))

    def agent_runner(**_kwargs):
        return {"text": json.dumps(payload, ensure_ascii=False),
                "tool_calls": 2, "elapsed_ms": 10,
                "evidence": _agent_evidence()}

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    out = service.generate({"workingCopy": str(tmp_path), "parse": False})

    assert out["created"] == 1
    assert out["reused"] == 0
    assert out["duplicates_dropped"] == 1
    assert len(out["cases"]) == 1


def test_change_service_rejects_working_copy_drift_before_persist(
        tmp_path, db_conn, fake_zentao, fake_llm):
    (tmp_path / "src").mkdir()
    tracked = tmp_path / "src" / "a.cpp"
    tracked.write_text("before", encoding="utf-8")
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = []

    class ContentAwareSvn(StubSvn):
        def diff_digest(self, working_copy, *, from_revision=None, to_revision=None):
            raw = tracked.read_bytes()
            return {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "from_revision": from_revision,
                "to_revision": to_revision,
            }

    def agent_runner(**_kwargs):
        tracked.write_text("after", encoding="utf-8")
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": _agent_evidence(),
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = ContentAwareSvn(tmp_path)
    progress = []
    service.progress = progress.append
    with pytest.raises(TestPilotError, match="已变化"):
        service.generate({"workingCopy": str(tmp_path), "parse": False})

    assert progress[-1]["phase"] == "snapshot_changed"
    assert not [
        row for row in repos.list_cases(db_conn)
        if (row.get("raw") or {}).get("source") == "svn_agent"
    ]


def test_change_service_rejects_agent_read_drift_for_fixed_revision(
        tmp_path, db_conn, fake_zentao, fake_llm):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "RunController.cpp"
    source.write_text("before", encoding="utf-8")
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["related_case_ids"] = []

    def agent_runner(**_kwargs):
        source.write_text("after", encoding="utf-8")
        evidence = _agent_evidence("src/RunController.cpp")
        evidence["read_hashes"] = {"src/RunController.cpp": before_hash}
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 2,
            "evidence": evidence,
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(TestPilotError, match="已观察源码在分析期间变化"):
        service.generate({
            "workingCopy": str(tmp_path),
            "fromRevision": "40",
            "toRevision": "42",
            "parse": False,
        })

    assert not [
        row for row in repos.list_cases(db_conn)
        if (row.get("raw") or {}).get("source") == "svn_agent"
    ]


def test_change_service_rejects_grep_observation_drift_for_fixed_revision(
        tmp_path, db_conn, fake_zentao, fake_llm):
    (tmp_path / "src").mkdir()
    helper = tmp_path / "src" / "helper.cpp"
    helper.write_text("before", encoding="utf-8")
    before_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = []

    def agent_runner(**_kwargs):
        helper.write_text("after", encoding="utf-8")
        evidence = _agent_evidence()
        evidence["content_hashes"] = {"src/helper.cpp": before_hash}
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 2,
            "evidence": evidence,
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(TestPilotError, match="已观察源码在分析期间变化"):
        service.generate({
            "workingCopy": str(tmp_path),
            "fromRevision": "40",
            "toRevision": "42",
            "parse": False,
        })


def test_snapshot_guard_rejects_oversized_unversioned_content(
        tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "new.txt").write_text("1234", encoding="utf-8")
    monkeypatch.setattr(change_service_mod, "_SNAPSHOT_UNVERSIONED_BYTE_LIMIT", 3)

    def should_not_run(**_kwargs):
        raise AssertionError("Agent 不应在快照预算失败后启动")

    service = ChangeAnalysisService(None, None, {"svn": {}}, should_not_run)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(TestPilotError, match="快照保护预算"):
        service.generate({"workingCopy": str(tmp_path), "parse": False})


def test_change_service_requires_main_rpc_diff_evidence_before_persist(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = []

    def agent_runner(**_kwargs):
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": {
                "calls": {"agent.svn.diff": 1},
                "successes": {},
                "failures": {"agent.svn.diff": 1},
                "read_paths": [],
            },
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(AgentError, match="成功读取 SVN diff"):
        service.generate({"workingCopy": str(tmp_path), "parse": False})

    assert not [
        row for row in repos.list_cases(db_conn)
        if (row.get("raw") or {}).get("source") == "svn_agent"
    ]


def test_change_service_preflights_all_source_and_related_evidence_before_persist(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"] = [
        {
            **payload["cases"][0],
            "title": "合法变更路径",
            "source_files": ["src/a.cpp"],
            "related_case_ids": [],
        },
        {
            **payload["cases"][0],
            "title": "幻觉来源",
            "source_files": ["src/not-real.cpp"],
            "related_case_ids": ["TC-NOT-FOUND"],
        },
    ]

    def agent_runner(**_kwargs):
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": _agent_evidence(),
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(AgentError, match="生成依据校验失败") as exc:
        service.generate({"workingCopy": str(tmp_path), "parse": False})
    assert "src/not-real.cpp" in str(exc.value)
    assert "TC-NOT-FOUND" in str(exc.value)

    # 所有依据在写库前一次性校验，不能留下“前一条已落库”的半提交状态。
    assert not [
        row for row in repos.list_cases(db_conn)
        if (row.get("raw") or {}).get("source") == "svn_agent"
    ]


def test_change_service_requires_followup_when_full_diff_is_truncated(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = []
    evidence = _agent_evidence()
    evidence["full_diff_complete"] = False
    evidence["full_diff_truncated"] = True

    def agent_runner(**_kwargs):
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": evidence,
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(AgentError, match="全量 SVN diff 已截断"):
        service.generate({"workingCopy": str(tmp_path), "parse": False})

    evidence["diff_paths"] = ["src/a.cpp"]
    out = service.generate({"workingCopy": str(tmp_path), "parse": False})
    assert out["created"] == 1


def test_change_service_requires_read_for_unversioned_source(
        tmp_path, db_conn, fake_zentao, fake_llm):
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "new.txt").write_text("new", encoding="utf-8")
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["tmp/new.txt"]
    payload["cases"][0]["related_case_ids"] = []

    def runner_without_read(**_kwargs):
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": _agent_evidence(),
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, runner_without_read)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(AgentError, match="必须先 agent.fs.read"):
        service.generate({"workingCopy": str(tmp_path), "parse": False})

    service.agent_runner = lambda **_kwargs: {
        "text": json.dumps(payload, ensure_ascii=False),
        "tool_calls": 2,
        "evidence": _agent_evidence("tmp/new.txt"),
    }
    out = service.generate({"workingCopy": str(tmp_path), "parse": False})
    assert out["created"] == 1


def test_change_service_rejects_unobserved_related_case_even_when_it_exists(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    repos.create_case(
        db_conn, case_id="TC-EXISTS", zentao_case_id=None,
        title="真实已有用例", raw={"steps_text": "existing"},
        zentao_updated_at=None)
    payload = json.loads(_agent_json())
    payload["cases"][0]["source_files"] = ["src/a.cpp"]
    payload["cases"][0]["related_case_ids"] = ["TC-EXISTS"]

    def agent_runner(**_kwargs):
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "tool_calls": 1,
            "evidence": _agent_evidence(),
        }

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    with pytest.raises(AgentError, match="agent.case.search/detail") as exc:
        service.generate({"workingCopy": str(tmp_path), "parse": False})
    assert "TC-EXISTS" in str(exc.value)


def test_change_service_preserves_audit_when_agent_output_is_invalid(
        tmp_path, db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    audit = [{
        "seq": 1, "method": "agent.svn.diff", "params": {},
        "ok": True, "elapsed_ms": 2,
        "result": {"chars": 12, "truncated": False},
    }]

    def agent_runner(**_kwargs):
        return {"text": "not json", "tool_calls": 1, "audit": audit}

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    progress = []
    service.progress = progress.append

    with pytest.raises(AgentError):
        service.generate({"workingCopy": str(tmp_path), "parse": False})

    last = progress[-1]
    assert last["phase"] == "agent_output_invalid"
    assert last["agent_audit"] == audit
    assert "JSON" in last["agent_error"]


def test_change_service_writes_cancelled_agent_diagnostics_before_abort_recheck():
    service = ChangeAnalysisService(None, None, {"svn": {}}, lambda **_kwargs: None)
    events = []

    class Requested:
        @staticmethod
        def check():
            raise AbortRequested("cancelled")

    service.abort = Requested()
    service.progress = events.append
    service._agent_progress({
        "phase": "agent_cancelled",
        "agent_audit": [{"seq": 1, "method": "agent.svn.diff", "ok": True}],
    })

    assert events[-1]["phase"] == "agent_cancelled"
    assert events[-1]["agent_audit"][0]["method"] == "agent.svn.diff"


def test_change_service_rejects_loose_generation_options(tmp_path):
    service = ChangeAnalysisService(None, None, {"svn": {}}, lambda **_kwargs: None)
    service.svn = StubSvn(tmp_path)

    with pytest.raises(TestPilotError, match="parse"):
        service.generate({"workingCopy": str(tmp_path), "parse": "false"})
    with pytest.raises(TestPilotError, match="maxCases"):
        service.generate({"workingCopy": str(tmp_path), "maxCases": True})
    with pytest.raises(TestPilotError, match="maxCases"):
        service.generate({"workingCopy": str(tmp_path), "maxCases": 1.5})
    with pytest.raises(TestPilotError, match=">= 1"):
        service.generate({"workingCopy": str(tmp_path), "maxCases": 0})


def test_change_service_rejects_invalid_agent_output():
    with pytest.raises(AgentError):
        _parse_analysis('{"summary":"x","cases":[]}', max_cases=20)
    with pytest.raises(AgentError):
        _parse_analysis("not json", max_cases=20)


def test_change_service_rejects_unbounded_agent_metadata():
    bad_category = json.loads(_agent_json())
    bad_category["cases"][0]["category"] = "whatever"
    with pytest.raises(AgentError, match="输出结构无效"):
        _parse_analysis(json.dumps(bad_category), max_cases=20)

    escaped_path = json.loads(_agent_json())
    escaped_path["cases"][0]["source_files"] = ["../outside.cpp"]
    with pytest.raises(AgentError, match="输出结构无效"):
        _parse_analysis(json.dumps(escaped_path), max_cases=20)


def test_change_prompt_merges_tracked_and_status_only_paths():
    prompt = _build_prompt({
        "working_copy": "D:/wc",
        "from_revision": None,
        "to_revision": None,
        "changed": [{"status": "M", "path": "src/a.cpp"}],
        "working_status": [
            {"item": "modified", "path": "src/a.cpp"},
            {"item": "unversioned", "path": "src/new.cpp"},
        ],
    }, max_cases=5)
    assert prompt.count("src/a.cpp") == 1
    assert "src/new.cpp" in prompt
    assert "unversioned" in prompt


def test_pi_runtime_websocket_env_is_version_gated(monkeypatch):
    env = {}
    monkeypatch.setattr(PiAgentRuntime, "_node_version", staticmethod(lambda: (20, 10)))
    PiAgentRuntime._enable_native_websocket(env)
    assert "--experimental-websocket" in env["NODE_OPTIONS"]

    env = {}
    monkeypatch.setattr(PiAgentRuntime, "_node_version", staticmethod(lambda: (21, 7)))
    PiAgentRuntime._enable_native_websocket(env)
    assert "--experimental-websocket" in env["NODE_OPTIONS"]

    for version in ((20, 9), (22, 0), (24, 1)):
        env = {}
        monkeypatch.setattr(PiAgentRuntime, "_node_version",
                            staticmethod(lambda v=version: v))
        PiAgentRuntime._enable_native_websocket(env)
        assert "NODE_OPTIONS" not in env

    env = {"NODE_OPTIONS": "--no-experimental-websocket"}
    monkeypatch.setattr(PiAgentRuntime, "_node_version", staticmethod(lambda: (20, 10)))
    PiAgentRuntime._enable_native_websocket(env)
    assert env["NODE_OPTIONS"] == "--no-experimental-websocket"


def test_pi_runtime_builds_only_supported_safe_flags(tmp_path):
    extension = tmp_path / "tool.mjs"
    extension.write_text("export default () => {}", encoding="utf-8")
    runtime = PiAgentRuntime({}, rpc_url="ws://127.0.0.1:9301", rpc_token="t")

    runtime._help_cache = """
      --mode <mode>
      --no-session
      --no-builtin-tools
      --no-skills
      --no-prompt-templates
      --no-extensions
      --extension <source>
    """
    command = runtime._command("pi", extension)
    assert "--no-builtin-tools" in command
    assert "--no-tools" not in command
    assert "--no-skills" in command
    assert "--no-mcp" not in command
    assert command[-2:] == ["--extension", str(extension)]

    runtime._help_cache = """
      --mode <mode>
      --no-session
      --no-tools
      --no-skills
      --extension <source>
    """
    command = runtime._command("pi", extension)
    assert "--no-tools" in command
    assert "--no-builtin-tools" not in command

    runtime._help_cache = "--mode <mode> --no-session --extension <source>"
    with pytest.raises(AgentError, match="不支持禁用内置工具"):
        runtime._command("pi", extension)


def test_pi_extension_has_no_native_file_or_shell_tools():
    source = (Path(__file__).resolve().parents[1]
              / "testpilot" / "agent" / "pi_rpc_tools.mjs").read_text(encoding="utf-8")
    assert "agent.fs.read" in source
    assert "agent.svn.diff" in source
    assert "node:fs" not in source
    assert "node:child_process" not in source
    assert "undici" not in source
    assert 'from "ws"' not in source
    assert "globalThis.WebSocket" in source
    assert "@sinclair/typebox" in source
    assert "return ENABLED.has(name)" in source
    assert "ENABLED.size === 0 ||" not in source
    assert "exec(" not in source
    runtime = (Path(__file__).resolve().parents[1]
               / "testpilot" / "agent" / "pi_runtime.py").read_text(encoding="utf-8")
    assert '"--no-builtin-tools"' in runtime
    assert '"--no-mcp"' in runtime


def test_agent_rpc_rejects_empty_queries_and_loose_diff_params(tmp_path, rpc_svc):
    scope = {"workspace": str(tmp_path.resolve())}

    with pytest.raises(RpcError, match="find pattern"):
        rpc_svc.dispatch("agent.fs.find", {
            "_agentScope": scope, "pattern": "   ",
        })
    with pytest.raises(RpcError, match="grep 查询不能为空"):
        rpc_svc.dispatch("agent.fs.grep", {
            "_agentScope": scope, "query": "   ",
        })
    with pytest.raises(RpcError, match="搜索关键词不能为空"):
        rpc_svc.dispatch("agent.case.search", {
            "_agentScope": scope, "query": "   ",
        })
    with pytest.raises(RpcError, match="max_chars"):
        rpc_svc.dispatch("agent.svn.diff", {
            "_agentScope": scope, "max_chars": True,
        })
    with pytest.raises(RpcError, match="max_chars"):
        rpc_svc.dispatch("agent.svn.diff", {
            "_agentScope": scope, "max_chars": 12_000.5,
        })
    with pytest.raises(RpcError, match=">= 10000"):
        rpc_svc.dispatch("agent.svn.diff", {
            "_agentScope": scope, "max_chars": 9_999,
        })
    with pytest.raises(RpcError, match="非空字符串路径"):
        rpc_svc.dispatch("agent.svn.diff", {
            "_agentScope": scope, "paths": [123],
        })


def test_agent_svn_diff_enforces_model_context_cap(tmp_path, rpc_svc, monkeypatch):
    captured = {}

    def fake_diff(_self, _working_copy, **kwargs):
        captured.update(kwargs)
        return {"text": "", "truncated": False, "chars": 0, "paths": []}

    monkeypatch.setattr(SvnAdapter, "diff", fake_diff)
    out = rpc_svc.dispatch("agent.svn.diff", {
        "_agentScope": {"workspace": str(tmp_path.resolve())},
        "max_chars": 999_999,
    })
    assert captured["max_chars"] == 200_000
    assert out["agent_limit_chars"] == 200_000


def test_agent_rpc_scope_allows_only_agent_methods_and_workspace(tmp_path, rpc_svc):
    service = rpc_svc
    inside = tmp_path / "src.txt"
    inside.write_text("hello\nworld", encoding="utf-8")
    scope = {"workspace": str(tmp_path.resolve())}

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "agent.fs.read",
        "params": {"path": "src.txt"},
    })
    reply = asyncio.run(service._handle_raw(request, agent_scope=scope))
    assert reply["result"]["content"].startswith("1: hello")

    escape = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "agent.fs.read",
        "params": {"path": "../outside.txt"},
    })
    reply = asyncio.run(service._handle_raw(escape, agent_scope=scope))
    assert "error" in reply

    forbidden = json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "task.run",
        "params": {"taskId": 1},
    })
    reply = asyncio.run(service._handle_raw(forbidden, agent_scope=scope))
    assert reply["error"]["code"] == -32601


def test_agent_capability_token_is_scoped_and_revocable(tmp_path, rpc_svc):
    service = rpc_svc
    token = service.issue_agent_token(tmp_path)
    expected = str(tmp_path.resolve())
    assert service._agent_connection_scope("Bearer " + token)["workspace"] == expected
    assert service._agent_connection_scope("", "/?agent_token=" + token)["workspace"] == expected
    service.revoke_agent_token(token)
    assert service._agent_connection_scope("Bearer " + token) is None
    assert service._agent_connection_scope("", "/?agent_token=" + token) is None


def test_agent_rpc_records_structured_audit_without_source_body(tmp_path, rpc_svc):
    service = rpc_svc
    source = tmp_path / "src.txt"
    source.write_text("hello\nworld", encoding="utf-8")
    token = service.issue_agent_token(tmp_path)
    scope = service._agent_connection_scope("Bearer " + token)
    assert scope is not None

    read = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "agent.fs.read",
        "params": {"path": "src.txt", "limit": 1},
    })
    reply = asyncio.run(service._handle_raw(read, agent_scope=scope))
    assert reply["result"]["content"] == "1: hello"
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert reply["result"]["file_sha256"] == expected_hash

    escaped = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "agent.fs.read",
        "params": {"path": "../outside.txt"},
    })
    reply = asyncio.run(service._handle_raw(escaped, agent_scope=scope))
    assert "error" in reply

    audit = service.agent_audit(token)
    assert len(audit) == 2
    assert audit[0]["method"] == "agent.fs.read"
    assert audit[0]["params"] == {"path": "src.txt", "limit": 1}
    assert audit[0]["ok"] is True
    assert audit[0]["result"]["lines"] == 1
    assert "content" not in audit[0]["result"]
    assert audit[1]["ok"] is False
    assert "工作区" in audit[1]["error"]
    evidence = service.agent_evidence(token)
    assert evidence["read_hashes"]["src.txt"] == expected_hash
    assert evidence["content_hashes"]["src.txt"] == expected_hash

    service.revoke_agent_token(token)
    assert service.agent_audit(token) == []


def test_agent_grep_records_hash_for_observed_file(tmp_path, rpc_svc):
    service = rpc_svc
    source = tmp_path / "helper.txt"
    source.write_text("needle here\nother", encoding="utf-8")
    token = service.issue_agent_token(tmp_path)
    scope = service._agent_connection_scope("Bearer " + token)
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "agent.fs.grep",
        "params": {"query": "needle", "glob": "*.txt"},
    })
    reply = asyncio.run(service._handle_raw(request, agent_scope=scope))
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert reply["result"]["file_hashes"]["helper.txt"] == expected
    evidence = service.agent_evidence(token)
    assert evidence["content_hashes"]["helper.txt"] == expected
    service.revoke_agent_token(token)


def test_agent_evidence_survives_display_audit_trimming(tmp_path, rpc_svc):
    service = rpc_svc
    token = service.issue_agent_token(tmp_path)
    scope = service._agent_connection_scope("Bearer " + token)
    assert scope is not None

    service._record_agent_audit(
        scope, "agent.svn.diff", {},
        result={"chars": 10, "paths": [], "truncated": False},
        elapsed_ms=1)
    service._record_agent_audit(
        scope, "agent.case.search", {"query": "TC-1"},
        result={"cases": [{"case_id": "TC-1"}], "count": 1, "truncated": False},
        elapsed_ms=1)
    for index in range(205):
        service._record_agent_audit(
            scope, "agent.fs.read", {"path": f"src/{index}.cpp"},
            result={"path": f"src/{index}.cpp", "lines": 1, "total_lines": 1},
            elapsed_ms=1)

    audit = service.agent_audit(token)
    evidence = service.agent_evidence(token)
    assert len(audit) == 200
    assert all(item["method"] not in ("agent.svn.diff", "agent.case.search")
               for item in audit)
    assert evidence["successes"]["agent.svn.diff"] == 1
    assert evidence["successes"]["agent.case.search"] == 1
    assert evidence["successes"]["agent.fs.read"] == 205
    assert evidence["read_paths"][0] == "src/0.cpp"
    assert "src/204.cpp" in evidence["read_paths"]
    assert evidence["case_ids"] == ["TC-1"]
    assert evidence["full_diff_complete"] is True
    assert evidence["full_diff_truncated"] is False
    assert evidence["diff_paths"] == []

    service.revoke_agent_token(token)
    assert service.agent_evidence(token) == {}


def test_pi_runner_preserves_audit_in_failed_job_progress(
        tmp_path, rpc_svc, monkeypatch):
    service = rpc_svc
    events = []
    revoked = []

    monkeypatch.setattr(service, "issue_agent_token", lambda _workspace: "agent-token")
    monkeypatch.setattr(service, "agent_audit", lambda _token: [{
        "seq": 1,
        "method": "agent.fs.read",
        "params": {"path": "src/a.cpp"},
        "ok": True,
        "elapsed_ms": 4,
        "result": {"path": "src/a.cpp", "lines": 20},
    }])
    monkeypatch.setattr(service, "agent_evidence", lambda _token: {
        "calls": {"agent.fs.read": 1},
        "successes": {"agent.fs.read": 1},
        "failures": {},
        "read_paths": ["src/a.cpp"],
        "read_paths_truncated": False,
        "read_hashes": {},
        "content_hashes": {},
        "content_hashes_truncated": False,
        "case_ids": [],
        "case_ids_truncated": False,
        "diff_paths": [],
        "diff_paths_truncated": False,
        "full_diff_complete": False,
        "full_diff_truncated": False,
    })
    monkeypatch.setattr(service, "revoke_agent_token", revoked.append)

    def fail_run(self, *args, **kwargs):
        raise AgentError("provider unavailable")

    monkeypatch.setattr(PiAgentRuntime, "run", fail_run)

    with pytest.raises(AgentError, match="provider unavailable"):
        service._run_pi_agent(
            prompt="inspect",
            workspace=str(tmp_path),
            tools=["tp_read_file"],
            progress=events.append,
        )

    assert events[-1]["phase"] == "agent_failed"
    assert events[-1]["agent_error"] == "provider unavailable"
    assert events[-1]["agent_audit"][0]["method"] == "agent.fs.read"
    assert events[-1]["agent_evidence"]["read_paths"] == ["src/a.cpp"]
    assert revoked == ["agent-token"]

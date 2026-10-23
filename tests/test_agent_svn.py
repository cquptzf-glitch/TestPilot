"""SVN change analysis + Pi RPC-only tool boundary tests.

No real SVN/Pi process is required. These tests pin the main-process trust boundary so a
future refactor cannot accidentally give the agent native file/shell/database access.
"""
import asyncio
import json
from pathlib import Path

import pytest

from testpilot.adapters.svn_adapter import SvnAdapter, _revision_args
from testpilot.agent.change_service import ChangeAnalysisService, _build_prompt, _parse_analysis
from testpilot.agent.pi_runtime import PiAgentRuntime
from testpilot.case_parser import CaseService
from testpilot.core.errors import AgentError, SvnError
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
            "audit": [{
                "seq": 1, "method": "agent.svn.diff", "params": {},
                "ok": True, "elapsed_ms": 3,
                "result": {"chars": 10, "truncated": False, "path_count": 1},
            }],
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
    payload["cases"].append(dict(payload["cases"][0]))

    def agent_runner(**_kwargs):
        return {"text": json.dumps(payload, ensure_ascii=False),
                "tool_calls": 2, "elapsed_ms": 10}

    service = ChangeAnalysisService(db_conn, cases, {"svn": {}}, agent_runner)
    service.svn = StubSvn(tmp_path)
    out = service.generate({"workingCopy": str(tmp_path), "parse": False})

    assert out["created"] == 1
    assert out["reused"] == 0
    assert out["duplicates_dropped"] == 1
    assert len(out["cases"]) == 1


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

    service.revoke_agent_token(token)
    assert service.agent_audit(token) == []



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
    assert revoked == ["agent-token"]

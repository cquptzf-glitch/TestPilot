"""Regression checks for the 2026-10-15 review: decisions, boundaries and evidence."""
import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from testpilot.core.contracts import ExecContext, StepResult
from testpilot.core.dsl import Step, TestCase as DslCase, validate_execution
from testpilot.core.errors import TestPilotError as PilotError, RpcError
from testpilot.engine.verifier import Verifier
from testpilot.knowledge.signature import FailureSignature
from testpilot.knowledge.service import KnowledgeService
from testpilot.rpc import RpcService
from testpilot.storage import repos
from testpilot.storage.db import recover_orphaned_runs
from tests.conftest import prepare_case, FakeEmulator
from tests.test_executor import Env, _start


@pytest.mark.parametrize("action", ["assert.state", "assert.ui", "assert.vision"])
def test_incomplete_assertions_cannot_be_approved(mem_conn, action):
    from testpilot.case_parser import CaseService
    service = CaseService(mem_conn, None, None)
    service.import_draft(json.dumps({"case_id": "TC-1", "steps": [
        {"id": "s", "action": action}]}))
    with pytest.raises(PilotError, match="DSL"):
        service.accept("TC-1")


def test_duplicate_step_ids_rejected():
    with pytest.raises(ValueError, match="重复"):
        validate_execution(DslCase(case_id="TC-1", steps=[
            Step(id="s", action="sleep"), Step(id="s", action="sleep")]))


def test_device_action_runs_its_verifier(cfg, db_conn, tmp_path):
    env = Env(cfg, db_conn, tmp_path)
    repos.create_task_record(db_conn, name="t", case_ids=["TC-1"])
    case = DslCase(case_id="TC-1", steps=[Step(
        id="s", action="device.scene",
        args={"scene_id": "a", "path": "/state/run", "expect": "different"},
        verify={"type": "state", "timeout_ms": 1})])
    _, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].verify.status == "fail"
    assert repos.case_steps(db_conn, ce)[0]["status"] == "fail"


def test_state_assertion_waits_until_ready():
    emulator = Mock(spec=["query_state"])
    emulator.query_state.side_effect = ["booting", "ready"]
    step = Step(id="s", action="assert.state",
                args={"path": "/x", "expect": "ready"},
                verify={"type": "state", "timeout_ms": 1000})
    result = Verifier({}, emulator, None, {}).verify(step, None, None, None)
    assert result.status == "pass"
    assert emulator.query_state.call_count == 2


def test_visual_request_uses_visual_endpoint(monkeypatch):
    from testpilot.adapters.llm_gateway import LlmGateway
    reply = Mock(status_code=200)
    reply.json.return_value = {"choices": [{"message": {
        "content": '{"bbox":[1,2,3,4],"confidence":0.9}'}}]}
    post = Mock(return_value=reply)
    monkeypatch.setattr("httpx.post", post)
    monkeypatch.setenv("AUDIT_VLM_KEY", "visual-key")
    gateway = LlmGateway({"api_base": "http://text.invalid/v1", "model": "text",
                          "vision": {"api_base": "http://vision.invalid/v1",
                                     "model": "visual", "api_key_env": "AUDIT_VLM_KEY",
                                     "timeout_s": 7}})
    gateway.ground("b64", "button")
    assert post.call_args.args[0] == "http://vision.invalid/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "visual"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer visual-key"
    assert 0 < post.call_args.kwargs["timeout"] <= 7


def test_device_commands_have_distinct_failure_signatures():
    signatures = []
    for cmd in ("load_scene", "inject_error"):
        case = DslCase(case_id="TC-1", steps=[Step(id="s", action="device.call",
                                                  args={"cmd": cmd})])
        ctx = ExecContext(1, 1, case=case)
        res = StepResult(1, "s", "device.call", "fail", error={"type": "device_error"})
        signatures.append(FailureSignature.from_result(res, ctx).digest())
    assert len(set(signatures)) == 2


def test_failed_scene_blocks_all_case_actions(cfg, db_conn, tmp_path):
    env = Env(cfg, db_conn, tmp_path)
    env.emulator = FakeEmulator(fail=True)
    env.rebuild()
    repos.create_task_record(db_conn, name="t", case_ids=["TC-1"])
    case = DslCase(case_id="TC-1", preconditions=["scene:a"],
                   steps=[Step(id="s", action="sleep", args={"ms": 0})])
    _, _, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "blocked"
    assert result.steps == []


def test_run_freezes_second_case_before_first_starts(orch_factory, db_conn, monkeypatch):
    orch = orch_factory()
    prepare_case(orch, (1, 2))
    task = orch.create_task({"name": "frozen", "case_ids": ["TC-1", "TC-2"]})
    original = orch.executor.run_case
    versions = []
    def run(case, ctx, hints):
        versions.append(case.version)
        if case.case_id == "TC-1":
            orch.cases.import_draft(json.dumps({"case_id": "TC-2", "steps": [
                {"id": "changed", "action": "sleep", "args": {"ms": 0}}]}))
            orch.cases.accept("TC-2")
        return original(case, ctx, hints)
    monkeypatch.setattr(orch.executor, "run_case", run)
    stats = orch.run_task(task["task_id"], writeback=False)
    detail = repos.run_detail(db_conn, stats["run_id"])
    assert versions == [1, 1]
    assert detail["env"]["dsl_versions"]["TC-2"] == 1
    assert orch.cases.to_testcase("TC-2").version == 2
    row = db_conn.execute("SELECT dsl_snapshot_json FROM case_exec "
                          "WHERE case_id='TC-2'").fetchone()
    assert json.loads(row[0])["version"] == 1


def test_orphan_recovery_preserves_live_owner(orch_factory, db_conn, monkeypatch):
    orch = orch_factory()
    prepare_case(orch)
    task = orch.create_task({"name": "recover", "case_ids": ["TC-1"]})
    dead = orch.recorder.begin_run({"task_id": task["task_id"]})
    live = orch.recorder.begin_run({"task_id": task["task_id"]})
    db_conn.execute("UPDATE test_run SET owner_session='999999:dead' WHERE id=?", (dead,))
    db_conn.commit()
    monkeypatch.setattr("testpilot.storage.db._process_alive", lambda pid: pid != 999999)
    assert recover_orphaned_runs(db_conn) == 1
    assert repos.run_detail(db_conn, dead)["status"] == "interrupted"
    assert repos.run_detail(db_conn, live)["status"] == "running"


def test_io_requests_use_an_independent_connection(orch_factory):
    orch = orch_factory()
    rpc = RpcService(orch)
    try:
        rpc._dispatch_io("case.list", {})
        assert rpc._io_orchestrator is not None
        assert rpc._io_orchestrator.conn is not orch.conn
        orch.conn.execute("BEGIN")
        rpc._io_orchestrator.conn.commit()
        assert orch.conn.in_transaction
        orch.conn.rollback()
    finally:
        rpc.close()


def test_accept_saves_current_text_and_checks_revision(orch_factory):
    orch = orch_factory()
    prepare_case(orch)
    rpc = RpcService(orch)
    try:
        revision = orch.cases.show("TC-1")["revision"]
        text = json.dumps({"case_id": "TC-1", "steps": [
            {"id": "new", "action": "sleep", "args": {"ms": 0}}]})
        rpc.dispatch("dsl.accept", {"caseId": "TC-1", "text": text,
                                     "expectedRevision": revision})
        assert orch.cases.to_testcase("TC-1").steps[0].id == "new"
        with pytest.raises(RpcError, match="变更"):
            rpc.dispatch("dsl.accept", {"caseId": "TC-1", "text": text,
                                       "expectedRevision": revision})
    finally:
        rpc.close()


def test_env_matrix_keeps_distinct_conditions(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    for n, mode in enumerate(("uia_only", "vision_only"), 1):
        kb.record_env_compat(run_id=n, test_app_version="1", suite_id=1,
                            case_ids=["TC-1"], statuses={"TC-1": "pass"},
                            condition={"driver_mode": mode})
    assert len(kb.env_compat_matrix(suite_id=1)) == 2


def test_rpc_requires_token_and_rejects_foreign_origin():
    import websockets
    async def check():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        rpc = RpcService(SimpleNamespace(health=lambda: {"ok": True}), token="test-key")
        server = asyncio.create_task(rpc.serve("127.0.0.1", port))
        try:
            for _ in range(100):
                if rpc._stop is not None:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            url = f"ws://127.0.0.1:{port}"
            for headers, origin in (({}, None),
                                    ({"Authorization": "Bearer test-key"}, "https://untrusted.invalid")):
                with pytest.raises(websockets.exceptions.InvalidStatus):
                    async with websockets.connect(url, additional_headers=headers, origin=origin):
                        pass
            async with websockets.connect(url, additional_headers={
                    "Authorization": "Bearer test-key"}) as ws:
                await ws.send(json.dumps({"id": 1, "method": "health"}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                assert reply["result"]["ok"] is True
        finally:
            if rpc._stop:
                rpc._stop.set()
            await server
            rpc.close()
    asyncio.run(check())


def test_pending_remote_create_is_recovered(orch_factory):
    from tests.conftest import FakeZentao
    zentao = FakeZentao(configured=True, fail_create=True)
    orch = orch_factory(zentao=zentao)
    prepare_case(orch)
    task = orch.create_task({"name": "remote", "case_ids": ["TC-1"], "remote": True})
    assert task["sync_status"] == "sync_pending"
    zentao.fail_create = False
    orch.sync_pending_tasks(task["task_id"])
    assert orch.task_detail(task["task_id"])["zentao_task_id"] is not None


def test_rpc_port_override_is_shared_by_shell_and_engine(tmp_path):
    from testpilot.config.settings import load_settings
    cfg = load_settings(env={"TESTPILOT_RPC_PORT": "19321",
                             "TESTPILOT_NO_OVERRIDE": "1"})
    assert cfg["rpc"]["port"] == 19321
    with pytest.raises(ValueError, match="TESTPILOT_RPC_PORT"):
        load_settings(env={"TESTPILOT_RPC_PORT": "99999",
                           "TESTPILOT_NO_OVERRIDE": "1"})

"""Regression tests for the audited AI/ZenTao boundaries; no live provider calls."""
import base64
import json
import threading
import time

import httpx
import pytest

from testpilot.adapters.llm_gateway import LlmGateway
from testpilot.adapters.zentao_adapter import ZentaoAdapter, ZentaoHttp, _zentao_ids
from testpilot.case_parser import CaseService
from testpilot.core.contracts import ExecContext, Screenshot, StepResult
from testpilot.core.dsl import TestCase as DslCase, VerifySpec
from testpilot.core.errors import AbortRequested, LlmError, ZentaoError
from testpilot.engine.judge import Judge
from testpilot.engine.verifier import Verifier
from testpilot.infra.abort import AbortSignal
from testpilot.infra.jobs import CaseJobs
from testpilot.knowledge.signature import FailureSignature
from testpilot.storage import repos
from tests.conftest import FakeLlm, FakeZentao, PNG_1PX


@pytest.mark.parametrize("bad", ["NaN", "Infinity", -0.1, 1.1, True, "not-a-number"])
def test_invalid_model_confidence_never_passes(bad):
    gateway = LlmGateway({}, transport=lambda *_: (
        json.dumps({"pass": True, "confidence": bad}), {}))
    result = Verifier({}, None, gateway, {})._verify_vision(
        VerifySpec(type="vision", expect="a"), Screenshot(PNG_1PX))
    assert result.status == "unknown"


@pytest.mark.parametrize("bad", [[], {}, 1, "yes", None])
def test_invalid_model_boolean_is_rejected(bad):
    gateway = LlmGateway({}, transport=lambda *_: (
        json.dumps({"pass": bad, "confidence": 0.99}), {}))
    with pytest.raises(LlmError):
        gateway.verify_vision("image", "a")


def test_normalized_grounding_uses_actual_image_size():
    import struct
    image = PNG_1PX[:16] + struct.pack(">II", 800, 480) + PNG_1PX[24:]
    gateway = LlmGateway({"vision": {"coordinate_space": "normalized_1000"}},
                         transport=lambda *_: ('{"bbox":[625,708,275,167],"confidence":0.9}', {}))
    result = gateway.ground(base64.b64encode(image).decode(), "Export")
    assert result["bbox"] == [500, 340, 220, 80]


def test_source_and_missing_assertion_are_flagged():
    payload = {"steps": [{"id": "s1", "action": "sleep", "args": {"ms": 0}}]}
    gateway = LlmGateway({}, transport=lambda *_: (json.dumps(payload), {}))
    draft = gateway.parse_case({"steps": [{"desc": "check", "expect": "42.5"}]})
    assert draft["uncertain_count"] == 1
    assert "来源" in draft["notes"]
    payload["steps"][0]["source_text"] = "check"
    draft = gateway.parse_case({"steps": [{"desc": "check", "expect": "42.5"}]})
    assert draft["uncertain_count"] == 1
    assert "未验证" in draft["notes"]


def test_capability_failure_clears_previous_manifest(db_conn):
    llm = FakeLlm()
    llm.emulator_capabilities = {"commands": ["old"]}
    service = CaseService(db_conn, FakeZentao(), llm, {},
                          capabilities_provider=lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    service._inject_capabilities()
    assert llm.emulator_capabilities is None


def test_first_sync_parses_new_cases(db_conn):
    service = CaseService(db_conn, FakeZentao(), FakeLlm(), {})
    result = service.sync(parse=True)
    assert result["new"] == result["parse"]["parsed"] == 2
    assert all(row["steps"] for row in repos.list_cases(db_conn))


def test_remote_precondition_is_present_in_model_payload_and_draft(db_conn):
    class Http:
        def get(self, path, params=None):
            return {"id": 1, "title": "sample", "precondition": "LOAD_REQUIRED_SCENE",
                    "steps": [{"desc": "click", "expect": "done"}]}
    llm = FakeLlm()
    service = CaseService(db_conn, ZentaoAdapter({}, Http()), llm, {})
    service.pull([1], parse=True)
    assert llm.parse_calls[0]["preconditions"] == ["LOAD_REQUIRED_SCENE"]
    assert "LOAD_REQUIRED_SCENE" in service.show("TC-1")["preconditions"]


def test_list_summary_is_hydrated_and_detail_failure_does_not_succeed():
    class Http:
        def __init__(self):
            self.fail = False
            self.calls = []
        def get(self, path, params=None):
            self.calls.append(path)
            if "/products/" in path and path.endswith("testcases"):
                return {"total": 1, "testcases": [{"id": 1, "stepNumber": 1}]}
            if path.endswith("modules"):
                return {"modules": []}
            if self.fail:
                raise ZentaoError("detail unavailable")
            return {"id": 1, "steps": [{"desc": "click"}]}
    http = Http()
    adapter = ZentaoAdapter({"product_id": 1}, http)
    assert adapter.fetch_cases()[0]["steps_text"] == "1. click"
    assert "/api.php/v1/testcases/1" in http.calls
    http.fail = True
    with pytest.raises(ZentaoError):
        adapter.fetch_cases()


@pytest.mark.parametrize("payload", [{"status": "fail"}, {"error": "denied"}, {"success": False}])
def test_business_failure_is_not_http_success(payload):
    with httpx.Client(base_url="http://test.invalid", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ZentaoError, match="业务失败"):
            ZentaoHttp({}, client).get("/api.php/v1/testtasks/1")


def test_task_creation_preserves_id_and_requires_real_links():
    class Http:
        def post(self, path, json=None):
            return {"id": 4}
        def get(self, path, params=None):
            return {"id": 4, "cases": "1"}
    with pytest.raises(ZentaoError) as error:
        ZentaoAdapter({"build": "1"}, Http()).create_testtask(
            {"name": "task", "zentao_case_ids": [1]})
    assert error.value.task_id == 4
    assert "testcases" in str(error.value)


def test_mismatched_readback_keeps_created_id():
    class Http:
        def post(self, path, json=None):
            return {"id": 4}
        def get(self, path, params=None):
            return {"id": 99}
    with pytest.raises(ZentaoError) as error:
        ZentaoAdapter({"build": "1"}, Http()).create_testtask({"name": "task"})
    assert error.value.task_id == 4


@pytest.mark.parametrize("cid", ["QT-DESKTOP-001", "LOCAL-2026-17", "bad"])
def test_arbitrary_case_names_are_never_turned_into_remote_ids(cid):
    with pytest.raises(ZentaoError):
        _zentao_ids({"case_ids": [cid]})


def test_orchestrator_uses_explicit_database_mapping(orch_factory):
    zentao = FakeZentao(configured=True)
    orch = orch_factory(zentao=zentao)
    orch.cases.import_draft(json.dumps({"case_id": "LOCAL-17", "zentao_case_id": 901,
        "steps": [{"id": "s", "action": "sleep", "args": {"ms": 0}}]}))
    orch.cases.accept("LOCAL-17")
    task = orch.create_task({"name": "mapped", "case_ids": ["LOCAL-17"], "remote": True})
    assert task["zentao_task_id"]
    assert zentao.created[0]["zentao_case_ids"] == [901]


def test_model_images_are_bytes_in_request_not_just_paths(cfg, tmp_path):
    captured = []
    cfg["app"]["screenshot_root"] = str(tmp_path)
    (tmp_path / "after.png").write_bytes(PNG_1PX)
    gateway = LlmGateway({}, transport=lambda prompt, images: (
        captured.append((prompt, images)) or
        ('{"category":"product_bug","confidence":0.8,"evidence":"visible result","suggestion":"review"}', {})))
    result = Judge(gateway, None, cfg)._llm_pass(
        StepResult(1, "s", "assert.ui", "fail", screenshot_after="after.png"),
        FailureSignature("verify_failed", "assert.ui", "result", "main"),
        ExecContext(1, 1, case=DslCase(case_id="TC-1")))
    assert captured[0][1] == [base64.b64encode(PNG_1PX).decode()]
    assert "with_images" in captured[0][0]
    assert result.needs_review


def test_model_request_cancellation_is_prompt(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def post(*args, **kwargs):
        started.set()
        release.wait(3)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr(httpx, "post", post)
    gateway = LlmGateway({"api_base": "http://test.invalid", "timeout_s": 5})
    abort, errors = AbortSignal(), []
    def invoke():
        try:
            with gateway.request_context(abort=abort):
                gateway.parse_case({})
        except AbortRequested:
            errors.append("cancelled")
    worker = threading.Thread(target=invoke)
    worker.start()
    assert started.wait(1)
    abort.request()
    worker.join(0.5)
    release.set()
    assert not worker.is_alive()
    assert errors == ["cancelled"]


def test_case_jobs_cancel_and_keep_queryable_terminal_result():
    def run(method, params, abort, progress):
        progress({"phase": "parse", "case_id": "TC-1"})
        while True:
            abort.check()
            time.sleep(0.01)
    jobs = CaseJobs(run)
    try:
        first = jobs.start("case.parse", {"caseId": "TC-1"})
        deadline = time.monotonic() + 1
        while jobs.status(first["id"])["progress"] == {}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        jobs.cancel(first["id"])
        while jobs.status(first["id"])["status"] != "cancelled":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert jobs.status()[0]["id"] == first["id"]
        assert jobs.status(first["id"])["progress"]["case_id"] == "TC-1"
    finally:
        jobs.close()


def test_v2_is_rejected_before_network_calls():
    with pytest.raises(ZentaoError, match="v2"):
        ZentaoAdapter({"api_version": "v2"})


def test_uncertain_creation_requires_reconciliation_without_post():
    class Http:
        def post(self, *args, **kwargs):
            pytest.fail("must not repeat an uncertain POST")
    with pytest.raises(ZentaoError, match="不确定"):
        ZentaoAdapter({}, Http()).create_testtask({"creation_uncertain": True})


def test_native_xyxy_normalized_box_converts_to_pixel_width_height():
    import struct
    image = PNG_1PX[:16] + struct.pack(">II", 800, 480) + PNG_1PX[24:]
    gateway = LlmGateway({"vision": {"coordinate_space": "normalized_1000", "bbox_format": "xyxy"}},
        transport=lambda *_: ('{"bbox":[625,708,900,875],"confidence":0.95}', {}))
    assert gateway.ground(base64.b64encode(image).decode(), "Export")["bbox"] == [500, 340, 220, 80]


def test_whole_model_retry_budget_is_not_reset(monkeypatch):
    calls = []
    def overloaded(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return httpx.Response(503, text="busy")
    monkeypatch.setattr(httpx, "post", overloaded)
    gateway = LlmGateway({"api_base": "http://test.invalid", "timeout_s": 0.08})
    started = time.monotonic()
    with pytest.raises(LlmError, match="超时"):
        gateway.parse_case({})
    assert time.monotonic() - started < 0.4
    assert len(calls) == 1
    # timeout 是由 monotonic deadline 反算的剩余预算，浮点/调度会产生微小误差。
    assert 0 < calls[0] <= 0.08 + 1e-6


def test_human_edit_during_parse_is_not_overwritten(db_conn):
    llm = FakeLlm()
    service = CaseService(db_conn, FakeZentao(), llm, {})
    service.pull([1])
    def edit_then_return(raw):
        service.import_draft(json.dumps({"case_id": "TC-1", "notes": "human change",
            "steps": [{"id": "human", "action": "sleep", "args": {"ms": 0}}]}))
        return {"steps": [{"id": "old", "action": "sleep", "args": {"ms": 0}}]}
    llm.parse_case = edit_then_return
    with pytest.raises(Exception, match="解析期间已变更"):
        service.parse("TC-1")
    assert service.show("TC-1")["steps"][0]["id"] == "human"


def test_ignored_remote_update_is_not_marked_success():
    class Http:
        def get(self, path, params=None):
            return {"id": 1, "status": "wait", "desc": "original"}
        def put(self, path, json=None):
            return {"status": "success"}
    with pytest.raises(ZentaoError, match="回读不一致"):
        ZentaoAdapter({}, Http()).update_task_status(1, "done", {}, retries=1)


def test_readback_summary_retries_preserve_existing_description():
    class Http:
        def __init__(self):
            self.task = {"id": 1, "status": "wait", "desc": "Human description"}
            self.puts = 0
        def get(self, path, params=None):
            return dict(self.task)
        def put(self, path, json=None):
            self.puts += 1
            self.task.update(json)
            raise ZentaoError("Response lost after commit")
    http = Http()
    adapter = ZentaoAdapter({}, http)
    adapter.update_task_status(1, "done", {"run_id": 7}, retries=2)
    adapter.update_task_status(1, "done", {"run_id": 7}, retries=1)
    assert http.puts == 1
    assert "Human description" in http.task["desc"]
    assert http.task["desc"].count('data-testpilot-summary="1"') == 1


def test_no_default_api_key_header_even_if_unrelated_env_exists(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "unrelated-key")
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr(httpx, "post", post)
    LlmGateway({"api_base": "http://test.invalid", "api_key_env": ""}).parse_case({})
    assert captured["headers"] == {}

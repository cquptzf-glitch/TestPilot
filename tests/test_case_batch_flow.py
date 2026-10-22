"""Batch analysis contracts: real RPC scopes, mixed failures, cancellation, review gate."""
import threading
import time
from types import SimpleNamespace

import pytest

from testpilot.case_parser import CaseService
from testpilot.core.errors import AbortRequested, RpcError
from testpilot.infra.abort import AbortSignal
from testpilot.infra.jobs import CaseJobs
from testpilot.rpc import RpcService
from tests.conftest import FakeLlm, FakeZentao


@pytest.fixture
def service(db_conn):
    svc = CaseService(db_conn, FakeZentao(), FakeLlm(), {})
    svc.pull([1, 2])
    return svc


def test_rpc_selected_batch_deduplicates_and_keeps_review_required(service):
    rpc = RpcService(SimpleNamespace(cases=service))
    try:
        result = rpc.dispatch("case.parse", {"caseIds": ["TC-2", "TC-1", "TC-2"]})
        assert result["total"] == result["parsed"] == 2
        assert [i["case_id"] for i in result["items"]] == ["TC-2", "TC-1"]
        assert service.not_runnable(["TC-1", "TC-2"])
        service.accept("TC-1")
        assert service.not_runnable(["TC-1"]) == []
        assert service.show("TC-2")["status"] == "draft"
    finally:
        rpc.drain()
        rpc._jobs.close()
        rpc._executor.shutdown()
        rpc._run_executor.shutdown()


@pytest.mark.parametrize("params", [
    {"caseIds": []}, {"caseIds": "TC-1"}, {"caseIds": [1]},
    {"caseIds": [""]}, {"caseIds": ["TC-1"] * 501},
    {"caseIds": ["TC-1"], "allStale": True},
    {"caseId": "TC-1", "caseIds": ["TC-2"]}, {},
])
def test_rpc_rejects_invalid_batch_without_model_work(service, params):
    rpc = RpcService(SimpleNamespace(cases=service))
    try:
        with pytest.raises(RpcError):
            rpc.dispatch("case.parse", params)
        assert service.llm.parse_calls == []
    finally:
        rpc._jobs.close()
        rpc._executor.shutdown()
        rpc._run_executor.shutdown()


def test_pending_batch_covers_initial_empty_dsl_but_skips_rejected(service):
    service.reject("TC-2")
    result = service.parse_many()
    assert result["parsed"] == 1
    assert result["items"][0]["case_id"] == "TC-1"
    assert result["failed"] == []


def test_one_unexpected_provider_error_does_not_stop_later_cases(service):
    original = service.llm.parse_case
    def parse(raw):
        if raw["case_id"] == "TC-1":
            raise RuntimeError("provider offline")
        return original(raw)
    service.llm.parse_case = parse
    progress = []
    service.progress = lambda p: progress.append(dict(p))
    result = service.parse_many(["TC-1", "TC-2"])
    assert result["parsed"] == 1
    assert result["failed"] == [{"case_id": "TC-1", "error": "provider offline"}]
    assert service.show("TC-1")["steps"] == []
    assert service.show("TC-2")["steps"]
    assert progress[-1]["total"] == progress[-1]["completed"] == 2


def test_abort_on_last_case_is_not_reported_as_completed_failure(service):
    abort = AbortSignal()
    service.abort = abort
    def cancelled(raw):
        abort.request()
        abort.check()
    service.llm.parse_case = cancelled
    with pytest.raises(AbortRequested):
        service.parse_many(["TC-1"])
    assert service.show("TC-1")["steps"] == []


def test_cancel_job_keeps_completed_batch_results_and_progress():
    started = threading.Event()
    def runner(method, params, abort, progress):
        result = {"total": 2, "parsed": 1, "failed": [],
                  "items": [{"case_id": "TC-1", "step_count": 2}]}
        progress({"total": 2, "completed": 1, "result": result})
        progress({"phase": "parse", "case_id": "TC-2"})
        started.set()
        while True:
            abort.check()
            time.sleep(0.01)
    jobs = CaseJobs(runner)
    try:
        job = jobs.start("case.parse", {"caseIds": ["TC-1", "TC-2"]})
        assert started.wait(1)
        jobs.cancel(job["id"])
        deadline = time.monotonic() + 1
        while jobs.status(job["id"])["status"] not in ("cancelled", "failed"):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        final = jobs.status(job["id"])
        assert final["status"] == "cancelled"
        assert final["progress"]["completed"] == 1
        assert final["progress"]["total"] == 2
        assert final["result"]["items"][0]["case_id"] == "TC-1"
    finally:
        jobs.close()


def test_rpc_background_cancellation_keeps_partial_result(service):
    entered = threading.Event()
    original = service.llm.parse_case
    def parse(raw):
        if raw["case_id"] == "TC-2":
            entered.set()
            while True:
                service.abort.check()
                time.sleep(0.01)
        return original(raw)
    service.llm.parse_case = parse
    rpc = RpcService(SimpleNamespace(cases=service))
    try:
        job = rpc.dispatch("job.start", {"method": "case.parse",
            "params": {"caseIds": ["TC-1", "TC-2"]}})
        assert entered.wait(2)
        rpc.dispatch("job.cancel", {"jobId": job["id"]})
        deadline = time.monotonic() + 2
        while True:
            final = rpc.dispatch("job.status", {"jobId": job["id"]})
            if final["status"] not in ("queued", "running"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert final["status"] == "cancelled"
        assert final["result"]["parsed"] == 1
        assert final["result"]["items"][0]["case_id"] == "TC-1"
        assert service.show("TC-1")["steps"]
        assert service.show("TC-2")["steps"] == []
    finally:
        rpc.drain()
        rpc.close()


@pytest.mark.parametrize("target", [{"title_re": "Instrument.*"}, {"exe": "D:/Qt/Instrument.exe"}])
def test_selected_target_is_visible_in_draft_and_requires_review(service, target):
    result = service.parse_many(["TC-1", "TC-2"], app_target=target)
    assert result["parsed"] == 2
    for cid in ("TC-1", "TC-2"):
        row = service.show(cid)
        assert row["status"] == "draft"
        assert row["steps"][0]["action"] == "app.launch"
        assert row["steps"][0]["args"] == target
    assert service.llm.parse_calls[0]["application"] == target


@pytest.mark.parametrize("target", [{}, [], {"exe": ""}, {"title_re": 1}, {"cmd": "x"}])
def test_invalid_application_target_rejected_before_analysis(service, target):
    rpc = RpcService(SimpleNamespace(cases=service))
    try:
        with pytest.raises(RpcError):
            rpc.dispatch("case.parse", {"caseIds": ["TC-1"], "appTarget": target})
        assert service.llm.parse_calls == []
    finally:
        rpc.close()

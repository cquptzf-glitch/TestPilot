"""Behavior checks for the network simulation lab (no fake HTTP transports)."""
import json
import time

import httpx
import pytest
import websocket

from testpilot.adapters.zentao_adapter import ZentaoAdapter
from testpilot.core.errors import DeviceError
from testpilot.drivers.emulator_adapter import EmulatorAdapter
from testpilot.simulation.runner import acceptance, local_network
from testpilot.simulation.services import LabError, LabServices, LabState


def test_measurement_generation_prevents_old_result_overwriting_new_scene():
    state = LabState()
    state.command("start_measurement", {"sample_id": "old"})
    state.command("load_scene", {"scene_id": "wrong_value"})
    time.sleep(0.35)
    assert state.command("query_state", {"path": "/state/status"}) == "Ready"
    assert state.command("query_state", {"path": "/state/value"}) == ""


def test_export_requires_completed_measurement():
    state = LabState()
    with pytest.raises(LabError, match="Complete a measurement"):
        state.command("export_report", {})


@pytest.mark.parametrize("command,args,code", [
    ("load_scene", {"scene_id": "missing"}, "SCENE_NOT_FOUND"),
    ("query_state", {"path": "/state/does_not_exist"}, "PATH_NOT_FOUND"),
    ("start_measurement", {"sample_id": ""}, "SAMPLE_REQUIRED"),
    ("unknown", {}, "UNKNOWN_COMMAND"),
])
def test_device_errors_are_explicit(command, args, code):
    with pytest.raises(LabError) as caught:
        LabState().command(command, args)
    assert caught.value.code == code


def test_real_rest_auth_pagination_and_task_round_trip():
    with local_network(), LabServices(0, 0) as lab:
        adapter = ZentaoAdapter({"base_url": lab.url, "account": "demo", "password": "demo",
                                 "token_env": "TESTPILOT_SIM_TEST_TOKEN", "product_id": 1, "build": "1"})
        assert len(adapter.fetch_cases()) == 7
        with httpx.Client(trust_env=False) as client:
            second = client.get(lab.url + "/api.php/v1/products/1/testcases?page=2&limit=3",
                                headers={"Token": lab.state.token}).json()
            assert len(second["testcases"]) == 3
            assert second["total"] == 7
            assert client.post(lab.url + "/lab/faults", json={"writeback": -1}).status_code == 400
            assert client.post(lab.url + "/lab/faults", json=[]).status_code == 400
        task = adapter.create_testtask({"name": "network", "case_ids": ["TC-91001"]})
        adapter.update_task_status(task, "done", {"total": 1, "pass": 1})
        assert lab.state.snapshot()["tasks"][task]["status"] == "done"
        assert lab.state.login_count == 1


def test_real_websocket_receipts_faults_and_state():
    with local_network(), LabServices(0, 0) as lab:
        emulator = EmulatorAdapter({"endpoint": lab.endpoint, "call_timeout_s": 2})
        try:
            assert emulator.protocol_version() == "1.0"
            emulator.load_scene("device_error")
            with pytest.raises(DeviceError, match="DEVICE_OFFLINE"):
                emulator.call("start_measurement", {"sample_id": "test"})
            emulator.load_scene("normal")
            emulator.call("start_measurement", {"sample_id": "test"})
            deadline = time.monotonic() + 3
            while emulator.query_state("/state/status") != "Completed":
                assert time.monotonic() < deadline
                time.sleep(0.03)
            assert emulator.query_state("/state/value") == "42.5"
            assert emulator.call("export_report", {})["data"]["export_count"] == 1
            assert emulator.history(0) == []
        finally:
            emulator.close()


def test_protocol_rejects_malformed_json_and_versions_without_dropping_connection():
    with local_network(), LabServices(0, 0) as lab:
        client = websocket.create_connection(lab.endpoint, timeout=2,
                                              http_no_proxy=["127.0.0.1"])
        try:
            for payload, code in [
                    ("[1]", "INVALID_REQUEST"), ("invalid", "BAD_JSON"),
                    (json.dumps({"id": 2, "cmd": "hello", "ver": "2"}), "VERSION_MISMATCH")]:
                client.send(payload)
                response = json.loads(client.recv())
                assert response["ok"] is False
                assert response["err"]["code"] == code
            client.send(json.dumps({"id": 3, "cmd": "hello", "args": {}, "ver": "1.0"}))
            response = json.loads(client.recv())
            assert response["id"] == 3 and response["ok"]
        finally:
            client.close()


def test_engine_rpc_network_acceptance(tmp_path):
    result = acceptance(tmp_path / "isolated-lab")
    assert result["ok"]
    assert all(check["passed"] for check in result["checks"])
    assert len(result["runs"]) == 9
    assert (tmp_path / "isolated-lab/service-evidence.json").is_file()


@pytest.mark.parametrize("command", ["hello", "query_state"])
def test_real_state_query_respects_shared_timeout_budget(command):
    with local_network(), LabServices(0, 0) as lab:
        lab.ws_delays[command] = 0.6
        emulator = EmulatorAdapter({"endpoint": lab.endpoint, "connect_timeout_s": 2,
                                    "call_timeout_s": 10})
        started = time.monotonic()
        try:
            with pytest.raises(DeviceError):
                emulator.query_state_bounded("/state/status", timeout_s=0.12)
            assert time.monotonic() - started < 0.5
            assert not emulator.connected
        finally:
            emulator.close()


def test_real_state_query_cancels_during_pending_receipt():
    import threading
    from testpilot.core.errors import AbortRequested
    from testpilot.infra.abort import AbortSignal
    with local_network(), LabServices(0, 0) as lab:
        lab.ws_delays["query_state"] = 1
        emulator = EmulatorAdapter({"endpoint": lab.endpoint, "call_timeout_s": 10})
        assert emulator.protocol_version() == "1.0"
        abort = AbortSignal()
        timer = threading.Timer(0.1, abort.request)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(AbortRequested):
                emulator.query_state_bounded("/state/status", timeout_s=5, abort=abort)
            assert time.monotonic() - started < 0.6
            assert not emulator.connected
        finally:
            timer.join()
            emulator.close()


def test_multimodal_lab_accepts_image_sized_requests():
    with local_network(), LabServices(0, 0) as lab:
        response = httpx.post(lab.url + "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": [
                {"type": "text", "text": "synthetic image assertion"},
                {"type": "image_url", "image_url": {"url": "x" * 1100000}},
            ]}]}, trust_env=False, timeout=3)
        assert response.status_code == 200
        assert response.json()["choices"]

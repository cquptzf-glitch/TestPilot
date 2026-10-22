"""JSON-RPC 服务单测 — 方法表、参数校验、错误语义、事件推送（设计 2.5）。"""
import asyncio
import json

import pytest

from testpilot.core.errors import INVALID_PARAMS, METHOD_NOT_FOUND, RpcError
from testpilot.infra.events import EventBus
from testpilot.rpc import RpcService
from tests.conftest import prepare_case


@pytest.fixture()
def svc(db_conn, orch_factory):
    orch = orch_factory()
    ids = prepare_case(orch, (1, 2))
    return RpcService(orch), orch, ids


# ---------- 基础 ----------

def test_health(svc):
    service, orch, _ = svc
    out = service.dispatch("health", {})
    assert out["db"]["ok"] is True
    assert "uia" in out["drivers"]


def test_unknown_method_raises_method_not_found(svc):
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("nope.nope", {})
    assert e.value.code == METHOD_NOT_FOUND


def test_methods_table_is_complete_and_sorted(svc):
    service, _, _ = svc
    names = service.methods()
    assert names == sorted(names)
    for expected in ("case.pull", "suite.create", "task.run", "run.detail",
                     "kb.confirm", "dsl.review", "automation.invoke", "shutdown"):
        assert expected in names
    # 方法表与实现必须一致（防止文档与代码漂移）
    for name in names:
        try:
            service.dispatch(name, {})
        except RpcError as e:
            assert e.code != METHOD_NOT_FOUND, f"{name} 未在 dispatch 表中实现"


def test_missing_required_param(svc):
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("case.detail", {})
    assert e.value.code == INVALID_PARAMS


def test_non_integer_param_maps_to_invalid_params(svc):
    """整型参数传非整数：INVALID_PARAMS（-32602），而不是兜成 INTERNAL_ERROR。"""
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("run.detail", {"runId": "abc"})
    assert e.value.code == INVALID_PARAMS


def test_case_pull_rejects_non_integer_ids(svc):
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("case.pull", {"caseIds": [1, "x"], "parse": False})
    assert e.value.code == INVALID_PARAMS


# ---------- 自动化探索 ----------

def test_automation_rpc_session_history_and_draft(svc):
    service, _, _ = svc
    tools = service.dispatch("automation.tools", {})
    assert any(t["name"] == "ui.click" for t in tools["tools"])
    assert "uia" in tools["drivers"]
    assert tools["emulator"] is True

    out = service.dispatch("automation.invoke", {
        "tool": "ui.click",
        "args": {"key": "btn.start", "name": "开始检测", "control_type": "Button"},
    })
    assert out["tool"] == "ui.click"
    assert out["history_count"] == 1

    history = service.dispatch("automation.history", {})
    assert history["session"]["steps"][0]["tool"] == "ui.click"

    draft = service.dispatch("automation.draft", {
        "caseId": "TC-EXP-1", "title": "探索生成",
    })
    assert draft["case"]["case_id"] == "TC-EXP-1"
    assert draft["case"]["steps"][0]["action"] == "ui.click"
    assert draft["case"]["steps"][0]["target"]["primary"]["name"] == "开始检测"

    edited = service.dispatch("automation.updateStep", {
        "index": 1,
        "args": {"key": "btn.start.v2", "name": "开始检测", "control_type": "Button"},
    })
    assert edited["step"]["args"]["key"] == "btn.start.v2"

    service.dispatch("automation.invoke", {
        "tool": "device.scene",
        "args": {"scene_id": "normal_sample_a"},
    })
    moved = service.dispatch("automation.moveStep", {"index": 2, "toIndex": 1})
    assert [step["tool"] for step in moved["steps"]] == ["device.scene", "ui.click"]

    removed = service.dispatch("automation.removeStep", {"index": 1})
    assert removed["removed"]["tool"] == "device.scene"
    assert removed["history_count"] == 1
    assert service.dispatch("automation.history", {})["session"]["steps"][0]["index"] == 1

    reset = service.dispatch("automation.reset", {"title": "新探索"})
    assert reset["ok"] is True
    assert service.dispatch("automation.history", {})["session"]["steps"] == []


def test_automation_rpc_restore_session(svc):
    service, _, _ = svc
    service.dispatch("automation.invoke", {
        "tool": "ui.click",
        "args": {"key": "btn.start", "name": "开始检测", "control_type": "Button"},
    })
    saved = service.dispatch("automation.history", {})["session"]
    saved["steps"][0]["index"] = 77

    service.dispatch("automation.reset", {})
    restored = service.dispatch("automation.restore", {"session": saved})
    assert restored["history_count"] == 1
    assert restored["session"]["steps"][0]["index"] == 1
    assert restored["session"]["steps"][0]["tool"] == "ui.click"

    with pytest.raises(RpcError) as e:
        service.dispatch("automation.restore", {"session": {"steps": "bad"}})
    assert e.value.code == INVALID_PARAMS


def test_automation_restore_recovers_serialized_history(svc):
    service, _, _ = svc
    service.dispatch("automation.invoke", {
        "tool": "ui.click",
        "args": {"key": "btn.start", "name": "开始检测",
                 "control_type": "Button"},
    })
    saved = service.dispatch("automation.history", {})["session"]
    assert len(saved["steps"]) == 1

    service.dispatch("automation.reset", {})
    assert service.dispatch("automation.history", {})["session"]["steps"] == []

    restored = service.dispatch("automation.restore", {"session": saved})
    assert restored["history_count"] == 1
    assert restored["session"]["steps"][0]["tool"] == "ui.click"

    draft = service.dispatch("automation.draft", {})
    assert draft["case"]["steps"][0]["action"] == "ui.click"
    assert draft["case"]["steps"][0]["target"]["primary"]["name"] == "开始检测"


def test_automation_rpc_rejects_ui_actions_while_run_is_active(svc):
    service, orch, _ = svc
    orch._in_run.set()
    try:
        with pytest.raises(RpcError) as e:
            service.dispatch("automation.invoke", {
                "tool": "ui.click", "args": {"name": "开始检测"}})
        assert e.value.code == INVALID_PARAMS
    finally:
        orch._in_run.clear()


# ---------- 知识库：用例上下文 / 环境矩阵 ----------

def test_kb_case_ctx_crud(svc):
    service, _, ids = svc
    saved = service.dispatch("kb.caseCtxSave", {
        "caseId": ids[0], "scene": "normal_sample_a",
        "pitfalls": "预热慢;偶发超时", "notes": "人审备注"})
    assert saved["payload"]["pitfalls"] == ["预热慢", "偶发超时"]
    assert saved["payload"]["zentao_case_id"] == 1

    listed = service.dispatch("kb.caseCtxList", {"caseId": ids[0]})["entries"]
    assert len(listed) == 1 and listed[0]["case_title"]

    # 部分更新：只改 notes，不清掉 scene/pitfalls
    merged = service.dispatch("kb.caseCtxSave", {
        "caseId": ids[0], "payload": {"notes": "改过"}})
    assert merged["payload"]["scene"] == "normal_sample_a"
    assert merged["payload"]["notes"] == "改过"

    assert service.dispatch("kb.caseCtxDelete",
                            {"caseId": ids[0]})["deleted"] == 1
    assert service.dispatch("kb.caseCtxList", {})["entries"] == []


def test_kb_env_compat_query(svc):
    service, orch, ids = svc
    orch.kb.record_env_compat(run_id=1, test_app_version="1.0.0",
                              suite_id=None, case_ids=ids,
                              statuses={ids[0]: "pass"})
    out = service.dispatch("kb.envCompat", {"caseIds": ids})
    assert len(out["entries"]) == 1
    assert out["entries"][0]["payload"]["matrix"][ids[0]]["pass"] == 1


def test_task_run_writes_driver_stats_and_env_compat(svc):
    """run 收尾必须同时产出降级统计与矩阵条目（前端与报告都依赖）。"""
    service, _, ids = svc
    created = service.dispatch("task.create", {"name": "统计任务", "caseIds": ids})
    stats = service.dispatch("task.run", {"taskId": created["task_id"]})
    assert stats["drivers"]["steps"] >= 2
    assert "transitions" in stats["drivers"]
    assert "unknown|cases:" in stats["env_compat"]["signature"]


# ---------- 用例 / 方案 ----------

def test_case_list_and_detail(svc):
    service, _, ids = svc
    listed = service.dispatch("case.list", {"status": "active"})
    assert [c["case_id"] for c in listed["cases"]] == ids

    detail = service.dispatch("case.detail", {"caseId": "TC-1"})
    assert detail["status"] == "active"
    assert detail["step_count"] == 2


def test_case_export_and_import(svc):
    service, _, _ = svc
    exported = service.dispatch("case.export", {"caseId": "TC-1", "format": "json"})
    content = exported["content"]
    assert "TC-1" in content

    data = json.loads(content)
    data["title"] = "改过的标题"
    out = service.dispatch("case.import", {"text": json.dumps(data),
                                           "caseId": "TC-1"})
    assert out["case_id"] == "TC-1"
    assert service.dispatch("case.detail", {"caseId": "TC-1"})["title"] == "改过的标题"


def test_case_pull_and_parse(svc):
    service, orch, _ = svc
    pulled = service.dispatch("case.pull", {"caseIds": [1], "parse": True})
    assert pulled["unchanged"] == 1
    assert pulled["parse"]["parsed"] == 1


def test_suite_crud(svc):
    service, _, ids = svc
    created = service.dispatch("suite.create", {"name": "RPC方案", "caseIds": ids})
    assert created["name"] == "RPC方案"
    assert created["preconditions"] == ["模拟器场景: normal_sample_a"]

    assert [s["id"] for s in service.dispatch("suite.list", {})["suites"]] == [created["id"]]
    assert service.dispatch("suite.detail", {"suiteId": created["id"]})["id"] == created["id"]

    updated = service.dispatch("suite.update", {"suiteId": created["id"],
                                               "name": "RPC方案2"})
    assert updated["name"] == "RPC方案2"

    service.dispatch("suite.delete", {"suiteId": created["id"]})
    assert service.dispatch("suite.list", {})["suites"] == []


# ---------- 任务 / 报告 ----------

def test_task_create_run_and_drilldown(svc):
    service, orch, ids = svc
    created = service.dispatch("task.create", {"name": "RPC任务", "caseIds": ids})
    assert created["sync_status"] == "local"

    stats = service.dispatch("task.run", {"taskId": created["task_id"]})
    assert stats["status"] == "completed" and stats["pass"] == 2

    runs = service.dispatch("run.list", {})["runs"]
    assert runs[0]["id"] == stats["run_id"]

    detail = service.dispatch("run.detail", {"runId": stats["run_id"]})
    assert len(detail["cases"]) == 2
    assert detail["env"]["case_ids"] == ids

    case_exec_id = detail["cases"][0]["id"]
    trace = service.dispatch("run.stepTrace", {"caseExecId": case_exec_id})["steps"]
    assert trace and trace[0]["action"] == "app.launch"
    assert trace[1]["target"]["key"] == "btn.start_detect"

    case_detail = service.dispatch("run.caseDetail", {"runId": stats["run_id"],
                                                      "caseExecId": case_exec_id})
    assert case_detail["case_id"] == "TC-1"

    report = service.dispatch("run.report", {"runId": stats["run_id"]})
    assert report["path"].endswith("report.md")

    assert service.dispatch("task.list", {})["tasks"][0]["id"] == created["task_id"]
    assert service.dispatch("task.detail",
                            {"taskId": created["task_id"]})["name"] == "RPC任务"


def test_task_run_rejects_when_explore_interaction_lock_is_busy(svc):
    service, _, ids = svc
    created = service.dispatch("task.create", {"name": "互斥", "caseIds": ids})
    assert service._interaction_lock.acquire(blocking=False)
    try:
        with pytest.raises(RpcError) as e:
            service.dispatch("task.run", {"taskId": created["task_id"]})
        assert e.value.code == INVALID_PARAMS
    finally:
        service._interaction_lock.release()


def test_task_run_releases_interaction_lock_on_error(svc):
    service, orch, ids = svc
    created = service.dispatch("task.create", {"name": "异常释放锁", "caseIds": ids})
    original = orch.run_task
    orch.run_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            service.dispatch("task.run", {"taskId": created["task_id"]})
        assert service._interaction_lock.acquire(blocking=False)
        service._interaction_lock.release()
    finally:
        orch.run_task = original


def test_task_abort_and_sync_pending(svc):
    service, _, _ = svc
    assert service.dispatch("task.abort", {})["ok"] is True
    assert service.dispatch("task.syncPending", {})["results"] == []


def test_task_sync_pending_passes_task_id(svc):
    """``task.syncPending`` 的 taskId 必须透传到引擎 —— 否则 Tasks 页行内
    「补偿」会静默退化成全量补偿（把所有待回写任务都重推进禅道）。
    """
    service, orch, _ = svc
    seen: list[int | None] = []
    orch.sync_pending_tasks = lambda task_id=None: (seen.append(task_id), [])[1]

    service.dispatch("task.syncPending", {})
    service.dispatch("task.syncPending", {"taskId": 7})
    service.dispatch("task.syncPending", {"taskId": "8"})   # 前端可能传字符串
    assert seen == [None, 7, 8]


def test_run_detail_unknown_run(svc):
    service, _, _ = svc
    with pytest.raises(RpcError):
        service.dispatch("run.detail", {"runId": 999})


def test_run_detail_carries_case_stability(svc):
    """稳定性是跨 run 的派生视图：第二次 run 之后应能看到 2 次样本（全通过）。"""
    service, _, ids = svc
    created = service.dispatch("task.create", {"name": "稳定性", "caseIds": ids})
    service.dispatch("task.run", {"taskId": created["task_id"]})
    service.dispatch("task.run", {"taskId": created["task_id"]})

    run_id = service.dispatch("run.list", {})["runs"][0]["id"]
    detail = service.dispatch("run.detail", {"runId": run_id})
    entry = detail["stability"][ids[0]]
    assert entry["runs"] == 2 and entry["level"] == "stable"
    assert entry["recent"] == ["pass", "pass"]


def test_run_detail_carries_signature_history(svc):
    """失败用例附带签名历史画像（设计 9.4）：第二次同签名失败时 count=2 且定位失败步骤。"""
    service, orch, ids = svc
    created = service.dispatch("task.create", {"name": "快速定位", "caseIds": ids})
    orch.drivers["uia"].fail_locate = True
    service.dispatch("task.run", {"taskId": created["task_id"]})
    second = service.dispatch("task.run", {"taskId": created["task_id"]})

    detail = service.dispatch("run.detail", {"runId": second["run_id"]})
    hist = detail["cases"][0]["history"]
    # ids 两个用例 × 两次失败 run = 同一签名 4 次
    assert hist["count"] == 4
    assert hist["last"]["run_id"] == second["run_id"]
    assert hist["last"]["step_no"] is not None
    assert hist["categories"][0]["category"] == "uia_change"

    one = service.dispatch("run.caseDetail", {"runId": second["run_id"],
                                              "caseExecId": detail["cases"][0]["id"]})
    assert one["history"]["count"] == 4


def test_run_compare_buckets_and_validation(orch_factory):
    from tests.conftest import FakeDriver, prepare_case

    orch = orch_factory(drivers={"uia": FakeDriver("uia")})
    ids = prepare_case(orch, (1,))
    created = orch.create_task({"name": "对比", "case_ids": ids})
    good = orch.run_task(created["task_id"])

    orch.drivers["uia"].fail_locate = True          # 第二次全挂
    bad = orch.run_task(created["task_id"])

    service = RpcService(orch)
    out = service.dispatch("run.compare", {"baseRunId": good["run_id"],
                                           "targetRunId": bad["run_id"]})
    assert [c["case_id"] for c in out["new_failures"]] == ["TC-1"]
    assert out["counts"]["new_failures"] == 1
    assert out["base"]["pass_rate"] == 1.0
    assert out["target"]["pass_rate"] == 0.0
    assert out["pass_rate_delta"] == -1.0

    with pytest.raises(RpcError) as e:
        service.dispatch("run.compare", {"baseRunId": 1, "targetRunId": 1})
    assert e.value.code == INVALID_PARAMS
    with pytest.raises(RpcError) as e2:
        service.dispatch("run.compare", {"baseRunId": 1})
    assert e2.value.code == INVALID_PARAMS


def test_run_case_detail_unknown(svc):
    service, _, _ = svc
    with pytest.raises(RpcError):
        service.dispatch("run.caseDetail", {"runId": 1, "caseExecId": 999})


# ---------- 知识库 / DSL ----------

def test_kb_flow(svc):
    service, orch, ids = svc
    # 造一条 pending 失败知识
    from testpilot.knowledge.service import FailureKnowledge
    orch.kb.save_failure(FailureKnowledge(entry_id=0, signature="sig-x",
                                          category="timing", disposition="none",
                                          suggestion="加等待", confidence=0.6))
    pending = service.dispatch("kb.pending", {})["entries"]
    assert len(pending) == 1
    entry_id = pending[0]["id"]

    assert service.dispatch("kb.setDisposition", {
        "entryId": entry_id, "disposition": "retry_ready",
    })["disposition"] == "retry_ready"

    assert service.dispatch("kb.confirm", {"entryId": entry_id})["status"] == "active"
    assert service.dispatch("kb.pending", {})["entries"] == []


def test_kb_reject(svc):
    service, orch, _ = svc
    from testpilot.knowledge.service import FailureKnowledge
    orch.kb.save_failure(FailureKnowledge(entry_id=0, signature="sig-y",
                                          category="env", disposition="none",
                                          suggestion="", confidence=0.5))
    entry_id = service.dispatch("kb.pending", {})["entries"][0]["id"]
    assert service.dispatch("kb.reject", {"entryId": entry_id})["status"] == "rejected"


def test_dsl_review_save_accept_reject(svc):
    service, orch, _ = svc
    orch.cases.pull([1])                       # 新增草稿 TC-1 不变，另建草稿用例
    review = service.dispatch("dsl.review", {"caseId": "TC-1"})
    assert review["status"] == "active"

    payload = {"case_id": "TC-1", "title": "T",
               "steps": [{"id": "s1", "action": "sleep", "args": {"ms": 1}}]}
    out = service.dispatch("dsl.save", {"caseId": "TC-1", "text": json.dumps(payload)})
    assert out["step_count"] == 1
    assert service.dispatch("dsl.accept", {"caseId": "TC-1"})["version"] == 2
    assert service.dispatch("dsl.reject", {"caseId": "TC-1"})["status"] == "rejected"


# ---------- 协议层 ----------

def test_handle_raw_returns_jsonrpc_envelope(svc):
    service, _, _ = svc
    resp = asyncio.run(service._handle_raw(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "health", "params": {}})))
    assert resp["id"] == 7 and resp["result"]["db"]["ok"] is True


def test_handle_raw_parse_error(svc):
    service, _, _ = svc
    resp = asyncio.run(service._handle_raw("{不是 json"))
    assert resp["error"]["code"] == -32700


def test_handle_raw_method_not_found(svc):
    service, _, _ = svc
    resp = asyncio.run(service._handle_raw(
        json.dumps({"id": 1, "method": "nope", "params": {}})))
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_handle_raw_internal_error(svc):
    service, _, _ = svc
    resp = asyncio.run(service._handle_raw(
        json.dumps({"id": 1, "method": "case.detail", "params": {}})))
    assert resp["error"]["code"] == INVALID_PARAMS


def test_shutdown_requests_stop(svc):
    service, _, _ = svc
    out = service.dispatch("shutdown", {})
    assert out["ok"] is True


# ---------- 配置（设置页） ----------

def test_config_describe_and_apply_roundtrip(svc):
    service, _, _ = svc
    described = service.dispatch("config.describe", {})
    assert described["groups"], "设置页拿不到任何分组"
    paths = [f["path"] for g in described["groups"] for f in g["fields"]]
    assert "driver.mode" in paths

    out = service.dispatch("config.apply", {"values": {"driver.mode": "uia_only"}})
    assert out["restart_required"] is True
    assert out["written"] == 1

    after = service.dispatch("config.describe", {})
    field = next(f for g in after["groups"] for f in g["fields"] if f["path"] == "driver.mode")
    assert field["overridden"] is True
    assert field["value"] == "uia_only"


def test_config_apply_rejects_bad_value_as_invalid_params(svc):
    """面向用户的校验错误要归到 INVALID_PARAMS（前端据此显示可读文案）。"""
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("config.apply", {"values": {"driver.mode": "telepathy"}})
    assert e.value.code == INVALID_PARAMS


def test_config_apply_rejects_non_object_values(svc):
    service, _, _ = svc
    with pytest.raises(RpcError) as e:
        service.dispatch("config.apply", {"values": ["not", "an", "object"]})
    assert e.value.code == INVALID_PARAMS


def test_config_override_text_reflects_disk(svc):
    """设置页「高级」页签直接展示该返回值 —— 它必须与磁盘内容一致。

    文件不存在时不能报错（首次启动就是这种状态），要返回 exists=False。
    """
    service, _, _ = svc
    before = service.dispatch("config.overrideText", {})
    assert before["exists"] is False
    assert before["text"] == ""
    assert before["path"].endswith("config.override.yaml")

    service.dispatch("config.apply", {"values": {"llm.temperature": 0.7}})
    after = service.dispatch("config.overrideText", {})
    assert after["exists"] is True
    assert "temperature" in after["text"], "展示的 YAML 与磁盘内容不一致"
    assert "0.7" in after["text"]


def test_config_reset_clears_override_text(svc):
    service, _, _ = svc
    service.dispatch("config.apply", {"values": {"llm.temperature": 0.7}})
    assert service.dispatch("config.overrideText", {})["exists"] is True
    service.dispatch("config.reset", {})
    assert service.dispatch("config.overrideText", {})["exists"] is False
    assert service.dispatch("config.describe", {})["exists"] is False


# ---------- 事件推送 ----------

def test_event_forwarding_to_publish(cfg, db_conn, orch_factory):
    bus = EventBus()
    orch = orch_factory(bus=bus)
    service = RpcService(orch)
    sent = []
    service.publish = lambda event, payload: sent.append(event)

    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "事件", "case_ids": ids})
    orch.run_task(task["task_id"])

    assert "step.finished" in sent
    assert "case.finished" in sent
    assert "run.progress" in sent


def test_publish_without_subscribers_is_noop(svc):
    service, _, _ = svc
    service.publish("step.finished", {"x": 1})     # 无订阅者、无 loop -> 静默返回


def test_close_unsubscribes(db_conn, orch_factory):
    bus = EventBus()
    orch = orch_factory(bus=bus)
    service = RpcService(orch)
    service.close()
    assert service._subs == set()
    assert bus._subs == []                         # 已退订，不再持有引用

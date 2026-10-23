"""任务编排单测 — 建任务门禁/禅道回写/run 收尾（M1-4、M2-1，设计 3.2/3.5）。
"""
import os
from pathlib import Path

import pytest

from testpilot.core.errors import TestPilotError
from testpilot.engine.orchestrator import TaskOrchestrator
from testpilot.infra.events import EventBus
from testpilot.storage import repos
from testpilot.storage.recorder import Recorder
from tests.conftest import FakeDriver, FakeEmulator, FakeLlm, FakeZentao


def _orch(cfg, registry, db_conn, tmp_path, *, zentao=None, drivers=None,
          emulator=None, llm=None, bus=None):
    return TaskOrchestrator(
        cfg, registry,
        conn=db_conn, recorder=Recorder(db_conn, tmp_path / "shots"),
        zentao=zentao or FakeZentao(), llm=llm or FakeLlm(),
        emulator=emulator or FakeEmulator(),
        drivers=drivers if drivers is not None else {"uia": FakeDriver("uia")},
        bus=bus)


@pytest.fixture()
def orch(cfg, registry, db_conn, tmp_path):
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cases.pull([1, 2])
    for cid in ("TC-1", "TC-2"):
        o.cases.parse(cid)
        o.cases.accept(cid)
    return o


# ---------- 建任务 ----------

def test_create_task_local_first(orch, db_conn):
    out = orch.create_task({"name": "本地任务", "case_ids": ["TC-1"]})
    assert out["zentao_task_id"] is None
    assert out["sync_status"] == "local"
    task = repos.get_task(db_conn, out["task_id"])
    assert task["case_ids"] == ["TC-1"]
    assert task["preconditions"] == ["模拟器场景: normal_sample_a"]
    assert task["driver_config"]["mode"] == "auto"


def test_create_task_requires_name_and_cases(orch):
    with pytest.raises(TestPilotError):
        orch.create_task({"name": " ", "case_ids": ["TC-1"]})
    with pytest.raises(TestPilotError):
        orch.create_task({"name": "无用例", "case_ids": []})


def test_create_task_gate_blocks_draft_cases(orch, db_conn):
    orch.cases.pull([1])
    orph = _draft_case(db_conn, "TC-9")
    assert orph
    with pytest.raises(TestPilotError) as e:
        orch.create_task({"name": "带草稿", "case_ids": ["TC-9"]})
    assert "不可执行" in str(e.value)

    out = orch.create_task({"name": "带草稿", "case_ids": ["TC-9"], "force": True})
    assert out["not_runnable"][0]["case_id"] == "TC-9"


def _draft_case(conn, case_id):
    repos.create_case(conn, case_id=case_id, zentao_case_id=9, title="草稿",
                      raw=None, zentao_updated_at="t")
    return True


def test_create_task_from_suite_snapshot(orch, db_conn):
    suite = orch.suites.create("方案", ["TC-1", "TC-2"],
                               driver_config={"mode": "vision_first"})
    out = orch.create_task({"name": "方案任务", "suite_id": suite["id"]})
    task = repos.get_task(db_conn, out["task_id"])
    assert task["case_ids"] == ["TC-1", "TC-2"]
    assert task["driver_config"]["mode"] == "vision_first"
    assert task["preconditions"] == ["模拟器场景: normal_sample_a"]

    # 方案后续修改不影响已建任务（快照语义）
    orch.suites.update(suite["id"], case_ids=["TC-1"])
    assert repos.get_task(db_conn, out["task_id"])["case_ids"] == ["TC-1", "TC-2"]


def test_create_task_rejects_invalid_driver_override(orch):
    with pytest.raises(TestPilotError, match="非法驱动模式"):
        orch.create_task({
            "name": "非法驱动",
            "case_ids": ["TC-1"],
            "driver_config": {"mode": "turbo"},
        })


def test_create_task_explicit_options_override_suite_defaults(orch, db_conn):
    suite = orch.suites.create(
        "带默认配置", ["TC-1", "TC-2"],
        driver_config={"mode": "vision_first"},
        preconditions=["模拟器场景: suite-default"])
    out = orch.create_task({
        "name": "本次覆盖",
        "suite_id": suite["id"],
        "driver_config": {"mode": "uia_only"},
        "preconditions": ["模拟器场景: task-override"],
    })
    task = repos.get_task(db_conn, out["task_id"])
    assert task["driver_config"]["mode"] == "uia_only"
    assert task["preconditions"] == ["模拟器场景: task-override"]


def test_create_task_rejects_malformed_collection_inputs(orch):
    with pytest.raises(TestPilotError, match="case_ids 必须是字符串数组"):
        orch.create_task({"name": "坏范围", "case_ids": "TC-1"})

    with pytest.raises(TestPilotError, match="case_ids 必须只包含非空字符串"):
        orch.create_task({"name": "混合范围", "case_ids": ["TC-1", 2]})

    with pytest.raises(TestPilotError, match="preconditions 必须是字符串数组"):
        orch.create_task({
            "name": "坏前置", "case_ids": ["TC-1"],
            "preconditions": "scene:a",
        })

    with pytest.raises(TestPilotError, match="driver_config 必须是对象"):
        orch.create_task({
            "name": "坏驱动配置", "case_ids": ["TC-1"],
            "driver_config": ["auto"],
        })


def test_create_task_rejects_unsafe_ids_and_boolean_flags(orch):
    bad_specs = [
        {"name": "bool-suite", "suite_id": True, "case_ids": []},
        {"name": "float-suite", "suite_id": 1.0, "case_ids": []},
        {"name": "zero-suite", "suite_id": 0, "case_ids": []},
        {"name": "bad-force", "case_ids": ["TC-1"], "force": "false"},
        {"name": "bad-remote", "case_ids": ["TC-1"], "remote": "false"},
        {
            "name": "bad-remote-id",
            "case_ids": ["TC-1"],
            "zentao_task_id": True,
        },
    ]
    for spec in bad_specs:
        with pytest.raises(TestPilotError):
            orch.create_task(spec)


def test_create_task_rejects_ambiguous_scope_and_dedupes_direct_cases(orch, db_conn):
    suite = orch.suites.create("范围方案", ["TC-1"])
    with pytest.raises(TestPilotError, match="只能指定一种"):
        orch.create_task({
            "name": "歧义",
            "suite_id": suite["id"],
            "case_ids": ["TC-2"],
        })

    out = orch.create_task({
        "name": "去重",
        "case_ids": ["TC-1", "TC-1", "TC-2", "TC-2"],
    })
    assert repos.get_task(db_conn, out["task_id"])["case_ids"] == ["TC-1", "TC-2"]


def test_create_task_remote_when_zentao_available(cfg, registry, db_conn, tmp_path):
    zentao = FakeZentao(configured=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")

    out = o.create_task({"name": "禅道任务", "case_ids": ["TC-1"], "remote": True})
    assert out["zentao_task_id"] == 9001
    assert out["sync_status"] == "synced"
    assert zentao.created[0]["case_ids"] == ["TC-1"]


def test_create_task_remote_degrades_when_zentao_unavailable(cfg, registry,
                                                            db_conn, tmp_path):
    o = _orch(cfg, registry, db_conn, tmp_path,
              zentao=FakeZentao(configured=False))
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    out = o.create_task({"name": "降级", "case_ids": ["TC-1"], "remote": True})
    assert (out["zentao_task_id"], out["sync_status"]) == (None, "local")


def test_create_task_remote_failure_marks_sync_pending(cfg, registry, db_conn,
                                                       tmp_path):
    o = _orch(cfg, registry, db_conn, tmp_path,
              zentao=FakeZentao(configured=True, fail_create=True))
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    out = o.create_task({"name": "失败", "case_ids": ["TC-1"], "remote": True})
    assert out["sync_status"] == "sync_pending"
    assert out["zentao_task_id"] is None


def test_remote_creation_crash_marker_prevents_duplicate_compensation(
        cfg, registry, db_conn, tmp_path, monkeypatch):
    """POST 已发生但本地未保存远端 ID 时，补偿必须 fail-closed，绝不重复创建。"""
    zentao = FakeZentao(configured=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")

    def crash_after_post(name, spec, case_ids):
        zentao.create_testtask({**spec, "name": name, "case_ids": case_ids})
        raise SystemExit("simulated crash after remote POST")

    monkeypatch.setattr(o, "_create_remote_task", crash_after_post)
    with pytest.raises(SystemExit, match="simulated crash"):
        o.create_task({
            "name": "崩溃窗口",
            "case_ids": ["TC-1"],
            "remote": True,
        })

    task = o.list_tasks()[0]
    assert task["sync_status"] == "sync_pending"
    assert task["zentao_task_id"] is None
    assert task["remote_state"]["creation_attempted"] is True
    assert len(zentao.created) == 1

    result = o.sync_pending_tasks(task["id"])
    assert result == [{
        "task_id": task["id"],
        "result": "creation_uncertain",
        "action": "reconcile_remote_id",
    }]
    assert len(zentao.created) == 1


def test_definite_remote_create_failure_can_retry_safely(
        cfg, registry, db_conn, tmp_path):
    zentao = FakeZentao(configured=True, fail_create=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")

    task = o.create_task({
        "name": "明确失败",
        "case_ids": ["TC-1"],
        "remote": True,
    })
    saved = o.list_tasks()[0]
    assert task["sync_status"] == "sync_pending"
    assert saved["remote_state"]["creation_attempted"] is False
    assert saved["remote_state"]["creation_uncertain"] is False

    zentao.fail_create = False
    result = o.sync_pending_tasks(task["task_id"])
    assert result[0]["result"] == "remote_ready"
    assert len(zentao.created) == 1
    saved = o.list_tasks()[0]
    assert saved["zentao_task_id"] is not None
    assert saved["sync_status"] == "synced"


def test_run_without_remote_id_keeps_pending_writeback_semantics(
        cfg, registry, db_conn, tmp_path):
    zentao = FakeZentao(configured=True, fail_create=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")

    task = o.create_task({
        "name": "远端待补偿仍先执行",
        "case_ids": ["TC-1"],
        "remote": True,
    })
    assert task["sync_status"] == "sync_pending"
    assert task["zentao_task_id"] is None

    stats = o.run_task(task["task_id"])
    assert stats["status"] == "completed"
    assert stats["writeback"] == "sync_pending"
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "sync_pending"


def test_create_task_with_preconditions_override(orch, db_conn):
    out = orch.create_task({"name": "自定义前置", "case_ids": ["TC-1"],
                            "preconditions": ["模拟器场景: custom"]})
    assert repos.get_task(db_conn, out["task_id"])["preconditions"] == [
        "模拟器场景: custom"]


# ---------- 执行 ----------

def test_fatal_run_setup_error_always_finalizes_run(
        orch, db_conn, monkeypatch):
    task = orch.create_task({"name": "致命准备异常", "case_ids": ["TC-1"]})
    seen = []
    orch.bus.subscribe(lambda event: seen.append(event.to_dict()))

    def boom(*_args, **_kwargs):
        raise RuntimeError("prefetch boom")

    monkeypatch.setattr(orch.kb, "prefetch", boom)
    with pytest.raises(RuntimeError, match="prefetch boom"):
        orch.run_task(task["task_id"])

    runs = repos.list_runs(db_conn, limit=10, task_id=task["task_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "error"
    assert run["finished_at"]
    assert run["stats"]["status"] == "error"
    assert "prefetch boom" in run["stats"]["fatal_error"]
    assert orch._active_run_id is None
    finished = [
        event for event in seen
        if event["event"] == "run.progress"
        and event["payload"].get("phase") == "finished"
    ]
    assert finished
    assert finished[-1]["payload"]["stats"]["status"] == "error"


def test_unexpected_remote_writeback_error_does_not_corrupt_local_run(
        cfg, registry, db_conn, tmp_path, monkeypatch):
    zentao = FakeZentao(configured=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({
        "name": "回写未知异常",
        "case_ids": ["TC-1"],
        "remote": True,
    })

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected writeback bug")

    monkeypatch.setattr(o, "_writeback", boom)
    stats = o.run_task(task["task_id"])

    assert stats["status"] == "completed"
    assert stats["writeback"] == "error"
    assert "unexpected writeback bug" in stats["writeback_error"]
    run = repos.run_detail(db_conn, stats["run_id"])
    assert run["status"] == "completed"
    assert run["stats"]["writeback"] == "error"
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "sync_pending"


def test_local_task_stays_local_when_writeback_hook_crashes(
        orch, db_conn, monkeypatch):
    task = orch.create_task({"name": "本地回写钩子异常", "case_ids": ["TC-1"]})

    def boom(*_args, **_kwargs):
        raise RuntimeError("local hook bug")

    monkeypatch.setattr(orch, "_writeback", boom)
    stats = orch.run_task(task["task_id"])

    assert stats["status"] == "completed"
    assert stats["writeback"] == "error"
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "local"


def test_run_task_happy_path(orch, db_conn, cfg):
    task = orch.create_task({"name": "跑通", "case_ids": ["TC-1", "TC-2"]})
    stats = orch.run_task(task["task_id"])

    assert stats["status"] == "completed"
    assert stats["total"] == 2 and stats["pass"] == 2
    assert stats["writeback"] == "local_only"          # 本地任务不回写禅道
    assert stats["report"]
    assert stats["llm_usage"]["calls"] == 0            # 执行主干不依赖 LLM

    run = repos.run_detail(db_conn, stats["run_id"])
    assert run["status"] == "completed"
    assert run["env"]["dsl_versions"] == {"TC-1": 1, "TC-2": 1}
    assert run["env"]["driver_mode"] == "auto"
    assert [c["status"] for c in run["cases"]] == ["pass", "pass"]

    from pathlib import Path
    assert Path(stats["report"]).exists()


def test_run_stats_counts_kb_hits_after_human_review(cfg, registry, db_conn, tmp_path):
    """知识闭环端到端：失败 -> 自动入队 -> 人审判定处置 -> 再跑即命中（写入 run 统计）。

    这条链路横跨 executor（签名/归因）→ knowledge（人审/处置）→ orchestrator（统计），
    是 M3-2/M4-5 的核心验收点，因此用两次真实 run 来验证而不是打桩。
    """
    o = _orch(cfg, registry, db_conn, tmp_path,
              drivers={"uia": FakeDriver("uia", fail_locate=True),
                       "vision": FakeDriver("vision", fail_locate=True)})
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({"name": "知识闭环", "case_ids": ["TC-1"]})

    first = o.run_task(task["task_id"])
    assert first["kb_hit_cases"] == 0
    assert first["fail"] == 1

    entries = o.kb.pending()
    assert entries, "失败归因没有产生待审条目"
    entry_id = entries[0]["id"]
    o.kb.set_disposition(entry_id, "block_known")     # 人审：判定为已知失败
    o.kb.confirm(entry_id, "active")

    second = o.run_task(task["task_id"])
    assert second["kb_hit_cases"] == 1
    assert second["blocked"] == 1 and second["fail"] == 0

    detail = repos.run_detail(db_conn, second["run_id"])
    assert detail["stats"]["kb_hit_cases"] == 1
    hit_case = detail["cases"][0]
    assert hit_case["kb_hit_id"] == entry_id
    assert hit_case["root_cause"]["source"] == "kb_exact"


def test_run_task_preloads_scenes(orch, db_conn):
    orch.emulator.scenes.clear()
    task = orch.create_task({"name": "场景", "case_ids": ["TC-1"]})
    orch.run_task(task["task_id"])
    assert orch.emulator.scenes[-1] == "normal_sample_a"


def test_run_task_records_scene_preload_failure(orch):
    """A failed case prerequisite blocks execution instead of producing a false pass."""
    orch.emulator.fail = True
    task = orch.create_task({"name": "场景失败", "case_ids": ["TC-1"]})
    stats = orch.run_task(task["task_id"])
    assert stats["blocked"] == 1
    assert stats["pass"] == 0
    assert stats["total"] == 1


def test_run_task_unknown_task_raises(orch):
    with pytest.raises(TestPilotError):
        orch.run_task(999)


def test_run_task_records_precheck_error_for_unrunnable_case(orch, db_conn):
    _draft_case(db_conn, "TC-9")
    task = orch.create_task({"name": "含草稿", "case_ids": ["TC-9"], "force": True})
    stats = orch.run_task(task["task_id"])
    assert stats["error"] == 1 and stats["total"] == 1

    run = repos.run_detail(db_conn, stats["run_id"])
    case = run["cases"][0]
    assert case["status"] == "error"
    steps = repos.case_steps(db_conn, case["id"])
    assert steps[0]["action"] == "precheck"
    assert "active" in steps[0]["error"]["message"]
    assert case["root_cause"]["category"] == "dsl_error"


def test_run_task_writeback_synced(cfg, registry, db_conn, tmp_path):
    zentao = FakeZentao(configured=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({"name": "回写", "case_ids": ["TC-1"], "remote": True})
    stats = o.run_task(task["task_id"])

    assert stats["writeback"] == "synced"
    assert zentao.writebacks[0]["task_id"] == 9001
    assert zentao.writebacks[0]["summary"]["pass"] == 1
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "synced"


def test_run_task_writeback_failure_then_compensation(cfg, registry, db_conn,
                                                      tmp_path):
    zentao = FakeZentao(configured=True, fail_writeback=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({"name": "回写失败", "case_ids": ["TC-1"], "remote": True})
    stats = o.run_task(task["task_id"])
    assert stats["writeback"] == "sync_pending"
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "sync_pending"

    results = o.sync_pending_tasks()
    assert results[0]["result"].startswith("failed")     # 仍然失败

    zentao.fail_writeback = False
    results = o.sync_pending_tasks()
    assert results[0]["result"] == "synced"
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "synced"


def test_sync_pending_reports_missing_zentao_id(cfg, registry, db_conn, tmp_path):
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({"name": "本地", "case_ids": ["TC-1"]})
    repos.set_task_sync(db_conn, task["task_id"], "sync_pending")
    assert o.sync_pending_tasks()[0]["result"] == "remote_ready"
    saved = o.list_tasks()[0]
    assert saved["zentao_task_id"] is not None
    assert saved["sync_status"] == "synced"


def test_sync_pending_can_target_single_task(cfg, registry, db_conn, tmp_path):
    """单任务补偿：Tasks 页行内「补偿」按钮必须只重推该任务。

    全量补偿会把用户没想动的任务也一起重推进禅道（外部可见的副作用），
    所以 ``task_id`` 参数是**行为约束**而非便利选项。
    """
    zentao = FakeZentao(configured=True, fail_writeback=True)
    o = _orch(cfg, registry, db_conn, tmp_path, zentao=zentao)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")

    # sync_pending 只在 run 的回写失败时产生 —— 必须真跑一次，不能直接置状态
    # （直接 set_task_sync 会绕过真实路径，锁不住"哪条路径会让它变成待补偿"）
    a = o.create_task({"name": "甲", "case_ids": ["TC-1"], "remote": True})
    b = o.create_task({"name": "乙", "case_ids": ["TC-1"], "remote": True})
    assert o.run_task(a["task_id"])["writeback"] == "sync_pending"
    assert o.run_task(b["task_id"])["writeback"] == "sync_pending"
    assert repos.get_task(db_conn, a["task_id"])["sync_status"] == "sync_pending"
    assert repos.get_task(db_conn, b["task_id"])["sync_status"] == "sync_pending"

    zentao.fail_writeback = False
    results = o.sync_pending_tasks(task_id=a["task_id"])
    assert [r["task_id"] for r in results] == [a["task_id"]]
    assert repos.get_task(db_conn, a["task_id"])["sync_status"] == "synced"
    # 乙必须原封不动 —— 这正是本用例要锁住的行为
    assert repos.get_task(db_conn, b["task_id"])["sync_status"] == "sync_pending"


def test_remote_sync_and_reconcile_are_blocked_during_run(orch):
    assert orch._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(TestPilotError, match="执行中"):
            orch.sync_pending_tasks()
        with pytest.raises(TestPilotError, match="执行中"):
            orch.reconcile_remote_task(1, 9001)
    finally:
        orch._run_lock.release()


def test_sync_pending_unknown_task_returns_empty(cfg, registry, db_conn, tmp_path):
    """指定不存在的任务时不报错也不误伤他人（返回空结果，调用方自行提示）。"""
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    t = o.create_task({"name": "本地", "case_ids": ["TC-1"]})
    repos.set_task_sync(db_conn, t["task_id"], "sync_pending")
    assert o.sync_pending_tasks(task_id=99999) == []
    # 全量补偿仍能找到它（证明上面不是靠"库里没数据"侥幸通过）
    assert o.sync_pending_tasks()[0]["task_id"] == t["task_id"]


def test_run_task_no_writeback_flag(orch):
    task = orch.create_task({"name": "跳过回写", "case_ids": ["TC-1"]})
    stats = orch.run_task(task["task_id"], writeback=False)
    assert stats["writeback"] == "skipped"


def test_run_task_abort_stops_dispatch(cfg, registry, db_conn, tmp_path):
    holder = {}

    class Aborting(FakeDriver):
        def click(self, el, ctx):
            holder["orch"].abort()
            return super().click(el, ctx)

    o = _orch(cfg, registry, db_conn, tmp_path, drivers={"uia": Aborting("uia")})
    holder["orch"] = o
    o.cases.pull([1, 2])
    for cid in ("TC-1", "TC-2"):
        o.cases.parse(cid)
        o.cases.accept(cid)

    task = o.create_task({"name": "中止", "case_ids": ["TC-1", "TC-2"]})
    stats = o.run_task(task["task_id"])

    assert stats["status"] == "aborted"
    assert stats["skip"] == 1
    assert stats["total"] == 1                  # 第二个用例不再派发


def test_events_forwarded_to_bus(cfg, registry, db_conn, tmp_path):
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e.event))
    o = _orch(cfg, registry, db_conn, tmp_path, bus=bus)
    o.cases.pull([1])
    o.cases.parse("TC-1")
    o.cases.accept("TC-1")
    task = o.create_task({"name": "事件", "case_ids": ["TC-1"]})
    o.run_task(task["task_id"])

    assert seen.count("run.progress") >= 3
    assert "case.finished" in seen and "step.finished" in seen


def test_health_and_close(orch):
    health = orch.health()
    assert health["db"]["ok"] is True
    assert health["drivers"]["uia"]["ok"] is True
    assert health["zentao"]["configured"] is False
    orch.close()
    assert orch.emulator.connected is False


def test_health_exposes_absolute_resource_paths(orch):
    """`health.paths` 是桌面壳注册读取白名单的**唯一来源**。

    缺了它整条资源链都断：壳的 `resolve_asset` 因"根目录未注册"拒绝一切读取
    → RunDetail 的截图永久空白、报告按钮点了没反应、设置页路径恒为 `—`。
    而且前端全部 `.catch(...)` 静默处理，不报错，所以只有这里能拦住。
    """
    paths = orch.health()["paths"]
    assert set(paths) == {"db", "screenshot_root", "report_root"}
    for key, value in paths.items():
        assert os.path.isabs(value), f"{key} 必须是绝对路径（壳的 cwd 不是数据根）: {value}"

    # 必须是**目录**：Rust 用 `candidate.starts_with(root)` 判白名单，
    # 若把 db 文件路径当根，只有它自己能通过，图表与报告全被拒。
    for key in ("screenshot_root", "report_root"):
        assert Path(paths[key]).is_dir(), f"{key} 未建目录，Rust canonicalize 会失败: {paths[key]}"
    # 截图与报告的根必须不同，否则互相覆盖
    assert paths["screenshot_root"] != paths["report_root"]
    assert Path(paths["db"]).parent.is_dir(), "db 的父目录必须存在"


def test_health_resource_paths_survive_unwritable_dir(orch, cfg):
    """单个路径建不出来时（权限/非法），health 仍要能返回 —— 前端会看到该键缺失。

    不能让整个 health 抛异常：那样设置页与状态徽章一起白屏，用户连"哪里坏了"都看不到。
    """
    from pathlib import Path as _P

    bad = _P("Z:/definitely/not/exist/shots") if os.name == "nt" else _P("/proc/nope/shots")
    cfg["app"]["screenshot_root"] = str(bad)
    paths = orch.resource_paths()
    assert "report_root" in paths, "一个路径坏掉不该连累其他路径"
    assert "screenshot_root" not in paths


def test_task_detail_and_list(orch, db_conn):
    task = orch.create_task({"name": "查询", "case_ids": ["TC-1"]})
    orch.run_task(task["task_id"])
    detail = orch.task_detail(task["task_id"])
    assert detail["name"] == "查询"
    assert len(detail["runs"]) == 1
    assert [t["id"] for t in orch.list_tasks()] == [task["task_id"]]
    assert orch.last_run_of_task(task["task_id"])["status"] == "completed"


def test_env_snapshot_probes_test_app_version(cfg, registry, db_conn, tmp_path,
                                              monkeypatch):
    """M2-5：配置了被测 exe 路径就自动读文件版本，写入 env_snapshot。"""
    from testpilot.infra import winver

    monkeypatch.setattr(winver, "file_version", lambda path: "9.9.9.9")
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cfg["app"]["test_app_exe"] = "C:/app/target.exe"

    env = o._collect_env({"case_ids": [], "driver_config": {}, "name": "t",
                          "suite_id": None}, None)
    assert env["test_app_version"] == "9.9.9.9"


def test_env_snapshot_prefers_explicit_version(cfg, registry, db_conn, tmp_path,
                                               monkeypatch):
    from testpilot.infra import winver

    def boom(path):
        raise AssertionError("显式配置时不应去探测文件")

    monkeypatch.setattr(winver, "file_version", boom)
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cfg["app"]["test_app_version"] = "1.0.0-custom"

    env = o._collect_env({"case_ids": [], "driver_config": {}, "name": "t",
                          "suite_id": None}, None)
    assert env["test_app_version"] == "1.0.0-custom"


def test_env_snapshot_unknown_when_probe_fails(cfg, registry, db_conn, tmp_path,
                                               monkeypatch):
    from testpilot.infra import winver

    monkeypatch.setattr(winver, "file_version", lambda path: None)
    o = _orch(cfg, registry, db_conn, tmp_path)
    o.cfg["app"]["test_app_exe"] = "C:/app/missing.exe"
    o.cfg["app"]["test_app_version"] = ""

    env = o._collect_env({"case_ids": [], "driver_config": {}, "name": "t",
                          "suite_id": None}, None)
    assert env["test_app_version"] == "unknown"


def test_env_snapshot_records_cases_and_mode(cfg, registry, db_conn, tmp_path):
    o = _orch(cfg, registry, db_conn, tmp_path)
    env = o._collect_env({"case_ids": [], "driver_config": {"mode": "uia_only"},
                          "name": "t", "suite_id": 3}, {"mode": "vision_first"})
    assert env["driver_mode"] == "vision_first"      # run 级覆盖优先
    assert env["suite_id"] == 3
    assert env["dsl_versions"] == {}
    assert env["screen"]


def test_driver_assembly_failure_is_tolerated(cfg, registry, db_conn, tmp_path):
    """未装 pywinauto 等依赖时装配不得崩（驱动缺失 -> 执行期明确报错）。"""
    o = TaskOrchestrator(cfg, registry, conn=db_conn,
                         recorder=Recorder(db_conn, tmp_path / "shots"),
                         zentao=FakeZentao(), llm=FakeLlm(),
                         emulator=FakeEmulator())
    assert isinstance(o.drivers, dict)          # 可能为空/部分驱动，但必须可用


@pytest.mark.parametrize("status", ["aborted", "running", "error", "interrupted"])
def test_compensation_never_completes_unfinished_run(orch, db_conn, status):
    task = orch.create_task({"name": "incomplete", "case_ids": ["TC-1"]})
    repos.set_task_sync(db_conn, task["task_id"], "sync_pending", 9981)
    rid = orch.recorder.begin_run({"task_id": task["task_id"]})
    orch.recorder.finish_run(rid, {"total": 1, "pass": 1, "status": status})
    result = orch.sync_pending_tasks(task["task_id"])
    assert result[0]["result"] == "not_completed"
    assert orch.zentao.writebacks == []
    assert repos.get_task(db_conn, task["task_id"])["sync_status"] == "sync_pending"


def test_compensation_marks_remote_ready_before_first_run(orch, db_conn):
    task = orch.create_task({"name": "create-only", "case_ids": ["TC-1"]})
    repos.set_task_sync(db_conn, task["task_id"], "sync_pending")
    result = orch.sync_pending_tasks(task["task_id"])
    assert result[0]["result"] == "remote_ready"
    saved = repos.get_task(db_conn, task["task_id"])
    assert saved["zentao_task_id"] is not None
    assert saved["sync_status"] == "synced"
    before = len(orch.zentao.created)
    assert orch.sync_pending_tasks(task["task_id"]) == []
    assert len(orch.zentao.created) == before


def test_compensation_does_not_reuse_older_completed_run(orch, db_conn):
    task = orch.create_task({"name": "latest-only", "case_ids": ["TC-1"]})
    orch.run_task(task["task_id"])
    repos.set_task_sync(db_conn, task["task_id"], "sync_pending", 9981)
    rid = orch.recorder.begin_run({"task_id": task["task_id"]})
    orch.recorder.finish_run(rid, {"total": 1, "status": "aborted"})
    assert orch.sync_pending_tasks(task["task_id"])[0]["result"] == "not_completed"
    assert orch.zentao.writebacks == []


def test_task_queries_keep_each_tasks_latest_run(orch, db_conn):
    completed = orch.create_task({"name": "completed", "case_ids": ["TC-1"]})["task_id"]
    active = orch.create_task({"name": "active", "case_ids": ["TC-1"]})["task_id"]
    idle = orch.create_task({"name": "idle", "case_ids": ["TC-1"]})["task_id"]
    recorder = Recorder(db_conn, orch.recorder.shot_root)
    old = recorder.begin_run({"task_id": completed})
    recorder.finish_run(old, {"status": "completed", "pass": 1})
    # 其他任务超过全局历史窗口，仍须保留 completed 的状态与详情。
    for _ in range(201):
        current = recorder.begin_run({"task_id": active})
        recorder.finish_run(current, {"status": "completed"})
    running = recorder.begin_run({"task_id": active})
    tasks = {task["id"]: task for task in orch.list_tasks()}
    assert tasks[completed]["runs"][0]["id"] == old
    assert tasks[completed]["runs"][0]["stats"]["pass"] == 1
    assert tasks[active]["runs"][0]["id"] == running
    assert tasks[active]["runs"][0]["status"] == "running"
    assert tasks[idle]["runs"] == []
    assert [r["id"] for r in orch.task_detail(completed)["runs"]] == [old]
    assert len(orch.task_detail(active)["runs"]) == 200

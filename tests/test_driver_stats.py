"""驱动降级统计单测 — 尝试链落库与聚合口径（设计 10 章"驱动降级率"）。

覆盖三层：
1. 纯函数聚合（``aggregate``）的边界口径；
2. 执行器真的把 uia→vision 降级写进轨迹链；
3. run 收尾把统计写进 ``stats.drivers``，报告可见。
"""
import json

from testpilot.core.contracts import ExecContext
from testpilot.engine.executor import Executor
from testpilot.engine.judge import Judge
from testpilot.engine.router import Router
from testpilot.engine.stats import aggregate, driver_stats
from testpilot.engine.verifier import Verifier
from testpilot.infra.abort import AbortSignal
from testpilot.knowledge.service import KnowledgeService
from testpilot.storage import repos
from testpilot.storage.recorder import Recorder
from tests.conftest import (FakeDriver, FakeEmulator, FakeLlm, make_case,
                            make_step, prepare_case)


# ---------- 纯函数 ----------

def _chain(*entries):
    return json.dumps([{"driver": d, "ok": ok, "ms": ms, "error": err}
                       for d, ok, ms, err in entries])


def test_aggregate_counts_downgrade_and_transition():
    rows = [
        # uia 定位失败 -> vision 成功：一次降级
        {"driver": "vision",
         "driver_chain_json": _chain(("uia", False, 1200, "locate_timeout"),
                                     ("vision", True, 800, None))},
        # uia 直接成功：不降级
        {"driver": "uia", "driver_chain_json": _chain(("uia", True, 35, None))},
        # 全部失败：不降级但计入失败
        {"driver": "vision",
         "driver_chain_json": _chain(("uia", False, 900, "locate_timeout"),
                                     ("vision", False, 700, "locate_timeout"))},
    ]
    out = aggregate(rows)
    assert out["steps"] == 3
    assert out["degraded_steps"] == 1
    assert out["failed_steps"] == 1
    assert abs(out["downgrade_rate"] - 1 / 3) < 5e-4    # 聚合值保留 4 位小数
    assert out["by_driver"]["uia"]["attempts"] == 3
    assert out["by_driver"]["uia"]["fail"] == 2
    assert out["by_driver"]["vision"]["success"] == 1
    assert out["transitions"] == [
        {"from": "uia", "to": "vision", "count": 2, "resolved": 1}]


def test_aggregate_falls_back_to_driver_column_without_chain():
    """历史数据（加入尝试链之前）不能统计成 0 —— 回退 driver 列，成败记 unknown。"""
    out = aggregate([{"driver": "uia", "driver_chain_json": None}])
    assert out["steps"] == 1
    assert out["degraded_steps"] == 0
    assert out["by_driver"]["uia"] == {"attempts": 1, "success": 0, "fail": 0,
                                       "ms": 0, "success_rate": None,
                                       "avg_ms": 0}


def test_aggregate_skips_steps_without_driver():
    assert aggregate([{"driver": None, "driver_chain_json": None}])["steps"] == 0
    assert aggregate([])["downgrade_rate"] is None


def test_aggregate_accepts_parsed_chain_list():
    """仓储已解析过的 ``driver_chain``（列表）与原始 JSON 串两种形态都认。"""
    out = aggregate([{"driver": "vision", "driver_chain": [
        {"driver": "uia", "ok": False, "ms": 1},
        {"driver": "vision", "ok": True, "ms": 2}]}])
    assert out["degraded_steps"] == 1


# ---------- 轨迹落库 ----------

def _make_executor(cfg, db, tmp_path, uia_fail=True):
    kb = KnowledgeService(db, cfg["knowledge"])
    emulator = FakeEmulator()
    llm = FakeLlm()
    drivers = {"uia": FakeDriver("uia", fail_locate=uia_fail),
               "vision": FakeDriver("vision")}
    recorder = Recorder(db, tmp_path / "shots")
    executor = Executor(
        drivers,
        Router(cfg["driver"], kb.load_router_stats, fail_streak_n=3),
        Verifier(drivers, emulator, llm, cfg["driver"]),
        Judge(llm, kb, cfg), kb, recorder, emulator, llm,
        cfg["driver"])
    return executor, drivers, recorder


def _run_one_ui_step(cfg, db, tmp_path, uia_fail=True):
    executor, drivers, recorder = _make_executor(cfg, db, tmp_path, uia_fail)
    case = make_case("TC-1", steps=[make_step(id="s1")])
    repos.create_case(db, case_id=case.case_id, zentao_case_id=1,
                      title=case.title, raw=None, zentao_updated_at="t")
    repos.create_task_record(db, name="t", case_ids=[case.case_id])
    run_id = recorder.begin_run({"task_id": 1})
    ce = recorder.begin_case(run_id, case)
    ctx = ExecContext(run_id=run_id, case_exec_id=ce, case=case,
                      abort=AbortSignal())
    result = executor.run_case(case, ctx, None)
    return run_id, ce, result


def test_executor_chain_records_uia_to_vision_downgrade(cfg, db_conn, tmp_path):
    run_id, ce, result = _run_one_ui_step(cfg, db_conn, tmp_path)
    assert result.status == "pass"            # 视觉兜底成功
    trace = repos.case_steps(db_conn, ce)
    chain = trace[0]["driver_chain"]
    assert [a["driver"] for a in chain] == ["uia", "vision"]
    assert chain[0]["ok"] is False and chain[0]["error"] == "locate_timeout"
    assert chain[1]["ok"] is True and chain[1]["error"] is None
    assert trace[0]["driver"] == "vision"

    stats = driver_stats(db_conn, run_id)
    assert stats["degraded_steps"] == 1
    assert stats["downgrade_rate"] == 1.0
    assert stats["transitions"][0]["from"] == "uia"
    assert stats["transitions"][0]["to"] == "vision"


def test_executor_chain_records_all_failures(cfg, db_conn, tmp_path):
    executor, drivers, recorder = _make_executor(cfg, db_conn, tmp_path)
    drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s1")])
    repos.create_case(db_conn, case_id=case.case_id, zentao_case_id=1,
                      title=case.title, raw=None, zentao_updated_at="t")
    repos.create_task_record(db_conn, name="t", case_ids=[case.case_id])
    run_id = recorder.begin_run({"task_id": 1})
    ce = recorder.begin_case(run_id, case)
    result = executor.run_case(case, ExecContext(
        run_id=run_id, case_exec_id=ce, case=case, abort=AbortSignal()), None)

    assert result.status == "fail"
    chain = repos.case_steps(db_conn, ce)[0]["driver_chain"]
    assert [(a["driver"], a["ok"]) for a in chain] == [("uia", False),
                                                       ("vision", False)]
    stats = driver_stats(db_conn, run_id)
    assert stats["failed_steps"] == 1
    assert stats["degraded_steps"] == 0


def test_app_step_chain_is_single_attempt(cfg, db_conn, tmp_path):
    executor, drivers, recorder = _make_executor(cfg, db_conn, tmp_path)
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="app.launch", key="app.window",
                  args={"exe": "target.exe"})])
    repos.create_case(db_conn, case_id=case.case_id, zentao_case_id=1,
                      title=case.title, raw=None, zentao_updated_at="t")
    repos.create_task_record(db_conn, name="t", case_ids=[case.case_id])
    run_id = recorder.begin_run({"task_id": 1})
    ce = recorder.begin_case(run_id, case)
    executor.run_case(case, ExecContext(
        run_id=run_id, case_exec_id=ce, case=case, abort=AbortSignal()), None)

    chain = repos.case_steps(db_conn, ce)[0]["driver_chain"]
    assert chain == [{"driver": "uia", "ok": True, "ms": chain[0]["ms"],
                      "error": None}]


# ---------- run 汇总与报告 ----------

def test_orchestrator_run_stats_and_report_include_driver_stats(
        cfg, db_conn, orch_factory, tmp_path):
    from testpilot.report.reporter import Reporter

    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True),
                                 "vision": FakeDriver("vision")})
    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "降级统计", "case_ids": ids})
    stats = orch.run_task(task["task_id"])

    drivers = stats["drivers"]
    assert drivers["degraded_steps"] >= 1
    assert drivers["transitions"][0]["from"] == "uia"
    assert drivers["by_driver"]["vision"]["success"] >= 1

    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "## 驱动与降级统计" in text
    assert "uia→vision" in text


def test_run_detail_carries_driver_stats_for_ui(cfg, db_conn, orch_factory):
    """RunDetail 前端直接消费 ``run.stats.drivers``（无需新 RPC）。"""
    from testpilot.storage.repos import run_detail

    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True),
                                 "vision": FakeDriver("vision")})
    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "UI 降级", "case_ids": ids})
    stats = orch.run_task(task["task_id"])

    detail = run_detail(db_conn, stats["run_id"])
    assert detail["stats"]["drivers"]["degraded_steps"] >= 1

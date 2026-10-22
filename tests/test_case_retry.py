"""失败用例复跑单测 —— flake 过滤（设计 6.2 的"偶发/必现"判据，第九轮）。

覆盖：
1. 复跑通过：run 只计一次 pass，`retry_cases`/`retry_passed` 记账，`case_exec.attempts=2`
   且 `retry_json` 留住首次失败的签名与归因（不被"最终通过就清空"吞掉）；
2. 复跑耗尽：仍判 fail，attempts = 1 + N；
3. 默认关闭：attempts=1、不写 retry_json；
4. 轨迹分次：`case_steps` 默认取最后一次尝试，显式 attempt=1 能拿到首次失败；
5. 环境快照记复跑策略（开过复跑的 run 与没开的 run 不该直接比通过率）；
6. RPC `task.run` 的 `caseRetry` 透传；报告概要行与用例复跑记录行。
"""
from testpilot.report.reporter import Reporter
from testpilot.rpc import RpcService
from testpilot.storage import repos
from tests.conftest import FakeDriver, FlakyDriver, prepare_case


def _one_case_run(orch, *, retry: int | None = None, snapshot: int | None = None):
    """拉取->解析->人审->建任务->执行，返回 (stats, task)。"""
    ids = prepare_case(orch, (1,))
    spec = {"name": "复跑", "case_ids": ids}
    if snapshot is not None:
        spec["driver_config"] = {"case_retry": snapshot}
    task = orch.create_task(spec)
    stats = orch.run_task(task["task_id"],
                          {"case_retry": retry} if retry is not None else None)
    return stats, task


def _case_exec(db, run_id: int) -> dict:
    rows = repos.run_case_execs(db, run_id)
    assert len(rows) == 1, "复跑必须复用同一个 case_exec 行（一条用例一票）"
    return rows[0]


# ---------- 复跑通过（flake） ----------

def test_retry_passed_counts_once_and_keeps_first_failure(cfg, db_conn, orch_factory):
    # 第一次定位失败、第二次成功：典型的环境抖动
    orch = orch_factory(drivers={"uia": FlakyDriver(fail_times=1)})
    stats, _ = _one_case_run(orch, retry=1)

    assert stats["status"] == "completed"
    assert (stats["total"], stats["pass"], stats["fail"]) == (1, 1, 0)
    assert stats["retry_cases"] == 1 and stats["retry_passed"] == 1

    row = _case_exec(db_conn, stats["run_id"])
    assert row["status"] == "pass" and row["attempts"] == 2

    detail = repos.run_detail(db_conn, stats["run_id"])
    attempts = detail["cases"][0]["retry"]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert [a["status"] for a in attempts] == ["fail", "pass"]
    assert attempts[0]["failure_signature"]          # 首次失败的签名留住了
    assert attempts[1]["failure_signature"] is None

    assert detail["env"]["case_retry"] == 1          # 策略入环境快照
    assert detail["stats"]["retry_passed"] == 1


def test_retry_trace_is_split_by_attempt(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FlakyDriver(fail_times=1)})
    stats, _ = _one_case_run(orch, retry=1)
    ce = _case_exec(db_conn, stats["run_id"])["id"]

    last = repos.case_steps(db_conn, ce)              # 默认最后一次尝试
    assert {s["attempt"] for s in last} == {2}
    # 注意用"错误类型"判定：verify_unknown（低置信不硬判）会带 error 但步骤是通过的
    assert not any((s["error"] or {}).get("type") == "locate_timeout" for s in last)

    first = repos.case_steps(db_conn, ce, attempt=1)
    assert {s["attempt"] for s in first} == {1}
    assert any((s["error"] or {}).get("type") == "locate_timeout"
               for s in first), "首次失败的轨迹必须可回放"

    rows = db_conn.execute("SELECT DISTINCT attempt FROM step_trace"
                           " WHERE case_exec_id=? ORDER BY attempt", (ce,)).fetchall()
    assert [r[0] for r in rows] == [1, 2]


# ---------- 复跑耗尽 / 关闭 ----------

def test_retry_exhausted_keeps_fail(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True)})
    stats, _ = _one_case_run(orch, snapshot=2)        # 任务快照里的策略

    assert (stats["total"], stats["pass"], stats["fail"]) == (1, 0, 1)
    assert stats["retry_cases"] == 1 and stats["retry_passed"] == 0
    row = _case_exec(db_conn, stats["run_id"])
    assert row["attempts"] == 3 and row["status"] == "fail"
    attempts = repos.run_detail(db_conn, stats["run_id"])["cases"][0]["retry"]
    assert [a["status"] for a in attempts] == ["fail", "fail", "fail"]


def test_retry_disabled_by_default(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True)})
    stats, _ = _one_case_run(orch)

    assert stats["retry_cases"] == 0 and stats["retry_passed"] == 0
    row = _case_exec(db_conn, stats["run_id"])
    assert row["attempts"] == 1 and row["retry_json"] is None
    assert repos.run_detail(db_conn, stats["run_id"])["cases"][0]["retry"] is None


def test_retry_count_is_capped(cfg, db_conn, orch_factory):
    """配置写错（填 99）不该把一个用例跑成小时级：上限见 MAX_CASE_RETRY。"""
    from testpilot.engine.orchestrator import MAX_CASE_RETRY

    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True)})
    stats, _ = _one_case_run(orch, retry=99)
    assert _case_exec(db_conn, stats["run_id"])["attempts"] == MAX_CASE_RETRY + 1


# ---------- RPC / 报告 ----------

def test_rpc_task_run_passes_case_retry(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FlakyDriver(fail_times=1)})
    service = RpcService(orch)
    ids = prepare_case(orch, (1,))
    created = service.dispatch("task.create", {"name": "rpc复跑", "caseIds": ids})

    stats = service.dispatch("task.run", {"taskId": created["task_id"],
                                          "caseRetry": 1})
    assert stats["retry_passed"] == 1
    detail = service.dispatch("run.detail", {"runId": stats["run_id"]})
    assert detail["cases"][0]["attempts"] == 2


def test_report_renders_retry_record_and_summary(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FlakyDriver(fail_times=1)})
    stats, _ = _one_case_run(orch, retry=1)

    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "- 失败复跑: 1 例（flake 过滤），其中 **1 例复跑后通过**" in text
    assert "- 复跑记录: 第1次 fail" in text
    assert "复跑后通过（疑似偶发）" in text
    assert "第2次 pass" in text


def test_report_omits_retry_lines_without_retry(cfg, db_conn, orch_factory):
    orch = orch_factory()
    stats, _ = _one_case_run(orch)
    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "失败复跑" not in text
    assert "复跑记录" not in text

"""回归质量统计单测 — 用例稳定性（跨 run）与两次 run 对比（设计 9.1 / 14.3）。

覆盖：
1. 稳定性口径（窗口截断、翻转、连续、四档判定、样本不足、空集合语义）；
2. 两次 run 对比（新增失败/修复/持续失败/增删用例 + run 摘要 + 参数校验）；
3. 报告集成（稳定性段 + 非通过用例的历史行）。
"""
import json

import pytest

from testpilot.engine.stats import (DEFAULT_WINDOW, LEVEL_BROKEN, LEVEL_FLAKY,
                                    LEVEL_STABLE, LEVEL_UNKNOWN, case_stability,
                                    compare_runs)
from testpilot.report.reporter import Reporter

T = "2026-01-01T00:00:00"


def _seed_case(conn, case_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO dsl_case(case_id, zentao_case_id, title, version,"
        " steps_json, updated_at) VALUES(?,?,?,?,?,?)",
        (case_id, int(case_id.split("-")[-1]), f"用例 {case_id}", 1, "[]", T))


def _seed_run(conn, run_id: int, cases, *, task_id: int = 1, status: str = "done",
              stats: dict | None = None, env: dict | None = None) -> int:
    """插入一次 run 及其用例执行记录；``cases`` = [(case_id, status[, 签名]), ...]。

    插入顺序即时间顺序（case_exec 按自增 id 排序 = 新→旧）。
    """
    conn.execute("INSERT OR IGNORE INTO test_task(id, name, case_ids_json, created_at)"
                 " VALUES(?,?,?,?)", (task_id, f"任务{task_id}", "[]", T))
    conn.execute("INSERT INTO test_run(id, task_id, started_at, finished_at, status,"
                 " env_snapshot_json, stats_json) VALUES(?,?,?,?,?,?,?)",
                 (run_id, task_id, T, T, status, json.dumps(env or {}),
                  json.dumps(stats or {})))
    for item in cases:
        case_id, case_status = item[0], item[1]
        signature = item[2] if len(item) > 2 else None
        _seed_case(conn, case_id)
        conn.execute(
            "INSERT INTO case_exec(run_id, case_id, dsl_version, status, duration_ms,"
            " failure_signature, started_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, case_id, 1, case_status, 10, signature, T))
    return run_id


# ---------- 用例稳定性 ----------

def test_stability_levels_and_shape(mem_conn):
    """四档判定：全通过=稳定、有输有赢=抖动、全失败=必现、单次样本=未知。"""
    plan = [
        ("pass", "pass", "fail", "fail"),
        ("fail", "pass", "fail", None),
        ("pass", "pass", "fail", None),
        ("fail", "pass", "fail", None),
    ]
    for idx, (c1, c2, c3, c4) in enumerate(plan, start=1):
        cases = [("TC-1", c1), ("TC-2", c2), ("TC-3", c3)]
        if c4:
            cases.append(("TC-4", c4))
        _seed_run(mem_conn, idx, cases)

    out = case_stability(mem_conn)

    one = out["TC-1"]
    assert one["recent"] == ["fail", "pass", "fail", "pass"]   # 新 -> 旧
    assert (one["runs"], one["pass"], one["fail"]) == (4, 2, 2)
    assert one["pass_rate"] == 0.5
    assert one["flips"] == 3
    assert one["streak"] == {"status": "fail", "len": 1}
    assert one["last_status"] == "fail"
    assert one["last_run_id"] == 4
    assert one["level"] == LEVEL_FLAKY

    two = out["TC-2"]
    assert two["level"] == LEVEL_STABLE and two["flips"] == 0
    assert two["streak"] == {"status": "pass", "len": 4}

    assert out["TC-3"]["level"] == LEVEL_BROKEN
    assert out["TC-3"]["pass"] == 0 and out["TC-3"]["pass_rate"] == 0.0

    # 只出现过一次：不下结论（首次失败不能报成"必现"）
    four = out["TC-4"]
    assert four["runs"] == 1 and four["last_status"] == "fail"
    assert four["level"] == LEVEL_UNKNOWN


def test_stability_window_truncation_and_exclude(mem_conn):
    for idx in range(1, 6):                       # 前 5 次全失败
        _seed_run(mem_conn, idx, [("TC-1", "fail")])
    for idx in range(6, 9):                       # 后 3 次全通过
        _seed_run(mem_conn, idx, [("TC-1", "pass")])

    recent2 = case_stability(mem_conn, ["TC-1"], window=2)["TC-1"]
    assert recent2["recent"] == ["pass", "pass"]
    assert recent2["runs"] == 2 and recent2["level"] == LEVEL_STABLE

    full = case_stability(mem_conn, ["TC-1"])["TC-1"]         # 默认窗口 10
    assert full["runs"] == 8 and full["pass"] == 3
    assert full["level"] == LEVEL_FLAKY                       # 窗口内翻过一次

    # 排除最新一次（那次是通过）：稳定性口径随之变化
    excluded = case_stability(mem_conn, ["TC-1"], exclude_run_id=8)["TC-1"]
    assert excluded["runs"] == 7 and excluded["pass"] == 2
    assert excluded["last_status"] == "pass" and excluded["last_run_id"] == 7
    assert DEFAULT_WINDOW == 10


def test_stability_counts_blocked_and_skip_as_not_pass(mem_conn):
    """通过率分母是全部样本：被阻塞/跳过的批次不能算通过。"""
    for idx, status in enumerate(["blocked", "skip", "pass"], start=1):
        _seed_run(mem_conn, idx, [("TC-1", status)])
    out = case_stability(mem_conn, ["TC-1"])["TC-1"]
    assert (out["pass"], out["blocked"], out["skip"]) == (1, 1, 1)
    assert out["pass_rate"] == round(1 / 3, 4)
    assert out["level"] == LEVEL_FLAKY


def test_stability_treats_retry_passed_as_flaky(mem_conn):
    """靠复跑兜住的用例本质就是不稳定的：全通过但出现过复跑 -> 抖动（第九轮口径）。"""
    _seed_run(mem_conn, 1, [("TC-1", "pass"), ("TC-2", "pass")])
    _seed_run(mem_conn, 2, [("TC-1", "pass"), ("TC-2", "pass")])
    mem_conn.execute("UPDATE case_exec SET attempts=2"
                     " WHERE run_id=2 AND case_id='TC-1'")

    out = case_stability(mem_conn, ["TC-1", "TC-2"])
    assert out["TC-1"]["level"] == LEVEL_FLAKY      # 两次都 pass，但有一次是复跑兜住的
    assert out["TC-1"]["retried"] == 1
    assert out["TC-2"]["level"] == LEVEL_STABLE     # 没复跑的正常用例不受影响
    assert out["TC-2"]["retried"] == 0

    # 报告里把这条讲清楚（否则用户看到"全通过却判抖动"会以为算错了）
    from testpilot.report.reporter import Reporter
    text = Reporter(mem_conn, {"app": {"report_root": "."}}).render(2)
    assert "| TC-1 | ✓✓ | 2/2 | 0 | 抖动（复跑通过 1 次） |" in text


def test_stability_empty_ids_means_empty_not_all(mem_conn):
    """空列表 != 全库：调用方传空集合时不该触发全表扫描。"""
    _seed_run(mem_conn, 1, [("TC-1", "fail")])
    assert case_stability(mem_conn, []) == {}
    assert list(case_stability(mem_conn)) == ["TC-1"]


# ---------- 两次 run 对比 ----------

def test_compare_runs_buckets(mem_conn):
    _seed_run(mem_conn, 1, [("TC-1", "pass"), ("TC-2", "fail", "sig-A"),
                            ("TC-3", "fail", "sig-B"), ("TC-4", "fail", "sig-C")])
    _seed_run(mem_conn, 2, [("TC-1", "fail", "sig-D"), ("TC-2", "pass"),
                            ("TC-3", "fail", "sig-E"), ("TC-4", "fail", "sig-C"),
                            ("TC-5", "pass")])

    out = compare_runs(mem_conn, 1, 2)

    assert [c["case_id"] for c in out["new_failures"]] == ["TC-1"]
    assert out["new_failures"][0]["base_status"] == "pass"
    assert out["new_failures"][0]["signature"] == "sig-D"

    assert [c["case_id"] for c in out["fixed"]] == ["TC-2"]
    assert out["fixed"][0]["base_signature"] == "sig-A"

    still = {c["case_id"]: c for c in out["still_failing"]}
    assert sorted(still) == ["TC-3", "TC-4"]
    assert still["TC-3"]["same_cause"] is False      # 签名变了 = 换了失败原因
    assert still["TC-3"]["target_signature"] == "sig-E"
    assert still["TC-4"]["same_cause"] is True

    assert out["added"] == [{"case_id": "TC-5", "target_status": "pass"}]
    assert out["removed"] == []
    assert out["counts"] == {"new_failures": 1, "fixed": 1, "still_failing": 2,
                             "added": 1, "removed": 0, "passed_both": 0}
    assert out["base"]["pass_rate"] == 0.25          # 1/4
    assert out["target"]["pass_rate"] == 0.4         # 2/5
    assert out["pass_rate_delta"] == pytest.approx(0.15)


def test_compare_runs_digest_and_validation(mem_conn):
    _seed_run(mem_conn, 1, [("TC-1", "pass")],
              env={"test_app_version": "1.0.0"},
              stats={"drivers": {"downgrade_rate": 0.25}})
    _seed_run(mem_conn, 2, [("TC-1", "fail")],
              task_id=2, env={"test_app_version": "2.0.0"},
              stats={"drivers": {"downgrade_rate": 0.5}})

    out = compare_runs(mem_conn, 1, 2)
    assert out["base"]["test_app_version"] == "1.0.0"
    assert out["target"]["test_app_version"] == "2.0.0"
    assert out["base"]["downgrade_rate"] == 0.25
    assert out["target"]["downgrade_rate"] == 0.5
    assert out["base"]["counts"] == {"pass": 1}
    assert out["target"]["pass_rate"] == 0.0
    assert out["base"]["task_id"] == 1 and out["target"]["task_id"] == 2

    with pytest.raises(ValueError, match="不能是同一个"):
        compare_runs(mem_conn, 1, 1)
    with pytest.raises(ValueError, match="run 不存在: 99"):
        compare_runs(mem_conn, 1, 99)


def test_compare_runs_same_cause_unknown_without_signatures(mem_conn):
    _seed_run(mem_conn, 1, [("TC-1", "fail")])
    _seed_run(mem_conn, 2, [("TC-1", "fail")])
    out = compare_runs(mem_conn, 1, 2)
    assert out["still_failing"][0]["same_cause"] is None      # 两侧无签名，无法判定


# ---------- 报告集成 ----------

def test_report_stability_section_and_case_history(cfg, mem_conn):
    _seed_run(mem_conn, 1, [("TC-1", "pass"), ("TC-2", "pass")])
    _seed_run(mem_conn, 2, [("TC-1", "fail", "sig-x"), ("TC-2", "fail", "sig-y")])
    _seed_run(mem_conn, 3, [("TC-1", "fail", "sig-x"), ("TC-2", "pass")])

    text = Reporter(mem_conn, cfg).render(3)
    assert "## 回归稳定性（跨 run）" in text
    assert "抖动=有输有赢或复跑后才通过（疑似偶发）" in text
    assert "| TC-1 | ✗✗✓ | 1/3 | 1 | 抖动 |" in text   # 新→旧：失败/失败/通过
    assert "| TC-2 | ✓✗✓ | 2/3 | 2 | 抖动 |" in text
    assert "必现失败 |" not in text.split("| 用例 |")[1]   # 两条都是抖动，无必现
    # 用例明细里给非通过用例补历史（通过用例不加噪声）
    assert "- 历史稳定性: 最近 3 次执行中 1 次通过，状态翻转 1 次（抖动）" in text
    assert text.count("- 历史稳定性:") == 1


def test_report_stability_section_absent_when_all_stable(cfg, mem_conn):
    _seed_run(mem_conn, 1, [("TC-1", "pass")])
    _seed_run(mem_conn, 2, [("TC-1", "pass")])
    text = Reporter(mem_conn, cfg).render(2)
    assert "## 回归稳定性（跨 run）" not in text


# ---------- 失败签名历史画像（设计 9.4） ----------

def _seed_failure_exec(conn, run_id, case_id, sig, category, suggestion):
    conn.execute("INSERT OR IGNORE INTO test_task(id, name, case_ids_json, created_at)"
                 " VALUES(1, '任务1', '[]', ?)", (T,))
    conn.execute("INSERT OR IGNORE INTO test_run(id, task_id, started_at, finished_at,"
                 " status, env_snapshot_json, stats_json)"
                 " VALUES(?,1,?,?,'done','{}','{}')", (run_id, T, T))
    _seed_case(conn, case_id)
    cur = conn.execute(
        "INSERT INTO case_exec(run_id, case_id, dsl_version, status, duration_ms,"
        " failure_signature, root_cause_json, started_at) VALUES(?,?,1,'fail',10,?,?,?)",
        (run_id, case_id, sig,
         json.dumps({"category": category, "suggestion": suggestion}), T))
    return cur.lastrowid


def _add_step(conn, case_exec_id, step_no, *, verify=None, error=None):
    conn.execute("INSERT INTO step_trace(case_exec_id, step_no, step_id, action,"
                 " verify_result, error_json, at) VALUES(?,?,?,?,?,?,?)",
                 (case_exec_id, step_no, f"s{step_no}", "ui.click", verify, error, T))


def test_signature_history_profile(mem_conn):
    from testpilot.engine.stats import signature_history

    sig = "sig-quick"
    _seed_failure_exec(mem_conn, 1, "TC-1", sig, "uia_change", "旧建议")
    _seed_failure_exec(mem_conn, 2, "TC-2", sig, "timing", "")
    ce3 = _seed_failure_exec(mem_conn, 3, "TC-3", sig, "uia_change", "")
    # 最近一次：step1 报 device_error、step2 验证失败 -> 失败点应取 step2
    _add_step(mem_conn, ce3, 1, error=json.dumps({"type": "device_error"}))
    _add_step(mem_conn, ce3, 2, verify="fail")

    hist = signature_history(mem_conn, sig)
    assert hist["count"] == 3
    assert hist["categories"] == [{"category": "uia_change", "count": 2},
                                  {"category": "timing", "count": 1}]
    assert hist["last"]["run_id"] == 3 and hist["last"]["case_exec_id"] == ce3
    assert hist["last"]["step_no"] == 2 and hist["last"]["case_id"] == "TC-3"
    assert hist["last"]["at"] == T
    assert hist["suggestion"] == "旧建议"        # 最近一次无建议则回溯历史
    assert signature_history(mem_conn, "") is None
    assert signature_history(mem_conn, "unknown-sig") is None


def test_report_includes_quick_locate_line(cfg, mem_conn):
    sig = "sig-quick"
    _seed_failure_exec(mem_conn, 1, "TC-1", sig, "uia_change", "重试后通过")
    ce2 = _seed_failure_exec(mem_conn, 2, "TC-1", sig, "timing", "插入 wait_ready")
    _add_step(mem_conn, ce2, 1, verify="fail")

    text = Reporter(mem_conn, cfg).render(2)
    assert "快速定位: 该签名历史出现 **2** 次" in text
    assert "根因分布: timing 1 / uia_change 1" in text
    assert "最近一次: run#2 / TC-1 / step#1" in text
    assert "建议: 插入 wait_ready" in text

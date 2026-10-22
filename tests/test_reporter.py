"""报告生成单测 — 步骤轨迹/原文对照/截图/归因分布（M1-6）。"""
import json
from pathlib import Path

import pytest

from testpilot.report.reporter import Reporter
from testpilot.storage import repos
from tests.conftest import FakeDriver, prepare_case


@pytest.fixture()
def passed_run(cfg, db_conn, orch_factory, tmp_path):
    orch = orch_factory()
    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "报告任务", "case_ids": ids})
    stats = orch.run_task(task["task_id"])
    return orch, stats


@pytest.fixture()
def failed_run(cfg, db_conn, orch_factory):
    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True),
                                 "vision": FakeDriver("vision", fail_locate=True)})
    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "失败任务", "case_ids": ids})
    stats = orch.run_task(task["task_id"])
    return orch, stats


def test_generate_writes_report_file(passed_run, cfg, db_conn):
    orch, stats = passed_run
    path = Reporter(db_conn, cfg).generate(stats["run_id"])
    assert path.exists()
    assert path.name == "report.md"
    text = path.read_text(encoding="utf-8")
    assert f"# Run#{stats['run_id']} 执行报告" in text
    assert "- 结果: 通过 **1**" in text


def test_report_contains_step_trace_with_source_text(passed_run, cfg, db_conn):
    orch, stats = passed_run
    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "## 用例明细" in text
    assert "| # | 动作 | 用例原文 | 驱动 |" in text
    assert "点击开始检测" in text                 # 用例原文对照
    assert "ui.click" in text
    assert "## 环境快照" in text
    env = json.loads(text.split("```json")[1].split("```")[0])
    assert env["driver_mode"] == "auto"
    assert env["case_ids"] == ["TC-1"]


def test_report_includes_screenshots_for_failed_run(failed_run, cfg, db_conn):
    orch, stats = failed_run
    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "截图:" in text
    assert "../../shots/" in text.replace("\\", "/")
    assert "## 失败归因分布" in text
    assert "uia_change" in text


def test_report_root_cause_section_for_passing_run_absent(passed_run, cfg, db_conn):
    orch, stats = passed_run
    assert "## 失败归因分布" not in Reporter(db_conn, cfg).render(stats["run_id"])


def test_report_renders_llm_usage_and_writeback(passed_run, cfg, db_conn):
    orch, stats = passed_run
    assert "LLM: 调用" in Reporter(db_conn, cfg).render(stats["run_id"])
    assert "禅道回写: local_only" in Reporter(db_conn, cfg).render(stats["run_id"])


def test_report_renders_case_ctx_maintenance_line(cfg, db_conn, orch_factory):
    """人审维护的 case_ctx 必须出现在报告里（否则维护了看不见，等于白维护）。"""
    orch = orch_factory()
    ids = prepare_case(orch, (1,))
    orch.kb.save_case_ctx(ids[0], {"scene": "normal_sample_a",
                                   "pitfalls": ["预热要等 3s"],
                                   "notes": "人手改过案例"})
    task = orch.create_task({"name": "上下文报告", "case_ids": ids})
    stats = orch.run_task(task["task_id"])

    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "用例上下文（人审维护）" in text
    assert "预热要等 3s" in text
    assert "人手改过案例" in text


def test_report_renders_env_compat_matrix(cfg, db_conn, orch_factory):
    """两次不同被测版本的 run 之后，报告里能看到「版本 × 用例」通过率矩阵。"""
    orch = orch_factory()
    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "矩阵报告", "case_ids": ids})
    first = orch.run_task(task["task_id"])             # version=unknown

    orch.cfg["app"]["test_app_version"] = "2.0.0"
    second = orch.run_task(task["task_id"])

    assert second["env_compat"]["signature"].endswith(
        first["env_compat"]["signature"].split("|", 1)[1])
    text = Reporter(db_conn, cfg).render(second["run_id"])
    assert "## 历史环境通过率（env_compat 矩阵）" in text
    assert "unknown" in text and "2.0.0" in text
    assert ids[0] in text


def test_report_shows_similar_history(cfg, db_conn, orch_factory):
    """相似历史对照（M4-5）：一次模糊命中之外，其余同族历史失败列为"相似历史"。

    注意语义：置信度最高的那条会作为**模糊命中**（决定本次处置参考），
    其余同族条目才以"相似历史"形式呈现 —— 因此这里预置两条。
    """
    from testpilot.knowledge.service import FailureKnowledge
    from testpilot.knowledge.signature import FailureSignature

    orch = orch_factory(drivers={"uia": FakeDriver("uia", fail_locate=True),
                                "vision": FakeDriver("vision", fail_locate=True)})

    def seed(key, confidence, suggestion):
        sig = FailureSignature("locate_timeout", "ui.click", key, "main")
        orch.kb.confirm(orch.kb.save_failure(FailureKnowledge(
            entry_id=0, signature=sig.digest(), category="uia_change",
            disposition="none", suggestion=suggestion, confidence=confidence,
            error_type=sig.error_type, action=sig.action,
            target_key=sig.target_key, page_key=sig.page_key)), "active")
        return sig.digest()

    seed("btn.other_detect", 0.95, "命中建议：更新控件名")
    similar_digest = seed("btn.third_detect", 0.5, "历史建议：补 fallback")

    ids = prepare_case(orch, (1,))
    task = orch.create_task({"name": "相似历史", "case_ids": ids})
    stats = orch.run_task(task["task_id"])

    text = Reporter(db_conn, cfg).render(stats["run_id"])
    assert "命中建议：更新控件名" in text          # 模糊命中作为本次参考
    assert "相似历史:" in text
    assert similar_digest in text
    assert "历史建议：补 fallback" in text
    assert "命中 0 次" in text


def test_report_unknown_run_raises(cfg, db_conn):
    with pytest.raises(ValueError):
        Reporter(db_conn, cfg).generate(999)
    with pytest.raises(ValueError):
        Reporter(db_conn, cfg).render(999)


def test_report_escapes_pipe_in_cells(cfg, db_conn):
    """错误信息含竖线时不得破表。"""
    conn = db_conn
    repos.create_case(conn, case_id="TC-1", zentao_case_id=1, title="t",
                      raw=None, zentao_updated_at="t")
    repos.create_task_record(conn, name="n", case_ids=["TC-1"])
    from testpilot.storage.recorder import Recorder
    rec = Recorder(conn, Path(cfg["app"]["screenshot_root"]))
    run_id = rec.begin_run({"task_id": 1})
    from testpilot.core.contracts import StepResult

    class _Case:
        case_id = "TC-1"
        zentao_case_id = 1
        version = 1

    ce = rec.begin_case(run_id, _Case())
    rec.record_step(ce, StepResult(step_no=1, step_id="s1", action="ui.click",
                                   status="fail",
                                   error={"type": "x", "message": "a|b"}))
    rec.finish_case(ce, "fail", "sig", None, 1, None)
    rec.finish_run(run_id, {"status": "completed", "total": 1})
    text = Reporter(conn, cfg).render(run_id)
    assert "a\\|b" in text

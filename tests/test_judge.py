"""失败归因单测 — 规则层 / 知识库命中 / LLM 层 / 入库策略（设计 8.9、3.3）。"""
from testpilot.core.contracts import ExecContext, StepResult, VerifyResult
from testpilot.core.dsl import Locator, Step, TargetDesc, TestCase
from testpilot.engine.judge import DISPOSITION, Judge
from testpilot.knowledge.service import FailureKnowledge, KnowledgeService
from testpilot.knowledge.signature import FailureSignature
from tests.conftest import FakeLlm


def _ctx():
    step = Step(id="s1", action="ui.click",
                target=TargetDesc(key="btn.start_detect",
                                  primary=Locator(type="uia", name="开始检测")))
    case = TestCase(case_id="TC-1", steps=[step])
    return ExecContext(run_id=1, case_exec_id=1, case=case)


def _res(error, verify=None, target_json=None):
    return StepResult(step_no=1, step_id="s1", action="ui.click", status="fail",
                      driver="uia", error=error, verify=verify,
                      target_json=target_json)


def _judge(mem_conn, llm=None):
    kb = KnowledgeService(mem_conn, {})
    return Judge(llm or FakeLlm(), kb, {}), kb


def _entries(mem_conn):
    return [dict(r) for r in mem_conn.execute(
        "SELECT id, signature, payload_json, status FROM kb_entry"
        " WHERE type='failure'")]


def test_rule_locate_timeout_is_uia_change_pending(mem_conn):
    judge, _ = _judge(mem_conn)
    sig = FailureSignature("locate_timeout", "ui.click", "btn.start_detect", "main")
    report = judge.analyze(_res({"type": "locate_timeout"}),
                           sig, None, _ctx())
    assert report.category == "uia_change"
    assert report.source == "rule"
    assert report.needs_review is True
    entries = _entries(mem_conn)
    assert len(entries) == 1
    assert entries[0]["status"] == "pending"           # 规则结论也需人审


def test_rule_device_error_is_env_and_auto_active(mem_conn):
    judge, _ = _judge(mem_conn)
    sig = FailureSignature("device_error", "device.call", "d", "main")
    report = judge.analyze(_res({"type": "device_error", "message": "超时"}),
                           sig, None, _ctx())
    assert report.category == "env"
    assert report.needs_review is False
    assert _entries(mem_conn)[0]["status"] == "active"


def test_low_confidence_verify_rule_escalates_to_llm(mem_conn):
    judge, _ = _judge(mem_conn)
    sig = FailureSignature("verify_failed", "ui.wait", "tbl.result", "main")
    report = judge.analyze(
        _res({"type": "verify_failed"},
             verify=VerifyResult(status="fail", detail="变化像素比 0.0001")),
        sig, None, _ctx())
    assert report.category == "uia_change"
    assert report.source == "llm"
    assert "变化像素比" in report.evidence


def test_rule_dsl_error_and_app_crash(mem_conn):
    judge, _ = _judge(mem_conn)
    sig = FailureSignature("dsl_error", "ui.click", "", "main")
    assert judge.analyze(_res({"type": "dsl_error", "message": "缺 target"}),
                         sig, None, _ctx()).category == "dsl_error"

    sig2 = FailureSignature("app_error", "app.launch", "", "main")
    crash = judge.analyze(_res({"type": "app_error", "message": "被测程序崩溃退出"}),
                          sig2, None, _ctx())
    assert crash.category == "app_crash"
    assert crash.needs_review is False


def test_kb_hit_short_circuits_without_new_entry(mem_conn):
    judge, kb = _judge(mem_conn)
    sig = FailureSignature("verify_failed", "ui.click", "btn.start", "main")
    hit = FailureKnowledge(entry_id=42, signature=sig.digest(),
                           category="timing", disposition="none",
                           suggestion="加 ui.wait", confidence=0.8,
                           error_type=sig.error_type, action=sig.action,
                           target_key=sig.target_key, page_key=sig.page_key)
    report = judge.analyze(_res({"type": "verify_failed"}), sig, hit, _ctx())
    assert report.source == "kb_fuzzy"
    assert report.suggestion == "加 ui.wait"
    assert _entries(mem_conn) == []                    # 不重复入库


def test_llm_path_for_unknown_error_type(mem_conn):
    llm = FakeLlm(judge={"category": "product_bug", "confidence": 0.66,
                         "evidence": "结果表格无数据", "suggestion": "提缺陷单"})
    judge, _ = _judge(mem_conn, llm)
    sig = FailureSignature("mystery_type", "ui.click", "btn.start", "main")
    report = judge.analyze(_res({"type": "mystery_type"}), sig, None, _ctx())
    assert report.category == "product_bug"
    assert report.source == "llm"
    assert report.needs_review is True
    entry = _entries(mem_conn)[0]
    assert entry["status"] == "pending"
    assert "product_bug" in entry["payload_json"]


def test_llm_unknown_category_coerced(mem_conn):
    llm = FakeLlm(judge={"category": "外星人干的", "confidence": 0.9})
    judge, _ = _judge(mem_conn, llm)
    sig = FailureSignature("mystery", "ui.click", "k", "main")
    report = judge.analyze(_res({"type": "mystery"}), sig, None, _ctx())
    assert report.category == "unknown"


def test_llm_exception_degrades_to_unknown(mem_conn):
    class _Boom(FakeLlm):
        def judge_failure(self, ctx):
            raise RuntimeError("llm down")

    judge, _ = _judge(mem_conn, _Boom())
    sig = FailureSignature("mystery", "ui.click", "k", "main")
    report = judge.analyze(_res({"type": "mystery"}), sig, None, _ctx())
    assert report.category == "unknown"
    assert report.confidence <= 0.3
    assert report.needs_review is True


def test_llm_missing_gateway_degrades(mem_conn):
    judge, _ = _judge(mem_conn, llm=None)
    judge.llm = None
    sig = FailureSignature("mystery", "ui.click", "k", "main")
    report = judge.analyze(_res({"type": "mystery"}), sig, None, _ctx())
    assert report.category == "unknown" and report.evidence == "LLM 网关未装配"


def test_llm_prompt_payload_contains_step_and_screenshots(mem_conn):
    captured = {}

    class _Spy(FakeLlm):
        def judge_failure(self, ctx):
            captured.update(ctx)
            return {"category": "timing", "confidence": 0.5}

    res = StepResult(step_no=1, step_id="s1", action="ui.click", status="fail",
                     driver="uia", error={"type": "mystery"},
                     source_text="点击开始检测",
                     screenshot_before="1/1/1_before.png")
    judge, _ = _judge(mem_conn, _Spy())
    sig = FailureSignature("mystery", "ui.click", "k", "main")
    judge.analyze(res, sig, None, _ctx())
    assert captured["case_id"] == "TC-1"
    assert captured["step"]["source_text"] == "点击开始检测"
    assert captured["screenshots"] == ["1/1/1_before.png"]


def test_report_attaches_similar_history(mem_conn):
    """相似历史对照：同 action + 同类型/同 target 前缀的历史失败会被挂到归因结果上。"""
    judge, kb = _judge(mem_conn)
    old = FailureSignature("locate_timeout", "ui.click", "btn.start_detect", "main")
    kb.confirm(kb.save_failure(FailureKnowledge(
        entry_id=0, signature=old.digest(), category="uia_change",
        disposition="none", suggestion="补 fallback", confidence=0.9,
        error_type=old.error_type, action=old.action, target_key=old.target_key,
        page_key=old.page_key)), "active")

    sig = FailureSignature("locate_timeout", "ui.click", "btn.stop_detect", "main")
    report = judge.analyze(_res({"type": "locate_timeout"}), sig, None, _ctx())

    assert [r["signature"] for r in report.similar] == [old.digest()]
    assert report.similar[0]["category"] == "uia_change"
    assert report.similar[0]["suggestion"] == "补 fallback"
    # 相似历史只作参考，不影响本次判定
    assert report.category == "uia_change" and report.source == "rule"


def test_similar_history_excludes_current_entry_on_kb_hit(mem_conn):
    judge, kb = _judge(mem_conn)
    sig = FailureSignature("verify_failed", "ui.click", "btn.start", "main")
    entry_id = kb.save_failure(FailureKnowledge(
        entry_id=0, signature=sig.digest(), category="timing",
        disposition="none", suggestion="加等待", confidence=0.8,
        error_type=sig.error_type, action=sig.action, target_key=sig.target_key,
        page_key=sig.page_key))
    kb.confirm(entry_id, "active")
    hit = kb.match_failure(sig)

    report = judge.analyze(_res({"type": "verify_failed"}), sig, hit, _ctx())
    assert report.source == "kb_fuzzy"
    assert report.similar == []                 # 自己不算"相似历史"


def test_similar_history_query_failure_does_not_break_analysis(mem_conn):
    judge, kb = _judge(mem_conn)

    def boom(sig, limit=3):
        raise RuntimeError("db locked")

    kb.similar_failures = boom
    sig = FailureSignature("locate_timeout", "ui.click", "btn.a", "main")
    report = judge.analyze(_res({"type": "locate_timeout"}), sig, None, _ctx())
    assert report.category == "uia_change"
    assert report.similar == []


def test_similar_top_n_from_config(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    judge = Judge(FakeLlm(), kb, {"knowledge": {"similar_top_n": 1}})
    for i in range(3):
        s = FailureSignature("locate_timeout", "ui.click", f"btn.a{i}", "main")
        kb.confirm(kb.save_failure(FailureKnowledge(
            entry_id=0, signature=s.digest(), category="uia_change",
            disposition="none", suggestion="", confidence=0.5,
            error_type=s.error_type, action=s.action, target_key=s.target_key,
            page_key=s.page_key)), "active")

    sig = FailureSignature("locate_timeout", "ui.click", "btn.x", "main")
    assert len(judge.analyze(_res({"type": "locate_timeout"}), sig, None,
                             _ctx()).similar) == 1


def test_disposition_mapping_used_on_persist(mem_conn):
    judge, _ = _judge(mem_conn)
    # product_bug 走 LLM 路径 -> disposition 映射为 block_known
    judge.llm.judge_result = {"category": "product_bug", "confidence": 0.9}
    sig = FailureSignature("mystery", "ui.click", "k", "main")
    judge.analyze(_res({"type": "mystery"}), sig, None, _ctx())
    assert '"disposition": "block_known"' in _entries(mem_conn)[0]["payload_json"]
    assert DISPOSITION["timing"] == "retry_ready"
    assert DISPOSITION["unknown"] == "none"

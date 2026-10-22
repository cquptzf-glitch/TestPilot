"""失败签名单测（设计 8.8）。"""
from testpilot.core.contracts import ExecContext, StepResult
from testpilot.core.dsl import Locator, Step, TargetDesc, TestCase
from testpilot.knowledge.signature import FailureSignature


def _ctx(page_key=None):
    step = Step(id="s5", action="ui.click",
                target=TargetDesc(key="btn.start_detect",
                                  primary=Locator(type="uia", name="开始检测")))
    case = TestCase(case_id="TC-1024", steps=[step])
    ctx = ExecContext(run_id=1, case_exec_id=1, case=case)
    if page_key:
        ctx.extra["page_key"] = page_key
    return ctx


def test_digest_stable_and_readable():
    sig = FailureSignature("verify_failed", "ui.click", "btn.start_detect", "main")
    sig2 = FailureSignature("verify_failed", "ui.click", "btn.start_detect", "main")
    assert sig.digest() == sig2.digest()
    assert len(sig.digest()) == 12
    assert str(sig) == "verify_failed|ui.click|btn.start_detect@main"


def test_digest_changes_with_any_element():
    base = FailureSignature("verify_failed", "ui.click", "a", "main").digest()
    assert FailureSignature("locate_timeout", "ui.click", "a", "main").digest() != base
    assert FailureSignature("verify_failed", "ui.input", "a", "main").digest() != base
    assert FailureSignature("verify_failed", "ui.click", "b", "main").digest() != base
    assert FailureSignature("verify_failed", "ui.click", "a", "settings").digest() != base


def test_from_result_extracts_target_key_and_page():
    ctx = _ctx(page_key="settings")
    res = StepResult(step_no=1, step_id="s5", action="ui.click", status="fail",
                     driver="uia", error={"type": "verify_failed"})
    sig = FailureSignature.from_result(res, ctx)
    assert sig.target_key == "btn.start_detect"
    assert sig.error_type == "verify_failed"
    assert sig.action == "ui.click"
    assert sig.page_key == "settings"


def test_from_result_defaults_when_no_error_and_no_target():
    case = TestCase(case_id="TC-1", steps=[Step(id="s1", action="sleep")])
    ctx = ExecContext(run_id=1, case_exec_id=1, case=case)
    res = StepResult(step_no=1, step_id="s1", action="sleep", status="fail")
    sig = FailureSignature.from_result(res, ctx)
    assert sig.error_type == "unknown"
    assert sig.target_key == ""
    assert sig.page_key == "main"


def test_from_result_tolerates_step_no_out_of_range():
    ctx = _ctx()
    res = StepResult(step_no=99, step_id="sX", action="ui.click", status="fail")
    sig = FailureSignature.from_result(res, ctx)
    assert sig.target_key == ""

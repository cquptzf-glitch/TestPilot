"""验证器单测 — 四类断言 + 置信度门禁（设计 8.6 / 6.3）。"""
import pytest

from testpilot.core.contracts import ExecContext, Screenshot
from testpilot.core.dsl import Step, TestCase, VerifySpec  # noqa: F401
from testpilot.engine.verifier import Verifier


def _ctx():
    return ExecContext(run_id=1, case_exec_id=1, case=TestCase(case_id="TC-1"))


def _step(verify: dict, target=None, args=None):
    data = {"id": "s1", "action": "assert.ui", "verify": verify}
    if target:
        data["target"] = target
    if args:
        data["args"] = args
    return Step.model_validate(data)


def _verifier(cfg, drivers=None, emulator=None, llm=None):
    return Verifier(drivers or {}, emulator, llm, cfg["driver"])


# ---------- none ----------

def test_verify_none_passes(cfg):
    vr = _verifier(cfg).verify(_step({"type": "none"}), _ctx(), None, None)
    assert vr.status == "pass"


# ---------- text ----------

def test_verify_text_contains_equals_regex(cfg, fake_drivers):
    fake_drivers["uia"].text = "结果: S001 已完成"
    v = _verifier(cfg, drivers=fake_drivers)
    step = _step({"type": "text", "expect": "S001", "op": "contains"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    assert v.verify(step, _ctx(), None, None).status == "pass"

    step = _step({"type": "text", "expect": "结果: S001 已完成", "op": "equals"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    assert v.verify(step, _ctx(), None, None).status == "pass"

    step = _step({"type": "text", "expect": r"S\d{3}", "op": "regex"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    assert v.verify(step, _ctx(), None, None).status == "pass"

    step = _step({"type": "text", "expect": "S999", "op": "contains"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    vr = v.verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "S999" in vr.detail


def test_verify_text_uses_step_target_when_verify_target_absent(cfg, fake_drivers):
    step = _step({"type": "text", "expect": "OK"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    assert _verifier(cfg, drivers=fake_drivers).verify(
        step, _ctx(), None, None).status == "pass"


def test_verify_text_without_uia_driver_fails(cfg):
    step = _step({"type": "text", "expect": "x"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    vr = _verifier(cfg).verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "UIA" in vr.detail


def test_verify_text_without_any_target_fails(cfg, fake_drivers):
    vr = _verifier(cfg, drivers=fake_drivers).verify(
        _step({"type": "text", "expect": "x"}), _ctx(), None, None)
    assert vr.status == "fail" and "target" in vr.detail


def test_verify_text_invalid_regex_reports_fail(cfg, fake_drivers):
    step = _step({"type": "text", "expect": "([", "op": "regex"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    vr = _verifier(cfg, drivers=fake_drivers).verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "正则" in vr.detail


def test_verify_text_locate_error_is_fail(cfg):
    from tests.conftest import FakeDriver
    drv = FakeDriver("uia", fail_locate=True)
    step = _step({"type": "text", "expect": "x"},
                 target={"key": "k", "primary": {"type": "uia", "name": "结果"}})
    vr = _verifier(cfg, drivers={"uia": drv}).verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "断言定位/读取失败" in vr.detail


# ---------- state ----------

def test_verify_state_pass_and_fail(cfg, fake_emulator):
    v = _verifier(cfg, emulator=fake_emulator)
    ok = _step({"type": "state"}, args={"path": "/state/run", "expect": "completed"})
    assert v.verify(ok, _ctx(), None, None).status == "pass"
    bad = _step({"type": "state"}, args={"path": "/state/run", "expect": "idle"})
    vr = v.verify(bad, _ctx(), None, None)
    assert vr.status == "fail" and "idle" in vr.detail


def test_verify_state_contains_operator(cfg):
    from tests.conftest import FakeEmulator
    emu = FakeEmulator(state={"state": {"run": "completed with warnings"}})
    step = _step(
        {"type": "state", "expect": "completed", "op": "contains"},
        args={"path": "/state/run"},
    )
    assert _verifier(cfg, emulator=emu).verify(
        step, _ctx(), None, None).status == "pass"


def test_verify_state_without_emulator_fails(cfg):
    vr = _verifier(cfg).verify(_step({"type": "state"}), _ctx(), None, None)
    assert vr.status == "fail" and "模拟器" in vr.detail


def test_verify_state_path_passed_through(cfg):
    """path 原样传给模拟器（模拟器侧按 JSON 路径解析，驱动不做路径加工）。"""
    from tests.conftest import FakeEmulator
    emu = FakeEmulator(state={"state": {"grid_a": {"env": "ok"}}})
    step = _step({"type": "state"}, args={"path": "/state/grid_a/env", "expect": "ok"})
    assert _verifier(cfg, emulator=emu).verify(step, _ctx(), None, None).status == "pass"
    assert emu.calls[-1] == ("query_state", {"path": "/state/grid_a/env"})


def test_verify_state_query_error_is_fail(cfg):
    from tests.conftest import FakeEmulator
    emu = FakeEmulator(fail=True)
    step = _step({"type": "state"}, args={"path": "/x", "expect": "1"})
    vr = _verifier(cfg, emulator=emu).verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "状态查询失败" in vr.detail


# ---------- vision ----------

def _shot():
    return Screenshot(png=b"\x89PNG-fake")


def test_verify_vision_pass_and_fail(cfg, fake_llm):
    fake_llm.vision_result = {"pass": True, "confidence": 0.95, "reason": "有绿标"}
    vr = _verifier(cfg, llm=fake_llm).verify(
        _step({"type": "vision", "expect": "绿色完成标记"}), _ctx(), None, _shot())
    assert vr.status == "pass" and vr.confidence == 0.95

    fake_llm.vision_result = {"pass": False, "confidence": 0.9, "reason": "未见绿标"}
    vr = _verifier(cfg, llm=fake_llm).verify(
        _step({"type": "vision", "expect": "绿色完成标记"}), _ctx(), None, _shot())
    assert vr.status == "fail"


def test_verify_vision_low_confidence_is_unknown(cfg, fake_llm):
    fake_llm.vision_result = {"pass": True, "confidence": 0.4, "reason": "模糊"}
    vr = _verifier(cfg, llm=fake_llm).verify(
        _step({"type": "vision", "expect": "绿标"}), _ctx(), None, _shot())
    assert vr.status == "unknown"          # 低置信不硬判（防幻觉）


def test_verify_vision_without_screenshot_or_llm(cfg, fake_llm):
    assert _verifier(cfg, llm=fake_llm).verify(
        _step({"type": "vision", "expect": "x"}), _ctx(), None, None).status == "unknown"
    assert _verifier(cfg).verify(
        _step({"type": "vision", "expect": "x"}), _ctx(), None, _shot()).status == "unknown"


def test_verify_vision_llm_exception_is_unknown(cfg):
    class _Boom:
        def verify_vision(self, b64, expect):
            raise RuntimeError("vlm down")

    vr = _verifier(cfg, llm=_Boom()).verify(
        _step({"type": "vision", "expect": "x"}), _ctx(), None, _shot())
    assert vr.status == "unknown" and "vlm down" in vr.detail


# ---------- screenshot_diff ----------

def test_verify_diff_detects_change(cfg):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    a = np.zeros((40, 40), dtype=np.uint8)
    b = a.copy()
    b[5:35, 5:35] = 255
    ok_a, buf_a = cv2.imencode(".png", a)
    ok_b, buf_b = cv2.imencode(".png", b)
    before = Screenshot(png=buf_a.tobytes())
    after = Screenshot(png=buf_b.tobytes())

    vr = _verifier(cfg).verify(_step({"type": "screenshot_diff"}), _ctx(),
                               before, after)
    assert vr.status == "pass"
    # 无变化 -> fail（只判有没有变）
    vr = _verifier(cfg).verify(_step({"type": "screenshot_diff"}), _ctx(),
                               before, Screenshot(png=buf_a.tobytes()))
    assert vr.status == "fail"


def test_verify_diff_without_before_requires_review(cfg):
    vr = _verifier(cfg).verify(_step({"type": "screenshot_diff"}), _ctx(),
                               None, _shot())
    assert vr.status == "unknown" and "无法验证" in vr.detail


def test_verify_diff_size_change_passes(cfg):
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    import cv2
    a = np.zeros((10, 10), dtype=np.uint8)
    b = np.zeros((20, 20), dtype=np.uint8)
    _, ba = cv2.imencode(".png", a)
    _, bb = cv2.imencode(".png", b)
    vr = _verifier(cfg).verify(_step({"type": "screenshot_diff"}), _ctx(),
                               Screenshot(png=ba.tobytes()), Screenshot(png=bb.tobytes()))
    assert vr.status == "pass" and "尺寸" in vr.detail


def test_verify_unknown_type_reports_fail(cfg):
    """VerifySpec 的 Literal 挡住未知类型；绕过校验时验证器必须给出 fail 而非静默通过。"""
    step = _step({"type": "none"})
    step.verify.type = "weird"          # 直接改属性（未开启 validate_assignment）
    vr = _verifier(cfg).verify(step, _ctx(), None, None)
    assert vr.status == "fail" and "未知验证类型" in vr.detail


def test_text_poll_uses_remaining_budget_and_rejects_late_success(monkeypatch):
    clock = [0.0]
    budgets = []
    class Driver:
        def locate(self, loc, ctx, timeout_ms):
            budgets.append(timeout_ms)
            clock[0] += 0.02 if len(budgets) == 1 else timeout_ms / 1000
            return None
        def read_text(self, _):
            return "waiting"
    monkeypatch.setattr("testpilot.engine.verifier.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("testpilot.engine.verifier.time.sleep",
                        lambda t: clock.__setitem__(0, clock[0] + t))
    step = _step({"type": "text", "expect": "done", "timeout_ms": 100},
                 target={"key": "x", "primary": {"type": "uia", "name": "result"}})
    verifier = Verifier({"uia": Driver()}, None, None, {})
    assert verifier.verify(step, _ctx(), None, None).status == "fail"
    assert len(budgets) == 2 and budgets[1] <= 30
    assert clock[0] <= 0.101
    def late(_remaining):
        clock[0] += 0.2
        from testpilot.core.contracts import VerifyResult
        return VerifyResult(status="pass")
    assert verifier._poll(late, 100).status == "fail"


def test_state_poll_passes_budget_and_abort_signal():
    from unittest.mock import Mock
    emu = Mock(spec=["query_state_bounded"])
    emu.query_state_bounded.return_value = "done"
    ctx = _ctx()
    step = _step({"type": "state", "timeout_ms": 100},
                 args={"path": "/status", "expect": "done"})
    assert Verifier({}, emu, None, {}).verify(step, ctx, None, None).status == "pass"
    call = emu.query_state_bounded.call_args
    assert 0 < call.kwargs["timeout_s"] <= 0.1
    assert call.kwargs["abort"] is ctx.abort

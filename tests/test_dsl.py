"""DSL 模型单测（设计 6 章）。"""
import pytest
from pydantic import ValidationError

from testpilot.core.dsl import (DriverMode, Locator, LocatorType, OnFailure, Step,
                               TargetDesc, TestCase, VerifySpec, execution_errors,
                               validate_execution)


def test_step_defaults_and_source_text():
    step = Step(id="s1", action="ui.click", source_text="点击开始检测按钮")
    assert step.source_text == "点击开始检测按钮"
    assert step.timeout_ms == 10000
    assert step.driver is None
    assert step.verify.type == "none"
    assert step.on_failure is OnFailure.ABORT
    assert step.uncertain is False


def test_case_roundtrip_json_keeps_uncertain_and_preconditions():
    case = TestCase(
        case_id="TC-1", zentao_case_id=1, version=3,
        preconditions=["模拟器场景: normal_sample_a"], notes="第 3 步不确定",
        steps=[
            Step(id="s1", action="app.launch",
                 args={"exe": "target.exe", "title_re": ".*主窗口.*"}),
            Step(id="s2", action="ui.click",
                 target=TargetDesc(key="btn.start", primary=Locator(type="uia", name="开始"),
                                   fallback=Locator(type="visual", desc="开始按钮"),
                                   vision_only_hint=True),
                 verify=VerifySpec(type="text", expect="S001", op="contains"),
                 driver=DriverMode.UIA_ONLY, uncertain=True),
        ])
    data = case.model_dump(mode="json")
    again = TestCase.model_validate(data)
    assert again.steps[1].target.key == "btn.start"
    assert again.steps[1].target.primary.name == "开始"
    assert again.steps[1].target.primary.type is LocatorType.UIA
    assert again.steps[1].target.vision_only_hint is True
    assert again.steps[1].uncertain is True
    assert again.steps[1].driver is DriverMode.UIA_ONLY
    assert again.preconditions == ["模拟器场景: normal_sample_a"]
    assert again.notes == "第 3 步不确定"


def test_locator_requires_type():
    with pytest.raises(ValidationError):
        Locator(name="没有 type")


def test_verify_type_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        VerifySpec(type="magic")


def test_driver_mode_values_match_design():
    assert {m.value for m in DriverMode} == {
        "auto", "uia_first", "vision_first", "uia_only", "vision_only"}


def test_on_failure_values():
    assert {m.value for m in OnFailure} == {"abort", "continue", "retry"}


def test_scroll_and_drag_semantic_validation():
    target = TargetDesc(
        key="panel.list",
        primary=Locator(type="uia", automation_id="list"),
    )
    assert execution_errors(Step(
        id="s1", action="ui.scroll", target=target,
        args={"direction": "down", "amount": 3},
    )) == []
    assert execution_errors(Step(
        id="s2", action="ui.drag", target=target,
        args={"dx": 80, "dy": 0, "duration_ms": 250},
    )) == []

    bad_scroll = execution_errors(Step(
        id="bad1", action="ui.scroll", target=target,
        args={"direction": "diagonal", "amount": 0},
    ))
    assert any("direction" in item for item in bad_scroll)
    assert any("amount" in item for item in bad_scroll)

    bad_drag = execution_errors(Step(
        id="bad2", action="ui.drag", target=target,
        args={"dx": 0, "dy": 0, "duration_ms": -1},
    ))
    assert any("dx/dy" in item for item in bad_drag)
    assert any("duration_ms" in item for item in bad_drag)



def test_wait_and_sleep_semantic_validation():
    target = TargetDesc(
        key="dialog.progress",
        primary=Locator(type="uia", automation_id="progress"),
    )
    assert execution_errors(Step(
        id="wait-ok", action="ui.wait", target=target,
        args={"state": "gone"},
    )) == []

    bad_wait = execution_errors(Step(
        id="wait-bad", action="ui.wait", target=target,
        args={"state": "disappear"},
    ))
    assert any("state" in item for item in bad_wait)

    assert execution_errors(Step(
        id="sleep-ok", action="sleep", args={"ms": "250"},
    )) == []
    bad_sleep = execution_errors(Step(
        id="sleep-bad", action="sleep", args={"ms": "later"},
    ))
    assert any("整数毫秒" in item for item in bad_sleep)

def test_assert_ui_requires_a_locator():
    step = Step(
        id="assert-no-target",
        action="assert.ui",
        verify=VerifySpec(type="text", expect="完成", op="contains"),
    )
    assert any("缺少 target" in item for item in execution_errors(step))

    via_verify = Step(
        id="assert-verify-target",
        action="assert.ui",
        verify=VerifySpec(
            type="text", expect="完成", op="contains",
            target=Locator(type="uia", automation_id="status"),
        ),
    )
    assert execution_errors(via_verify) == []


def test_app_action_semantic_validation():
    missing = TestCase(
        case_id="TC-APP",
        steps=[Step(id="s1", action="app.launch", args={})],
    )
    with pytest.raises(ValueError, match="args.exe 或 args.title_re"):
        validate_execution(missing)

    string_args = TestCase(
        case_id="TC-APP",
        steps=[Step(
            id="s1", action="app.launch",
            args={"exe": "app.exe",
                  "args": '--test-mode --config "D:\\a b.yaml"'},
        )],
    )
    validate_execution(string_args)

    list_args = TestCase(
        case_id="TC-APP",
        steps=[Step(
            id="s1", action="app.launch",
            args={"exe": "app.exe", "args": ["--test-mode", "D:\\a b.yaml"]},
        )],
    )
    validate_execution(list_args)

    bad_args = TestCase(
        case_id="TC-APP",
        steps=[Step(
            id="s1", action="app.launch",
            args={"exe": "app.exe", "args": {"bad": True}},
        )],
    )
    with pytest.raises(ValueError, match="命令行字符串或字符串数组"):
        validate_execution(bad_args)

    bad_close = TestCase(
        case_id="TC-APP",
        steps=[Step(id="s1", action="app.close", args={"mode": "terminate"})],
    )
    with pytest.raises(ValueError, match="graceful 或 force"):
        validate_execution(bad_close)

    # 历史 DSL 的 kill 保持兼容；新生成统一使用 force。
    legacy = TestCase(
        case_id="TC-APP",
        steps=[Step(id="s1", action="app.close", args={"mode": "kill"})],
    )
    validate_execution(legacy)


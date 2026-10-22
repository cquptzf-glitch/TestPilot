"""Qt interaction extensions must survive import and shared semantic validation."""
import pytest

from testpilot.adapters.llm_gateway import LlmGateway
from testpilot.core.dsl import Step, TestCase as DslCase, validate_execution


def test_qt_table_coordinates_survive_model_draft_normalization():
    gateway = LlmGateway({})
    draft = gateway._normalize_draft({
        "case_id": "QT-1", "steps": [{
            "id": "s1", "action": "ui.click",
            "target": {"key": "cell", "primary": {
                "type": "uia", "name": "Results table", "row": 79, "column": 2}}
        }]}, {})
    case = DslCase.model_validate(draft)
    validate_execution(case)
    assert case.steps[0].target.primary.row == 79
    assert case.steps[0].target.primary.column == 2


@pytest.mark.parametrize("action,value", [("ui.toggle", "true"), ("ui.toggle", "false"),
                                          ("ui.keypress", "{F2}{ENTER}")])
def test_qt_actions_are_executable(action, value):
    case = DslCase(case_id="QT-1", steps=[Step(
        id="s1", action=action, value=value,
        target={"key": "x", "primary": {"type": "uia", "name": "x"}})])
    validate_execution(case)


@pytest.mark.parametrize("patch", [
    {"action": "ui.toggle", "value": "maybe"}, {"action": "ui.keypress", "value": ""},
    {"target": {"key": "cell", "primary": {"type": "uia", "name": "table", "row": 1}}},
])
def test_incomplete_qt_operations_are_rejected(patch):
    step = {"id": "s", "action": "ui.click",
            "target": {"key": "x", "primary": {"type": "uia", "name": "x"}}, **patch}
    with pytest.raises(ValueError):
        validate_execution(DslCase(case_id="QT-1", steps=[Step.model_validate(step)]))

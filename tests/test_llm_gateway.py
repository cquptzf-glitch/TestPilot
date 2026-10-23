"""LLM 网关单测 — 结构校验、JSON 提取、计量、错误语义（设计 8.13 / 11 章）。"""
import json

import pytest

from testpilot.adapters import prompts
from testpilot.adapters.llm_gateway import (KNOWN_ACTIONS, LlmGateway,
                                           _parse_json_object, set_default_llm)
from testpilot.core.errors import LlmError


def _gw(payloads, usage=None):
    """构造带假传输的网关；``payloads`` 依次作为每次调用的返回内容。"""
    seq = list(payloads)

    def transport(prompt, image_b64):
        content = seq.pop(0) if len(seq) > 1 else seq[0]
        return content, usage or {"total_tokens": 12, "prompt_tokens": 8,
                                 "completion_tokens": 4}

    return LlmGateway({"model": "test-llm"}, transport=transport)


# ---------- JSON 提取 ----------

def test_parse_json_object_plain():
    assert _parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_with_fence_and_prose():
    text = "好的，结果如下：\n```json\n{\"bbox\": [1, 2, 3, 4]}\n```\n以上。"
    assert _parse_json_object(text) == {"bbox": [1, 2, 3, 4]}


def test_parse_json_object_without_fence_language():
    text = "```\n{\"ok\": true}\n```"
    assert _parse_json_object(text) == {"ok": True}


@pytest.mark.parametrize("bad", ["", "   ", "没有 JSON", "{不是对象", "[1,2]"])
def test_parse_json_object_raises_on_bad_input(bad):
    with pytest.raises(LlmError):
        _parse_json_object(bad)


# ---------- 草稿归一化 ----------

def test_parse_case_normalizes_and_marks_uncertain():
    draft = {
        "case_id": "TC-9", "title": "T", "preconditions": ["模拟器场景: a"],
        "notes": "", "steps": [
            {"id": "s1", "action": "ui.click", "source_text": "1. 点A",
             "target": {"key": "btn.a", "primary": {"type": "uia", "name": "A"}}},
            {"id": "s2", "action": "ui.magic", "target": None},     # 表外动作
            {"id": "s3", "action": "ui.input", "verify": {"type": "text",
                                                          "op": "bad-op"}},
        ]}
    gw = _gw([json.dumps(draft)])
    out = gw.parse_case({"case_id": "TC-9", "steps_text": "1. 点A"})

    assert out["case_id"] == "TC-9"
    assert out["prompt_version"] == prompts.PROMPT_VERSIONS["parse_case"]
    assert out["uncertain_count"] == 2                       # 表外动作 + 非法 op
    assert out["steps"][0]["action"] == "ui.click"
    assert out["steps"][0]["uncertain"] is False
    assert out["steps"][1]["uncertain"] is True              # 表外动作
    assert out["steps"][2]["verify"]["op"] == "contains"     # 非法 op 被纠正
    assert out["steps"][2]["uncertain"] is True              # 纠正即待人审
    assert "校验提示" in out["notes"]
    assert out["preconditions"] == ["模拟器场景: a"]


def test_parse_case_degrades_invalid_step_instead_of_failing():
    draft = {"case_id": "TC-1", "steps": [
        {"id": "s1", "action": "ui.click", "verify": {"type": "nope"}},
        "not-a-dict",
    ]}
    gw = _gw([json.dumps(draft)])
    out = gw.parse_case({"case_id": "TC-1"})
    assert len(out["steps"]) == 2
    assert out["steps"][1]["action"] == "review.required"
    assert out["steps"][1]["uncertain"] is True
    assert out["steps"][0]["verify"]["type"] == "none"
    assert out["steps"][0]["uncertain"] is True
    assert "第 2 步不是对象" in out["notes"]


def test_parse_case_fills_defaults_from_raw():
    gw = _gw([json.dumps({"steps": []})])
    out = gw.parse_case({"case_id": "TC-7", "title": "原题"})
    assert out["case_id"] == "TC-7"
    assert out["title"] == "原题"
    assert out["steps"] == []


def test_parse_case_empty_steps_flag_not_raised():
    gw = _gw([json.dumps({"steps": []})])
    assert gw.parse_case({"case_id": "TC-1"})["uncertain_count"] == 0


def test_known_actions_all_accepted():
    steps = []
    for i, action in enumerate(sorted(KNOWN_ACTIONS), 1):
        verify = {"assert.ui": "text", "assert.state": "state",
                  "assert.vision": "vision"}.get(action, "none")
        steps.append({"id": f"s{i}", "action": action,
                      "value": "true" if action == "ui.toggle" else "{ENTER}",
                      "target": {"key": "button", "primary": {"type": "uia", "name": "OK"}},
                      "args": {"cmd": "reset", "scene_id": "a", "path": "/x",
                               "expect": "ready", "ms": 0},
                      "verify": {"type": verify, "expect": "ready"}})
    gw = _gw([json.dumps({"steps": steps})])
    out = gw.parse_case({"case_id": "TC-1"})
    assert out["uncertain_count"] == 0


# ---------- 其它三类能力 ----------

def test_ground_normalizes_bbox_and_confidence():
    gw = _gw([json.dumps({"bbox": ["1", "2", "3", "4"], "confidence": "0.8"})])
    out = gw.ground("b64", "按钮")
    assert out["bbox"] == [1, 2, 3, 4]
    assert out["confidence"] == 0.8


def test_ground_null_bbox():
    gw = _gw([json.dumps({"bbox": None, "confidence": 0})])
    assert gw.ground("b64", "按钮")["bbox"] is None


def test_ground_rejects_wrong_length_bbox():
    gw = _gw([json.dumps({"bbox": [1, 2], "confidence": 0.9})])
    assert gw.ground("b64", "按钮")["bbox"] is None


def test_verify_vision_shape():
    gw = _gw([json.dumps({"pass": True, "confidence": 0.91, "reason": "有绿色标记"})])
    out = gw.verify_vision("b64", "出现绿色完成标记")
    assert out == {"pass": True, "confidence": 0.91, "reason": "有绿色标记"}


def test_judge_failure_shape_and_defaults():
    gw = _gw([json.dumps({"category": "timing"})])
    out = gw.judge_failure({"signature": "x"})
    assert out["category"] == "timing"
    assert out["confidence"] == 0.0
    assert out["evidence"] == ""


# ---------- 计量与错误 ----------

def test_usage_accumulates_and_delta():
    gw = _gw(['{"a": 1}'], usage={"total_tokens": 30})
    before = gw.usage_snapshot()
    gw.parse_case({"case_id": "TC-1"})
    after = gw.usage_snapshot()
    assert after["calls"] - before["calls"] == 1
    assert gw.usage_delta(before, after)["tokens"] == 30
    assert gw.usage_delta({}, {}) == {}


def test_non_json_response_raises_llmerror_and_counts_error():
    gw = _gw(["我不是 JSON"])
    with pytest.raises(LlmError):
        gw.parse_case({"case_id": "TC-1"})
    assert gw.stats["errors"] == 1


def test_transport_exception_wrapped_as_llmerror():
    def bad_transport(prompt, image_b64):
        raise RuntimeError("boom")

    gw = LlmGateway({}, transport=bad_transport)
    with pytest.raises(LlmError):
        gw.ground("b64", "x")
    assert gw.stats["errors"] == 1


def test_http_completion_requires_api_base():
    gw = LlmGateway({"api_base": ""})
    with pytest.raises(LlmError):
        gw._http_completion("p", None)


def test_emulator_capabilities_injected_into_prompt():
    captured = {}

    def transport(prompt, image_b64):
        captured["prompt"] = prompt
        return '{"steps": []}', {"total_tokens": 1}

    gw = LlmGateway({}, transport=transport)
    gw.emulator_capabilities = {"commands": ["load_scene"], "scenes": ["s1"]}
    gw.parse_case({"case_id": "TC-1"})
    assert "load_scene" in captured["prompt"]
    assert "s1" in captured["prompt"]


def test_set_default_llm_roundtrip():
    from testpilot.adapters.llm_gateway import get_default_llm

    fake = LlmGateway({}, transport=lambda p, i: ("{}", {}))
    set_default_llm(fake)
    assert get_default_llm() is fake
    set_default_llm(None)


# ---------- prompt 模板 ----------

def test_prompt_templates_have_stable_versions():
    assert set(prompts.PROMPT_VERSIONS) == {
        "parse_case", "ground", "verify_vision", "judge"}
    assert prompts.PROMPT_VERSIONS["parse_case"] == "parse_case_v4"


def test_parse_prompt_contains_action_table_and_raw_text():
    text = prompts.parse_case_prompt({"case_id": "TC-1", "steps_text": "1. 点开始"},
                                     None)
    assert "ui.click" in text and "device.call" in text
    assert "1. 点开始" in text
    assert "禁止生成 device.* 步骤" in text        # 无能力清单时的白名单约束


def test_parse_prompt_with_capabilities_whitelists():
    text = prompts.parse_case_prompt({"case_id": "TC-1"},
                                     {"commands": ["set_pressure"],
                                      "scenes": ["scene_a"], "terms": {"压力": "pressure"}})
    assert "set_pressure" in text and "scene_a" in text and "压力" in text


def test_other_prompts_embed_payload():
    assert "开始检测按钮" in prompts.ground_prompt("开始检测按钮")
    assert "绿色" in prompts.verify_vision_prompt("出现绿色标记")
    assert "uia_change" in prompts.judge_prompt({"signature": "s"})


# ---------- 11.2 设备能力清单：白名单校验 + 按用例裁剪 ----------

_CAPS = {
    "commands": [
        {"cmd": "set_sample", "desc": "装载/更换样本",
         "args_schema": {"type": "object", "required": ["type"],
                         "properties": {
                             "type": {"enum": ["A", "B", "C"]},
                             "barcode": {"type": "string"},
                             "errors": {"type": "array",
                                        "items": {"enum": ["E_REAGENT"]}}}}},
        {"cmd": "inject_error", "desc": "注入设备错误"},
    ],
    "scenes": [{"id": "normal_sample_a", "desc": "常规样本A · 无异常"},
               {"id": "probe_block", "desc": "探针堵塞 · 报警 E_PROBE"}],
    "terms": {"缺试剂": "错误码 E_REAGENT"},
}


def test_device_call_cmd_not_in_manifest_marked_uncertain():
    gw = _gw([json.dumps({"steps": [
        {"id": "s1", "action": "device.call", "args": {"cmd": "no_such_cmd"}}]})])
    gw.emulator_capabilities = _CAPS
    out = gw.parse_case({"case_id": "TC-1", "steps_text": "1. 装样本"})
    assert out["steps"][0]["uncertain"] is True
    assert "不在指令表" in out["notes"]


def test_device_call_args_schema_violation_marked_uncertain():
    gw = _gw([json.dumps({"steps": [
        {"id": "s1", "action": "device.call",
         "args": {"cmd": "set_sample", "args": {"type": "D"}}}]})])
    gw.emulator_capabilities = _CAPS
    out = gw.parse_case({"case_id": "TC-1", "steps_text": "1. 装样本"})
    assert out["steps"][0]["uncertain"] is True
    assert "不在枚举" in out["notes"]


def test_device_call_valid_args_pass_without_uncertain():
    gw = _gw([json.dumps({"steps": [
        {"id": "s1", "action": "device.call", "source_text": "1. 装样本",
         "args": {"cmd": "set_sample",
                  "args": {"type": "A", "barcode": "S1", "errors": ["E_REAGENT"]}}}]})])
    gw.emulator_capabilities = _CAPS
    out = gw.parse_case({"case_id": "TC-1", "steps_text": "1. 装样本"})
    assert out["steps"][0]["uncertain"] is False
    assert out["uncertain_count"] == 0


def test_device_scene_not_in_manifest_marked_uncertain():
    gw = _gw([json.dumps({"steps": [
        {"id": "s1", "action": "device.scene", "args": {"scene_id": "ghost"}}]})])
    gw.emulator_capabilities = _CAPS
    out = gw.parse_case({"case_id": "TC-1", "steps_text": "1. 场景"})
    assert out["steps"][0]["uncertain"] is True
    assert "不在场景清单" in out["notes"]


def test_device_step_without_manifest_not_marked():
    """模拟器未起时不误伤（prompt 层已禁止 device.*，校验侧保持宽容）。"""
    gw = _gw([json.dumps({"steps": [
        {"id": "s1", "action": "device.call", "args": {"cmd": "whatever"}}]})])
    out = gw.parse_case({"case_id": "TC-1"})
    assert out["steps"][0]["uncertain"] is False


def test_prompt_capabilities_filtered_by_case_text():
    caps = {"commands": [{"cmd": f"cmd_{i}", "desc": f"能力{i}"} for i in range(20)]
                      + [{"cmd": "inject_error", "desc": "注入设备错误"}],
            "scenes": [{"id": "probe_block", "desc": "探针堵塞"}],
            "terms": {}}
    prompt = prompts.parse_case_prompt(
        {"case_id": "TC-1", "steps_text": "1. 探针堵塞时注入设备错误"}, caps)
    assert "inject_error" in prompt and "probe_block" in prompt
    assert "已按用例相关性裁剪" in prompt
    # 低相关条目被截断（20+1 条命令，注入上限 15）
    assert "cmd_19" not in prompt


def test_prompt_capabilities_small_manifest_kept_whole():
    prompt = prompts.parse_case_prompt({"case_id": "TC-1"}, _CAPS)
    assert "set_sample" in prompt and "normal_sample_a" in prompt and "缺试剂" in prompt

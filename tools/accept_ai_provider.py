"""Live acceptance against the configured AI provider using synthetic evidence only."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
from testpilot.adapters.llm_gateway import LlmGateway
from testpilot.config.settings import load_settings
from testpilot.core.contracts import ExecContext, StepResult
from testpilot.core.dsl import TestCase, validate_execution
from testpilot.engine.judge import Judge
from testpilot.knowledge.signature import FailureSignature


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    cfg = load_settings(env={"TESTPILOT_NO_OVERRIDE": "1"})
    cfg["app"]["screenshot_root"] = str(root)
    gateway = LlmGateway({**cfg["llm"], "vision": cfg["vision"]["vlm"]})
    checks, outputs = [], {}
    def check(name, ok, **detail):
        checks.append({"name": name, "passed": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + name, flush=True)

    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else ImageFont.load_default()
    image = Image.new("RGB", (800, 480), "#f0f5f7")
    draw = ImageDraw.Draw(image)
    draw.text((40, 25), "SYNTHETIC Qt TEST LAB", fill="#152a38", font=font)
    draw.text((40, 100), "Sample: SAMPLE-A17", fill="#152a38", font=font)
    draw.text((40, 170), "Status: Completed", fill="#152a38", font=font)
    draw.text((40, 240), "Result: 99.9", fill="#b42318", font=font)
    draw.rectangle((500, 340, 720, 420), fill="#13796b")
    draw.text((525, 360), "Export", fill="white", font=font)
    image.save(root / "after.png")
    import base64
    b64 = base64.b64encode((root / "after.png").read_bytes()).decode()
    raw = {"case_id": "AI-LIVE-001", "title": "样本编号与结果校验",
           "preconditions": ["被测程序已启动，当前为主界面"],
           "steps": [
               {"desc": "在样本编号输入框输入 SAMPLE-A17", "expect": "样本编号显示 SAMPLE-A17"},
               {"desc": "点击开始测量按钮并等待完成", "expect": "状态显示 Completed"},
               {"desc": "检查结果数值", "expect": "结果显示 42.5"},
           ]}
    try:
        draft = gateway.parse_case(raw)
        outputs["draft"] = draft
        validate_execution(TestCase.model_validate(draft))
        sources = " ".join(str(s.get("source_text") or "") for s in draft["steps"])
        check("Live Chinese DSL parse and original step coverage",
              all(s["desc"] in sources for s in raw["steps"]) and len(draft["steps"]) >= 3)
        expectations = json.dumps([s.get("verify") for s in draft["steps"]], ensure_ascii=False)
        check("Parsed DSL preserves expected values", all(x in expectations for x in ("SAMPLE-A17", "Completed", "42.5")))
    except Exception as exc:
        outputs["parse_error"] = str(exc)
        check("Live Chinese DSL parse", False, error=str(exc))
    for expected, desired, name in [
            ("The displayed result is 99.9 and the status is Completed.", True, "True image assertion"),
            ("The displayed result is 42.5.", False, "False image assertion")]:
        try:
            result = gateway.verify_vision(b64, expected)
            outputs[name] = result
            check(name, result["pass"] is desired and result["confidence"] >= 0.7)
        except Exception as exc:
            check(name, False, error=str(exc))
    try:
        grounded = gateway.ground(b64, "The green Export button", None)
        outputs["grounding"] = grounded
        box = grounded.get("bbox") or []
        check("Image grounding locates actual Export button", len(box) == 4
              and 500 <= box[0] + box[2] / 2 <= 720 and 340 <= box[1] + box[3] / 2 <= 420
              and grounded["confidence"] >= 0.7)
    except Exception as exc:
        check("Image grounding", False, error=str(exc))
    try:
        res = StepResult(step_no=3, step_id="result", action="assert.ui", status="fail",
                         source_text="检查结果显示 42.5", screenshot_after="after.png",
                         error={"type": "verify_failed", "message": "Expected 42.5; actual 99.9"})
        report = Judge(gateway, None, cfg)._llm_pass(
            res, FailureSignature("verify_failed", "assert.ui", "result", "main"),
            ExecContext(run_id=1, case_exec_id=1, case=TestCase(case_id=raw["case_id"])))
        outputs["judge"] = report.to_dict()
        check("Failure analysis consumes real image and remains reviewable",
              report.source == "llm" and report.needs_review and report.category != "unknown"
              and "99.9" in (report.evidence + report.suggestion))
    except Exception as exc:
        check("Failure analysis", False, error=str(exc))
    result = {"ok": all(c["passed"] for c in checks),
              "provider": cfg["llm"]["api_base"], "model": cfg["llm"]["model"],
              "api_key": "empty", "at": datetime.now(timezone.utc).isoformat(),
              "checks": checks, "outputs": outputs, "usage": gateway.usage_snapshot()}
    (root / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

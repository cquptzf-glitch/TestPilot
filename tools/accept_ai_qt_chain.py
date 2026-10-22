"""Live AI -> reviewed DSL -> actual Qt -> strict local ZenTao readback acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from testpilot.config.settings import load_settings
from testpilot.simulation.runner import EngineRpc, configuration, local_network
from testpilot.simulation.services import LabServices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--app-exe", required=True)
    parser.add_argument("--run-reviewed", action="store_true")
    args = parser.parse_args()
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=args.run_reviewed)
    result = {"checks": []}
    def check(name, ok, **detail):
        result["checks"].append({"name": name, "passed": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        if not ok:
            raise RuntimeError(name + ": " + str(detail))
    with local_network(), LabServices(19480, 19481) as lab:
        lab.case_overrides[91901] = {
            "id": 91901, "product": 1, "title": "AI 生成并驱动真实 Qt",
            "precondition": "scene:normal\n主窗口已启动，使用 app.launch 只按 title_re 附加，不传 exe。"
                "窗口标题为 TestPilot Mock Instrument - 19480。"
                "输入框 Sample ID 是 Edit，下拉框 Measurement mode 是 ComboBox，"
                "Start measurement 和 Export report 是 Button，"
                "状态文本 Completed 以及结果文本 42.5 mg/L 是 Text。",
            "lastEditedDate": "2026-10-16T01:00:00",
            "steps": [
                {"desc": "附加标题为 TestPilot Mock Instrument - 19480 的已启动窗口",
                 "expect": ""},
                {"desc": "在 Sample ID 输入框覆盖输入 AI-REAL-17",
                 "expect": "Sample ID 的值等于 AI-REAL-17"},
                {"desc": "在 Measurement mode 下拉框选择 Precision",
                 "expect": ""},
                {"desc": "点击 Start measurement 按钮，等待界面状态 Completed",
                 "expect": "状态文本等于 Completed"},
                {"desc": "检查界面的结果文本 42.5 mg/L",
                 "expect": "界面显示 42.5 mg/L"},
                {"desc": "点击 Export report 按钮",
                 "expect": ""},
            ]}
        cfg = configuration(root, lab)
        provider = load_settings(env={"TESTPILOT_NO_OVERRIDE": "1"})
        cfg["llm"] = provider["llm"]
        cfg["vision"]["vlm"] = provider["vision"]["vlm"]
        cfg["app"]["test_app_exe"] = str(Path(args.app_exe).resolve())
        process = None
        try:
            with EngineRpc(cfg, gui=True) as rpc:
                if not args.run_reviewed:
                    pulled = rpc.call("case.sync", {"productId": 1})
                    check("Zentao summary sync hydrates real case details", pulled["new"] == 8)
                    job = rpc.call("job.start", {"method": "case.parse",
                                                "params": {"caseId": "TC-91901"}})
                    health_times = []
                    deadline = time.monotonic() + 100
                    while job["status"] in ("queued", "running"):
                        now = time.monotonic()
                        rpc.call("health")
                        health_times.append(time.monotonic() - now)
                        time.sleep(0.3)
                        job = rpc.call("job.status", {"jobId": job["id"]})
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Model job exceeded acceptance deadline")
                    check("Background real-model parse completes", job["status"] == "completed", job=job)
                    check("Health stays responsive during model IO", max(health_times) < 2,
                          max_health_seconds=max(health_times))
                    draft = rpc.call("case.export", {"caseId": "TC-91901"})["content"]
                    (root / "draft.json").write_text(draft, encoding="utf-8")
                    result.update(stage="awaiting_review", job=job)
                else:
                    draft_bytes = (root / "draft.json").read_bytes()
                    approved = (root / "approved-draft.sha256").read_text().strip()
                    check("Reviewed draft matches approved SHA256",
                          hashlib.sha256(draft_bytes).hexdigest() == approved)
                    rpc.call("case.import", {"caseId": "TC-91901", "text": draft_bytes.decode("utf-8")})
                    rpc.call("dsl.accept", {"caseId": "TC-91901"})
                    process = subprocess.Popen([str(Path(args.app_exe).resolve()),
                                                lab.url, str(root / "exports")])
                    task = rpc.call("task.create", {"name": "Live AI Qt chain",
                                                    "caseIds": ["TC-91901"], "remote": True})
                    stats = rpc.call("task.run", {"taskId": task["task_id"]})
                    check("Real Qt UIA execution passes", stats.get("pass") == 1, stats=stats)
                    remote = lab.state.snapshot()["tasks"][task["zentao_task_id"]]
                    check("Remote status and summary persisted",
                          stats["writeback"] == "synced" and remote["status"] == "done"
                          and "run#" in remote["desc"])
                    check("Remote actual case association matches",
                          remote["testcases"] == [{"id": 91901}])
                    exports = [json.loads(p.read_text(encoding="utf-8"))
                               for p in (root / "exports").glob("measurement-*.json")]
                    check("Qt independent export contents match",
                          any(e.get("sample_id") == "AI-REAL-17"
                              and e.get("mode") == "Precision" and e.get("value") == "42.5"
                              for e in exports))
                    result.update(stage="completed", task=task, stats=stats, remote=remote)
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    result["ok"] = all(c["passed"] for c in result["checks"])
    name = "chain-acceptance.json" if args.run_reviewed else "parse-acceptance.json"
    (root / name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

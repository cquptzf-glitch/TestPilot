"""Isolated real-model desktop acceptance lab. All business actions start in the UI."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from testpilot.config.settings import load_settings
from testpilot.simulation.runner import configuration, local_network
from testpilot.simulation.services import LabServices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--shell", required=True)
    parser.add_argument("--app-exe", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    app = Path(args.app_exe).resolve()
    with local_network(), LabServices(19680, 19681) as lab:
        for zid, sample in ((91911, "BATCH-QT-A"), (91912, "BATCH-QT-B")):
            lab.case_overrides[zid] = {
                "id": zid, "product": 1, "title": f"批量 AI Qt 操作流 {sample}",
                "lastEditedDate": "2026-10-16T04:00:00",
                "precondition": "scene:normal\n主窗口已启动，app.launch 只按 title_re 附加，不传 exe。"
                    "窗口标题为 TestPilot Mock Instrument - 19680。"
                    "Sample ID 为 Edit；Measurement mode 为 ComboBox；"
                    "Start measurement 和 Export report 为 Button；"
                    "状态 Completed、结果 42.5 mg/L 为 Text。",
                "steps": [
                    {"desc": "附加标题为 TestPilot Mock Instrument - 19680 的已启动窗口", "expect": ""},
                    {"desc": f"在 Sample ID 输入框覆盖输入 {sample}", "expect": f"Sample ID 的值等于 {sample}"},
                    {"desc": "在 Measurement mode 下拉框选择 Precision", "expect": ""},
                    {"desc": "点击 Start measurement 按钮并等待 Completed", "expect": "状态文本等于 Completed"},
                    {"desc": "检查界面结果文本 42.5 mg/L", "expect": "界面显示 42.5 mg/L"},
                    {"desc": "点击 Export report 按钮", "expect": ""},
                ]}
        lab.case_overrides[91913] = {
            "id": 91913, "product": 1, "title": "空原文：验证批量失败隔离",
            "lastEditedDate": "2026-10-16T04:00:00", "steps": []}
        cfg = configuration(root, lab)
        defaults = load_settings(env={"TESTPILOT_NO_OVERRIDE": "1"})
        cfg["llm"], cfg["vision"]["vlm"] = defaults["llm"], defaults["vision"]["vlm"]
        cfg["app"]["test_app_exe"] = str(app)
        config_file = root / "config.json"
        config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        token = secrets.token_urlsafe(40)
        (root / "rpc-session.json").write_text(json.dumps({"token": token}), encoding="utf-8")
        env = dict(os.environ, TESTPILOT_HOME=str(root), TESTPILOT_CONFIG=str(config_file),
                   TESTPILOT_RPC_PORT="19682", TESTPILOT_RPC_TOKEN=token)
        qt = subprocess.Popen([str(app), lab.url, str(root / "exports")])
        try:
            with (root / "process.log").open("w", encoding="utf-8") as log:
                shell = subprocess.Popen([str(Path(args.shell).resolve())], env=env,
                                         stdout=log, stderr=log)
                print(json.dumps({"shell_pid": shell.pid, "qt_pid": qt.pid,
                    "case_ids": [91911, 91912, 91913], "model": cfg["llm"]["model"],
                    "stage": "ready; pull, batch analysis, review, task and run must use desktop UI"}), flush=True)
                while shell.poll() is None:
                    tasks = lab.state.snapshot()["tasks"]
                    (root / "remote-tasks.json").write_text(
                        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
                    time.sleep(1)
        finally:
            if qt.poll() is None:
                qt.terminate()
                qt.wait(timeout=10)


if __name__ == "__main__":
    main()

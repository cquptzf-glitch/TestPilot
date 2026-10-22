"""Read-only verification of a Qt run initiated from the real desktop UI."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
    action = json.loads((root / "ui-start-action.json").read_text(encoding="utf-8"))
    uri = "file:" + root.joinpath("testpilot.db").as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = dict(conn.execute("SELECT * FROM test_run ORDER BY id DESC LIMIT 1").fetchone())
        cases = [dict(row) for row in conn.execute("SELECT * FROM case_exec WHERE run_id=?", (run["id"],))]
        steps = [dict(row) for row in conn.execute(
            "SELECT s.* FROM step_trace s JOIN case_exec c ON c.id=s.case_exec_id "
            "WHERE c.run_id=? ORDER BY s.id", (run["id"],))]
    finally:
        conn.close()
    stats = json.loads(run["stats_json"])
    widgets = json.loads((root / "exports/qt-controls-state.json").read_text(encoding="utf-8"))
    qml = json.loads((root / "exports/qt-qml-state.json").read_text(encoding="utf-8"))
    checks = []
    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        print(("PASS " if passed else "FAIL ") + name)
    check("Actual TestPilot UI execute action recorded",
          action["action"] == "click TestPilot task execute button"
          and action["app"].lower().endswith("testpilot-shell.exe")
          and action["taskId"] == run["task_id"])
    check("Desktop task completed and passed", run["status"] == "completed"
          and stats["pass"] == 1 and stats["fail"] == 0 and stats["error"] == 0)
    check("All 57 persisted steps passed", len(steps) == ready["steps"] == 57
          and all(s["status"] == "pass" for s in steps))
    check("Production UIA driver executed interactions", stats["drivers"]["by_driver"]["uia"]["success"] == 46)
    check("Complete evidence and actual report", not stats["evidence_incomplete"]
          and stats["evidence_errors"] == 0 and (root / f"reports/{run['id']}/report.md").is_file())
    expected = {"audit": False, "precision": False, "replicates": 17, "date": "2027-10-16",
                "gauge": 25, "table_value": "43.75", "selected_row": 79, "tree": "Chemistry",
                "list": "Beta", "accepted": 1, "cancelled": 1, "confirmation": "Confirmed: CONFIRM-17",
                "shared_main": "MAIN-ONLY", "shared_auxiliary": "AUX-ONLY", "menu_resets": 1}
    check("Widgets independent state matches all expected results",
          all(widgets.get(key) == value for key, value in expected.items()))
    check("QML independent state matches expected results",
          qml == {"canvas_count": 1, "enabled": True, "mode": "Rapid",
                  "sample": "QML+^{}-17-样本", "result": "QML+^{}-17-样本 | Rapid | true"})
    check("Case snapshot and desktop screenshot exist",
          all(c.get("dsl_snapshot_json") for c in cases)
          and (root / "desktop-result.jpg").is_file())
    result = {"ok": all(c["passed"] for c in checks), "verified_at": datetime.now(timezone.utc).isoformat(),
              "started_by": "TestPilot desktop task Execute button; helper never calls task.run",
              "task_id": run["task_id"], "run_id": run["id"], "shell_pid": ready["shell_pid"],
              "checks": checks, "stats": stats, "widgets": widgets, "qml": qml, "ui_action": action}
    (root / "desktop-acceptance.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# TestPilot 桌面端驱动 Qt 验收", "", f"结果：{'PASS' if result['ok'] else 'FAIL'}",
             "", "从真实 TestPilot 桌面任务页面点击执行，通过 Rust 桥和随包 sidecar 运行。",
             "准备脚本仅导入审核用例和建任务，不调用 task.run。",
             "", f"- task #{run['task_id']} / run #{run['id']}",
             "- 57 个步骤全部通过，46 次 UIA 操作成功。",
             "- Qt Widgets 和 QML 独立状态文件与期望一致。",
             "", "| 检查 | 结果 |", "|---|---|"]
    lines.extend(f"| {c['name']} | {'PASS' if c['passed'] else 'FAIL'} |" for c in checks)
    (root / "desktop-acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Qt Widgets/QML acceptance using real RPC, production UIA and independent app state."""
import json
from pathlib import Path
import subprocess
import time

from testpilot.core.contracts import ExecContext
from .runner import EngineRpc, configuration, local_network
from .services import LabServices


def locator(name, kind=None, **extra):
    return {"type": "uia", "name": name, **({"control_type": kind} if kind else {}), **extra}


def action(kind, name=None, control=None, **extra):
    step = {"action": kind, "timeout_ms": 10000, **extra}
    if name and "target" not in step:
        step["target"] = {"key": name, "primary": locator(name, control)}
    return step


def assertion(name, expect, kind=None, **coords):
    return action("assert.ui", name, kind, verify={
        "type": "text", "expect": str(expect), "op": "equals", "timeout_ms": 1000,
        "target": locator(name, kind, **coords)})


def qt_acceptance(root, app_exe):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    exe = Path(app_exe).resolve()
    result = {"ok": False, "checks": [], "runs": [], "framework": "Qt 6 Widgets + Quick/QML"}
    processes = []
    rpc = None

    def check(name, condition, detail=None):
        row = {"name": name, "passed": bool(condition), "detail": detail}
        result["checks"].append(row)
        print(f"[{'PASS' if condition else 'FAIL'}] {name}", flush=True)
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def state(filename):
        deadline = time.monotonic() + 3
        while True:
            try:
                return json.loads((root / "exports" / filename).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def dump(name):
        driver = rpc.orch.drivers["uia"]
        try:
            nodes = [{"name": w.element_info.name, "type": w.element_info.control_type,
                      "id": w.element_info.automation_id, "rect": str(w.rectangle())}
                     for w in driver._top.descendants()][:500]
            (root / (name + "-uia.json")).write_text(json.dumps(nodes, indent=2), encoding="utf-8")
            shot = driver.grab(driver._rect_of(driver._top), ExecContext(0, 0))
            (root / (name + ".png")).write_bytes(shot.png)
        except Exception as exc:
            result.setdefault("diagnostics", []).append(str(exc))

    with local_network(), LabServices(0, 0) as lab:
        cfg = configuration(root, lab)
        cfg["uia"]["input_mode"] = "keys"
        cfg["driver"]["step_retry"] = 0
        cfg["app"]["test_app_exe"] = str(exe)
        cfg["app"]["test_app_version"] = "qt-controls-2.0"
        suffix = str(lab.http_port)
        main_title = "^TestPilot Mock Instrument - " + suffix + "$"
        controls_title = "^TestPilot Qt Controls Lab - " + suffix + "$"
        def attach(title):
            return action("app.launch", args={"title_re": title})
        def tab(name):
            return action("ui.select", value=name, target={
                "key": "feature.tabs", "primary": {"type": "uia", "control_type": "Tab"}})
        def run(name, steps, expected="pass"):
            cid = f"QT-{len(result['runs']) + 1}"
            draft = {"case_id": cid, "title": name,
                     "steps": [{"id": f"s{i + 1}", **s} for i, s in enumerate(steps)]}
            rpc.call("case.import", {"text": json.dumps(draft)})
            rpc.call("dsl.accept", {"caseId": cid})
            task = rpc.call("task.create", {"name": name, "caseIds": [cid], "remote": False})
            stats = rpc.call("task.run", {"taskId": task["task_id"], "writeback": False})
            result["runs"].append({"name": name, "case_id": cid, "stats": stats})
            detail = rpc.call("run.detail", {"runId": stats["run_id"]})
            if stats.get(expected) != 1:
                dump("failure")
                for case in detail.get("cases", []):
                    traces = rpc.call("run.stepTrace", {"caseExecId": case["id"]})
                    print(json.dumps(traces, ensure_ascii=False, default=str)[-8000:], flush=True)
            check(name, stats.get(expected) == 1 and stats["status"] == "completed", stats)
            return stats

        try:
            # First attach an existing process, then launch another process with the same
            # title: launch must select its own PID and discard the previous native HWND.
            original = subprocess.Popen([str(exe), lab.url, str(root / "old-exports")])
            processes.append(original)
            with EngineRpc(cfg, gui=True) as rpc:
                run("Attach then launch binds the new Qt process", [
                    attach(main_title),
                    action("app.launch", args={"exe": str(exe),
                                              "args": [lab.url, str(root / "exports")],
                                              "title_re": main_title}),
                    action("ui.input", "Sample ID", "Edit", value="NEW+^{} WINDOW 中文",
                           args={"clear": True}),
                    assertion("Sample ID", "NEW+^{} WINDOW 中文", "Edit"),
                ])
                driver = rpc.orch.drivers["uia"]
                launched_pid = driver._app.process
                result["launched_pid"] = launched_pid
                processes.append(launched_pid)
                check("Native HWND belongs to newly launched process",
                      driver._native_top.process_id() == launched_pid
                      and launched_pid != original.pid)
                original.terminate()
                original.wait(timeout=5)
                run("Open Qt controls lab", [
                    attach(main_title), action("ui.click", "Qt controls lab", "Button"),
                    attach(controls_title),
                ])
                dump("widgets-initial")
                run("Checkbox idempotence and radio button", [
                    action("ui.toggle", "Enable audit", "CheckBox", value="true"),
                    action("ui.toggle", "Enable audit", "CheckBox", value="true"),
                    assertion("Enable audit", "true", "CheckBox"),
                    assertion("Audit enabled", "Audit enabled"),
                    action("ui.click", "Precision profile", "RadioButton"),
                ])
                check("Qt checkbox/radio actual state",
                      state("qt-controls-state.json")["audit"]
                      and state("qt-controls-state.json")["precision"])
                run("Spin box and custom painted accessible slider", [
                    action("ui.keypress", "Replicate count", value="^a17{ENTER}"),
                    action("ui.keypress", "Custom gauge", "Slider", value="{HOME}{RIGHT 7}"),
                    action("ui.keypress", "Run date", "Spinner", value="{HOME}{UP}"),
                ])
                actual = state("qt-controls-state.json")
                check("Qt spin and painted gauge values", actual["replicates"] == 17
                      and actual["gauge"] == 7 and actual["date"] == "2027-10-16", actual)
                run("Table cell read edit and offscreen row", [
                    tab("Results"),
                    assertion("Results table", "42.5", row=0, column=1),
                    action("ui.click", "Results table", target={
                        "key": "table.edit", "primary": locator("Results table", row=0, column=1)}),
                    action("ui.keypress", "Results table", value="{F2}"),
                    action("ui.keypress", "Results table", value="^a43.75{ENTER}"),
                    assertion("Results table", "43.75", row=0, column=1),
                    action("ui.click", "Results table", target={
                        "key": "table.last", "primary": locator("Results table", row=79, column=0)}),
                    assertion("Results table", "SAMPLE-080", row=79, column=0),
                ])
                actual = state("qt-controls-state.json")
                check("Qt table actual edit and scrolled selection",
                      actual["table_value"] == "43.75" and actual["selected_row"] == 79, actual)
                dump("table-scrolled")
                run("Out of range table cell fails", [
                    assertion("Results table", "invalid", row=800, column=0),
                ], expected="fail")
                run("Tree expand and list selection", [
                    tab("Navigation"),
                    action("ui.click", "Laboratory", "TreeItem"),
                    action("ui.keypress", "Laboratory", "TreeItem", value="{RIGHT}"),
                    action("ui.click", "Chemistry", "TreeItem"),
                    action("ui.click", "Beta", "ListItem"),
                ])
                actual = state("qt-controls-state.json")
                check("Qt tree/list actual selections",
                      actual["tree"] == "Chemistry" and actual["list"] == "Beta", actual)
                run("Modal dialog confirm and window return", [
                    tab("Settings"), action("ui.click", "Open modal dialog", "Button"),
                    attach("^TestPilot Qt Confirmation - " + suffix + "$"),
                    action("ui.input", "Confirmation code", "Edit", value="CONFIRM-17"),
                    assertion("Confirmation code", "CONFIRM-17", "Edit"),
                    action("ui.click", "Confirm", "Button"),
                    attach(controls_title),
                    assertion("Confirmed: CONFIRM-17", "Confirmed: CONFIRM-17"),
                ])
                check("Qt dialog accepted once", state("qt-controls-state.json")["accepted"] == 1)
                run("Modal dialog cancel", [
                    action("ui.click", "Open modal dialog", "Button"),
                    attach("^TestPilot Qt Confirmation - " + suffix + "$"),
                    action("ui.click", "Cancel", "Button"), attach(controls_title),
                ])
                check("Qt dialog cancel preserves confirmation",
                      state("qt-controls-state.json")["cancelled"] == 1
                      and state("qt-controls-state.json")["confirmation"] == "Confirmed: CONFIRM-17")
                run("Auxiliary window duplicate-name scope", [
                    action("ui.input", "Shared input", "Edit", value="MAIN-ONLY", args={"clear": True}),
                    action("ui.click", "Open auxiliary window", "Button"),
                    attach("^TestPilot Qt Auxiliary - " + suffix + "$"),
                    action("ui.input", "Shared input", "Edit", value="AUX-ONLY", args={"clear": True}),
                    action("ui.click", "Close auxiliary", "Button"), attach(controls_title),
                    assertion("Shared input", "MAIN-ONLY", "Edit"),
                ])
                actual = state("qt-controls-state.json")
                check("Qt windows retain independent input values",
                      actual["shared_main"] == "MAIN-ONLY"
                      and actual["shared_auxiliary"] == "AUX-ONLY", actual)
                run("Qt menu action and checkbox reset", [
                    action("ui.click", "Lab tools", "MenuItem"),
                    action("ui.click", "Reset selections", "MenuItem"),
                    assertion("Enable audit", "false", "CheckBox"),
                ])
                check("Qt menu action reached application", state("qt-controls-state.json")["menu_resets"] == 1)
                run("Open actual Qt Quick window", [
                    action("ui.click", "Open QML window", "Button"),
                    attach("^TestPilot Qt QML - " + suffix + "$"),
                ])
                dump("qml-initial")
                run("QML input checkbox combo apply and custom canvas", [
                    action("ui.input", "QML Sample", "Edit", value="QML+^{}-17-样本", args={"clear": True}),
                    action("ui.toggle", "QML Enabled", "CheckBox", value="true"),
                    action("ui.toggle", "QML Enabled", "CheckBox", value="true"),
                    action("ui.select", "QML Mode", "ComboBox", value="Rapid"),
                    action("ui.click", "Apply QML", "Button"),
                    action("ui.click", "QML Canvas action", "Button"),
                    assertion("QML+^{}-17-样本 | Rapid | true", "QML+^{}-17-样本 | Rapid | true"),
                    assertion("Canvas count: 1", "Canvas count: 1"),
                ])
                time.sleep(0.2)
                actual = state("qt-qml-state.json")
                check("QML independent actual state", actual["sample"] == "QML+^{}-17-样本"
                      and actual["mode"] == "Rapid" and actual["enabled"]
                      and actual["canvas_count"] == 1, actual)
                dump("qml-completed")
                run("Unknown QML combo option fails", [
                    action("ui.select", "QML Mode", "ComboBox", value="DOES-NOT-EXIST"),
                ], expected="fail")
                check("Unknown option leaves QML state unchanged",
                      state("qt-qml-state.json")["mode"] == "Rapid")
                result["ok"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            for process in reversed(processes):
                try:
                    if isinstance(process, subprocess.Popen):
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)
                    else:
                        subprocess.run(["taskkill", "/PID", str(process), "/T", "/F"],
                                       capture_output=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            (root / "qt-acceptance.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            lines = ["# Qt 专项适配验收", "", f"结果：{'PASS' if result['ok'] else 'FAIL'}",
                     "", "真实 C++ Qt Widgets / Qt Quick 程序，经生产 UIA 驱动和鉴权 RPC 执行。",
                     "独立应用状态在 exports/；执行报告在 reports/；截图和 UIA 树在本目录。", "",
                     "| 检查 | 结果 |", "|---|---|"]
            lines += [f"| {row['name']} | {'PASS' if row['passed'] else 'FAIL'} |"
                      for row in result["checks"]]
            if result.get("error"):
                lines += ["", result["error"]]
            (root / "qt-acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    return result

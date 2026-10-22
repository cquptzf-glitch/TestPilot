"""Prepare fixtures for the actual desktop shell; execution must be started in its UI.

This helper never calls task.run. It starts local endpoints, launches the built
TestPilot shell with its packaged sidecar, and imports approved Qt test fixtures.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time

from testpilot.simulation.runner import configuration, local_network
from testpilot.simulation.services import LabServices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Successful test-qt evidence directory")
    parser.add_argument("--root", required=True)
    parser.add_argument("--shell", required=True)
    parser.add_argument("--app-exe", required=True)
    parser.add_argument("--reuse", action="store_true", help="Reuse isolated desktop test data")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=args.reuse)
    accepted = json.loads((source / "qt-acceptance.json").read_text(encoding="utf-8"))
    if not accepted["ok"]:
        raise RuntimeError("Qt specialist acceptance must pass before desktop UI verification")
    with local_network(), LabServices(19390, 19391) as lab:
        cfg = configuration(root, lab)
        cfg["uia"]["input_mode"] = "keys"
        cfg["app"]["test_app_version"] = "qt-specialist-desktop"
        exe = str(Path(args.app_exe).resolve())
        cfg["app"]["test_app_exe"] = exe
        config_path = root / "config.json"
        config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        token = secrets.token_urlsafe(40)
        env = dict(os.environ, TESTPILOT_HOME=str(root), TESTPILOT_CONFIG=str(config_path),
                   TESTPILOT_RPC_PORT="19392", TESTPILOT_RPC_TOKEN=token)
        shell_log = (root / "process.log").open("w", encoding="utf-8")
        process = subprocess.Popen([str(Path(args.shell).resolve())], env=env,
                                   stdout=shell_log, stderr=shell_log)
        try:
            import websocket
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Desktop shell exited before RPC became ready")
                try:
                    client = websocket.create_connection(
                        "ws://127.0.0.1:19392", timeout=1,
                        header={"Authorization": "Bearer " + token}, suppress_origin=True,
                        http_no_proxy=["127.0.0.1"])
                    client.settimeout(30)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            rid = 0
            def call(method, params=None):
                nonlocal rid
                rid += 1
                client.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "method": method, "params": params or {}}))
                while True:
                    response = json.loads(client.recv())
                    if response.get("id") != rid:
                        continue
                    if "error" in response:
                        raise RuntimeError(str(response["error"]))
                    return response["result"]
            steps = [{"action": "app.launch", "timeout_ms": 15000,
                      "args": {"exe": exe, "args": [lab.url, str(root / "exports")],
                               "title_re": "^TestPilot Mock Instrument - 19390$"}}]
            conn = sqlite3.connect(source / "testpilot.db")
            try:
                for row in accepted["runs"]:
                    if row["stats"].get("pass") != 1 or row["name"].startswith("Attach then"):
                        continue
                    snapshot = conn.execute(
                        "SELECT dsl_snapshot_json FROM case_exec WHERE run_id=?",
                        (row["stats"]["run_id"],)).fetchone()
                    case = json.loads(snapshot[0])
                    for step in case["steps"]:
                        step["source_text"] = row["name"]
                        steps.append(step)
            finally:
                conn.close()
            old_port = accepted.get("http_port")
            if old_port is None:
                # The first attached main-window title records the isolated lab port.
                old = next(s["args"]["title_re"] for s in steps[1:]
                           if s["action"] == "app.launch" and "Mock Instrument" in s["args"].get("title_re", ""))
                old_port = old.split(" - ")[1].rstrip("$")
            for i, step in enumerate(steps, 1):
                step["id"] = f"desktop-{i:03}"
                if step["action"] == "app.launch" and step["args"].get("title_re"):
                    step["args"]["title_re"] = step["args"]["title_re"].replace(str(old_port), "19390")
            case = {"case_id": "QT-DESKTOP-001", "title": "Qt Widgets + QML 专项真实驱动",
                    "steps": steps}
            call("case.import", {"text": json.dumps(case, ensure_ascii=False)})
            call("dsl.accept", {"caseId": case["case_id"]})
            task = call("task.create", {"name": "Qt 专项：桌面端真实驱动验证",
                                        "caseIds": [case["case_id"]], "remote": False})
            ready = {"shell_pid": process.pid, "task_id": task["task_id"],
                     "case_id": case["case_id"], "steps": len(steps),
                     "rpc_port": 19392, "started_by_ui": False,
                     "config": str(config_path)}
            (root / "ready.json").write_text(json.dumps(ready, indent=2, ensure_ascii=False), encoding="utf-8")
            (root / "rpc-session.json").write_text(json.dumps({"token": token}), encoding="utf-8")
            client.close()
            print(json.dumps(ready, ensure_ascii=False), flush=True)
            while process.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        finally:
            shell_log.close()


if __name__ == "__main__":
    main()

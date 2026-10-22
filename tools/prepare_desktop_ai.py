"""Launch actual packaged desktop with a real model and isolated local ZenTao."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
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
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(Path(args.source) / "testpilot.db") as db:
        raw = json.loads(db.execute("SELECT raw_json FROM dsl_case WHERE case_id='TC-91901'").fetchone()[0])
    with local_network(), LabServices(19580, 19581) as lab:
        lab.case_overrides[91901] = {"id": 91901, "product": 1, "title": raw["title"],
            "precondition": "\n".join(raw["preconditions"]), "steps": raw["steps"],
            "lastEditedDate": "2026-10-16T01:00:00"}
        cfg = configuration(root, lab)
        defaults = load_settings(env={"TESTPILOT_NO_OVERRIDE": "1"})
        cfg["llm"], cfg["vision"]["vlm"] = defaults["llm"], defaults["vision"]["vlm"]
        config_file = root / "config.json"
        config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        token = secrets.token_urlsafe(40)
        env = dict(os.environ, TESTPILOT_HOME=str(root), TESTPILOT_CONFIG=str(config_file),
                   TESTPILOT_RPC_PORT="19582", TESTPILOT_RPC_TOKEN=token)
        with (root / "process.log").open("w", encoding="utf-8") as log:
            shell = subprocess.Popen([str(Path(args.shell).resolve())], env=env, stdout=log, stderr=log)
            import websocket
            deadline = time.monotonic() + 30
            while True:
                try:
                    client = websocket.create_connection("ws://127.0.0.1:19582",
                        timeout=1, header={"Authorization": "Bearer " + token},
                        suppress_origin=True, http_no_proxy=["127.0.0.1"])
                    break
                except OSError:
                    if shell.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
            client.settimeout(30)
            client.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "case.pull",
                                    "params": {"caseIds": [91901], "parse": False}}))
            while True:
                response = json.loads(client.recv())
                if response.get("id") == 1:
                    if "error" in response:
                        raise RuntimeError(response["error"])
                    break
            client.close()
            (root / "rpc-session.json").write_text(json.dumps({"token": token}), encoding="utf-8")
            print(json.dumps({"shell_pid": shell.pid, "case_id": "TC-91901",
                "model": cfg["llm"]["model"], "provider": cfg["llm"]["api_base"],
                "stage": "ready; AI parse must be started in desktop UI"}), flush=True)
            while shell.poll() is None:
                time.sleep(0.5)


if __name__ == "__main__":
    main()

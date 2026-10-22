"""Build and exercise the local lab through real TestPilot JSON-RPC."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import shutil
import subprocess
import threading
import time

from .services import LabServices


@contextmanager
def local_network():
    original = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    for key, value in original.items():
        os.environ[key] = ",".join(filter(None, [value, "127.0.0.1", "localhost", "::1"]))
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_app(output):
    if os.name != "nt":
        raise RuntimeError("The native mock application requires Windows.")
    qmake = os.environ.get("TESTPILOT_QMAKE") or shutil.which("qmake6") or shutil.which("qmake")
    cmake = shutil.which("cmake")
    if not qmake or not cmake:
        raise RuntimeError("Qt 6 SDK (qmake6) and CMake are required to build the C++ mock app.")
    prefix = Path(subprocess.check_output(
        [qmake, "-query", "QT_INSTALL_PREFIX"], text=True).strip())
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent
    build = output.parent / "qt-build"
    subprocess.run([cmake, "-S", str(source), "-B", str(build),
                    "-G", "Visual Studio 17 2022", "-A", "x64",
                    f"-DCMAKE_PREFIX_PATH={prefix}"], check=True, timeout=120)
    subprocess.run([cmake, "--build", str(build), "--config", "Release", "--parallel", "2"],
                   check=True, timeout=180)
    shutil.copy2(build / "Release/MockInstrument.exe", output)
    deploy = prefix / "bin/windeployqt.exe"
    subprocess.run([str(deploy), "--release", "--no-translations", "--no-opengl-sw",
                    "--no-system-d3d-compiler", "--qmldir", str(source), str(output)], check=True, timeout=120)
    return output


def configuration(root, lab):
    from testpilot.config.settings import load_settings
    cfg = load_settings(env={"TESTPILOT_NO_OVERRIDE": "1"})
    root = Path(root).resolve()
    for section, key, relative in (
            ("app", "db", "testpilot.db"), ("app", "screenshot_root", "shots"),
            ("app", "report_root", "reports"), ("log", "dir", "logs"),
            ("vision", "template_dir", "templates")):
        cfg[section][key] = str(root / relative)
    cfg["app"]["test_app_version"] = "mock-instrument-qt-1.0"
    cfg["driver"].update(mode="uia_only", case_retry=0)
    cfg["uia"]["input_mode"] = "set_text"
    cfg["zentao"].update(base_url=lab.url, account="demo", password="demo",
                         token_env="TESTPILOT_SIM_TOKEN", create_task=True, build="1")
    cfg["emulator"].update(endpoint=lab.endpoint, connect_timeout_s=2, call_timeout_s=2)
    cfg["llm"].update(api_base=lab.url + "/v1", model="simulation-fixture",
                      api_key_env="TESTPILOT_SIM_API_KEY", timeout_s=3)
    cfg["vision"]["vlm"].update(api_base=lab.url + "/v1", model="simulation-fixture")
    return cfg


class EngineRpc:
    """Actual authenticated RPC server; no transport or adapter monkeypatches."""
    def __init__(self, cfg, gui=False):
        from testpilot.app.cli import build_registry
        from testpilot.engine.orchestrator import TaskOrchestrator
        from testpilot.rpc import RpcService
        drivers = {}
        if gui:
            from testpilot.drivers.uia_driver import UIADriver
            drivers["uia"] = UIADriver(cfg["uia"])
        self.orch = TaskOrchestrator(cfg, build_registry(), drivers=drivers)
        self.service = RpcService(self.orch, token=secrets.token_urlsafe(32))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.future = None
        self.client = None
        self.next_id = 0

    def start(self):
        import websocket
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.thread.start()
        self.future = asyncio.run_coroutine_threadsafe(
            self.service.serve("127.0.0.1", self.port), self.loop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.future.done():
                self.future.result()
                raise RuntimeError("RPC server exited during startup")
            try:
                self.client = websocket.create_connection(
                    f"ws://127.0.0.1:{self.port}", timeout=1,
                    header={"Authorization": f"Bearer {self.service.token}"},
                    suppress_origin=True, http_no_proxy=["127.0.0.1"])
                self.client.settimeout(90)
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("RPC server startup timed out")

    def call(self, method, params=None):
        self.next_id += 1
        rid = self.next_id
        self.client.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "method": method, "params": params or {}}))
        while True:
            response = json.loads(self.client.recv())
            if response.get("id") != rid:
                continue
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response["result"]

    def close(self):
        if self.client is not None:
            try:
                self.call("shutdown")
            finally:
                self.client.close()
        if self.future is not None:
            self.future.result(timeout=15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.loop.close()
        self.service.close()
        for driver in self.orch.drivers.values():
            if hasattr(driver, "close"):
                driver.close("force")
        self.orch.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def acceptance(root, *, gui=False, app_exe=None):
    """Run happy paths and expected failures; save the evidence in an isolated root."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    checks = []
    result = {"simulation": True, "gui": gui, "gui_framework": "C++ / Qt Widgets" if gui else None,
              "checks": checks, "runs": [],
              "data_root": str(root)}
    process = None

    def check(name, condition, detail=None):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})
        print(f"[{'PASS' if condition else 'FAIL'}] {name}", flush=True)
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    with local_network(), LabServices(0, 0) as lab:
        cfg = configuration(root, lab)
        result["endpoints"] = {"http": lab.url, "emulator": lab.endpoint}
        try:
            if gui:
                exe = Path(app_exe).resolve() if app_exe else build_app(root / "MockInstrument.exe")
                if not exe.is_file():
                    raise RuntimeError(f"Qt mock application not found: {exe}")
                check("Qt Widgets application binary", b"Qt6Widgets.dll" in exe.read_bytes())
                cfg["app"]["test_app_exe"] = str(exe)
                process = subprocess.Popen([str(exe), lab.url, str(root / "exports")])
            with EngineRpc(cfg, gui=gui) as rpc:
                pulled = rpc.call("case.sync", {"productId": 1})
                check("Zentao REST sync and login", lab.state.login_count >= 1, pulled)
                available = rpc.call("case.list")
                case_ids = [f"TC-{cid}" for cid in range(91001, 91008)]
                for cid in case_ids:
                    rpc.call("case.parse", {"caseId": cid})
                    rpc.call("dsl.accept", {"caseId": cid})
                check("Seven fixture cases parsed and approved", len(available["cases"]) == 7)

                def run(cid, name, *, fault=None):
                    if fault:
                        with lab.state.lock:
                            lab.state.faults.update(fault)
                    task = rpc.call("task.create", {"name": name, "caseIds": [f"TC-{cid}"],
                                                    "remote": True})
                    summary = rpc.call("task.run", {"taskId": task["task_id"]})
                    result["runs"].append({"name": name, "task": task, "stats": summary})
                    check(name + " report exists", Path(summary["report"]).is_file())
                    return task, summary

                _, stats = run(91001, "Normal device measurement")
                check("Normal result and remote writeback", stats["pass"] == 1
                      and stats["writeback"] == "synced", stats)
                _, stats = run(91002, "Injected wrong value")
                check("Wrong value is a failure", stats["fail"] == 1, stats)
                _, stats = run(91003, "Delayed measurement")
                check("Async status polling succeeds", stats["pass"] == 1, stats)
                _, stats = run(91004, "Injected device outage")
                check("Device error is a failure", stats["fail"] == 1, stats)
                _, stats = run(91005, "Missing scene")
                check("Setup failure blocks execution", stats["blocked"] == 1, stats)

                task, stats = run(91001, "Writeback recovery", fault={"writeback": 3})
                check("Writeback outage remains pending", stats["writeback"] == "sync_pending", stats)
                recovered = rpc.call("task.syncPending", {"taskId": task["task_id"]})
                check("Writeback compensation succeeds", "synced" in json.dumps(recovered), recovered)
                task, stats = run(91001, "Create recovery", fault={"create": 1})
                check("Create outage preserves local execution",
                      task["zentao_task_id"] is None and stats["pass"] == 1, task)
                recovered = rpc.call("task.syncPending", {"taskId": task["task_id"]})
                check("Create compensation succeeds", "synced" in json.dumps(recovered), recovered)

                before_tasks = len(lab.state.tasks)
                task, stats = run(91001, "Readback recovery", fault={"readback": 1})
                check("Readback failure preserves remote ID without duplicate task",
                      task["zentao_task_id"] is not None and stats["writeback"] == "synced"
                      and len(lab.state.tasks) == before_tasks + 1, task)

                with lab.state.lock:
                    lab.state.faults["auth"] = 1
                    before = lab.state.login_count
                rpc.call("case.sync", {"productId": 1})
                check("Expired token refreshes and replays", lab.state.login_count > before)

                with lab.state.lock:
                    lab.state.faults["create"] = 1
                task = rpc.call("task.create", {"name": "Abort long wait",
                                                "caseIds": ["TC-91007"], "remote": True})
                # Abort is sent from a second authenticated connection while task.run is in flight.
                abort_results = []
                def abort_when_running():
                    import websocket
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if rpc.orch._in_run.is_set():
                            time.sleep(0.2)
                            client = websocket.create_connection(
                                f"ws://127.0.0.1:{rpc.port}", timeout=3,
                                header={"Authorization": f"Bearer {rpc.service.token}"},
                                suppress_origin=True, http_no_proxy=["127.0.0.1"])
                            try:
                                client.send(json.dumps({"jsonrpc": "2.0", "id": "abort",
                                                        "method": "task.abort", "params": {}}))
                                while True:
                                    response = json.loads(client.recv())
                                    if response.get("id") == "abort":
                                        abort_results.append(response.get("result", {}))
                                        return
                            finally:
                                client.close()
                        time.sleep(0.02)
                worker = threading.Thread(target=abort_when_running, daemon=True)
                worker.start()
                started = time.monotonic()
                stats = rpc.call("task.run", {"taskId": task["task_id"]})
                worker.join(timeout=3)
                result["runs"].append({"name": "Abort long wait", "task": task, "stats": stats})
                check("Long task aborts promptly through RPC", stats["status"] == "aborted"
                      and time.monotonic() - started < 5
                      and bool(abort_results and abort_results[0].get("stopping")), stats)
                recovered = rpc.call("task.syncPending", {"taskId": task["task_id"]})
                check("Aborted run is never compensated as done",
                      recovered["results"][0]["result"] == "not_completed"
                      and all(t["status"] != "done" for t in lab.state.tasks.values()
                              if t["name"] == "Abort long wait"), recovered)
                if gui:
                    for iteration in range(1, 3):
                        _, stats = run(91006, f"Qt UI measurement and export #{iteration}")
                        check(f"Real Qt UIA interaction and text assertion #{iteration}",
                              stats["pass"] == 1, stats)
                    exports = sorted((root / "exports").glob("*.json"))
                    exported = [json.loads(path.read_text(encoding="utf-8-sig")) for path in exports]
                    check("Qt UI exports two actual report files", len(exports) == 2
                          and all(item.get("sample_id") == "UI-001"
                                  and item.get("value") == "42.5"
                                  and item.get("mode") == "Precision" for item in exported),
                          exported)
                    result["native_exports"] = [str(path) for path in exports]
                history = rpc.call("run.list", {"limit": 50})
                check("RPC history includes all lab runs", len(history["runs"]) == len(result["runs"]))
                result["ok"] = True
        except Exception as exc:
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            snapshot = lab.state.snapshot()
            (root / "service-evidence.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            (root / "acceptance.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            lines = ["# 本地模拟环境验收", "", f"- 结果：{'PASS' if result.get('ok') else 'FAIL'}",
                     f"- 原生 UI：{'C++ / Qt Widgets，实际 UIA 驱动' if gui else '未启用'}",
                     "- 禅道、模拟器和解析端均为本地模拟；不代表真实服务验收。", "",
                     "| 检查 | 结果 |", "|---|---|"]
            lines.extend(f"| {c['name']} | {'PASS' if c['passed'] else 'FAIL'} |" for c in checks)
            if result.get("error"):
                lines.extend(["", "错误：" + result["error"]])
            lines.extend(["", "完整结果：acceptance.json；接口记录：service-evidence.json。",
                          "每轮引擎报告位于 reports/；原生界面证据位于 shots/。"])
            (root / "acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    return result

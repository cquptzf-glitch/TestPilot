"""Loopback-only real HTTP and WebSocket simulation services."""
import asyncio
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .fixtures import drafts, raw_cases


class LabError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class LabState:
    SCENES = {"normal": (0.25, "42.5"), "slow": (1.2, "42.5"),
              "wrong_value": (0.1, "99.9"), "device_error": (0.1, "42.5")}

    def __init__(self):
        self.lock = threading.RLock()
        self.history = deque(maxlen=1000)
        self.requests = deque(maxlen=1000)
        self.tasks = {}
        self.next_task = 1000
        self.faults = {"create": 0, "writeback": 0, "readback": 0, "auth": 0}
        self.token = "simulation-token"
        self.login_count = 0
        self.generation = 0
        self.state = {}
        self.command("load_scene", {"scene_id": "normal"})

    def capabilities(self):
        return {"protocol": "1.0", "name": "TestPilot local mock emulator",
                "commands": [{"cmd": cmd} for cmd in (
                    "hello", "load_scene", "query_state", "history",
                    "start_measurement", "export_report", "inject_error")],
                "scenes": [{"scene_id": scene} for scene in self.SCENES],
                "terms": {"measurement": "start_measurement"}}

    def command(self, cmd, args):
        with self.lock:
            self.history.append({"cmd": cmd, "args": deepcopy(args), "time": time.time()})
            if cmd == "hello":
                return self.capabilities()
            if cmd == "load_scene":
                scene = str(args.get("scene_id", ""))
                if scene not in self.SCENES:
                    raise LabError("SCENE_NOT_FOUND", f"Unknown scene: {scene}")
                self.generation += 1
                self.state = {"scene": scene, "status": "Ready", "sample_id": "",
                              "mode": "Standard", "value": "", "export_count": 0,
                              "error": ""}
                return deepcopy(self.state)
            if cmd == "query_state":
                path = str(args.get("path", "/"))
                if path in ("/", "/state", "/state/summary"):
                    return deepcopy(self.state)
                node = {"state": self.state}
                for part in path.strip("/").split("/"):
                    if not isinstance(node, dict) or part not in node:
                        raise LabError("PATH_NOT_FOUND", path)
                    node = node[part]
                return deepcopy(node)
            if cmd == "history":
                limit = max(0, min(int(args.get("limit", 100)), 1000))
                return deepcopy(list(self.history)[-limit:]) if limit else []
            if cmd == "inject_error":
                self.state["error"] = str(args.get("error", "Injected device fault"))
                return deepcopy(self.state)
            if cmd == "start_measurement":
                if self.state["scene"] == "device_error" or self.state["error"]:
                    raise LabError("DEVICE_OFFLINE", self.state["error"] or "Injected device offline")
                if self.state["status"] == "Measuring":
                    raise LabError("BUSY", "A measurement is already running")
                sample = str(args.get("sample_id", "")).strip()
                if not sample:
                    raise LabError("SAMPLE_REQUIRED", "Sample ID is required")
                self.state.update(status="Measuring", sample_id=sample,
                                  mode=str(args.get("mode", "Standard")), value="")
                generation = self.generation
                delay, value = self.SCENES[self.state["scene"]]
                timer = threading.Timer(delay, self._complete, args=(generation, value))
                timer.daemon = True
                timer.start()
                return {"accepted": True, "sample_id": sample}
            if cmd == "export_report":
                if self.state["status"] != "Completed":
                    raise LabError("NOT_READY", "Complete a measurement first")
                self.state["export_count"] += 1
                return {"sample_id": self.state["sample_id"], "value": self.state["value"],
                        "mode": self.state["mode"], "export_count": self.state["export_count"]}
            raise LabError("UNKNOWN_COMMAND", str(cmd))

    def _complete(self, generation, value):
        with self.lock:
            if generation == self.generation and self.state["status"] == "Measuring":
                self.state.update(status="Completed", value=value)

    def snapshot(self):
        with self.lock:
            return {"simulation": True, "state": deepcopy(self.state),
                    "tasks": deepcopy(self.tasks), "faults": deepcopy(self.faults),
                    "login_count": self.login_count, "requests": list(self.requests),
                    "history": list(self.history)}

    def fail_once(self, key):
        with self.lock:
            remaining = self.faults.get(key, 0)
            if remaining > 0:
                self.faults[key] -= 1
                return True
        return False


class LabServices:
    def __init__(self, http_port=19380, ws_port=19381):
        self.http_port = http_port
        self.ws_port = ws_port
        self.state = LabState()
        self.case_overrides = {}
        self.ws_delays = {}  # Acceptance-only delayed receipts, keyed by command.
        self.http = None
        self.loop = None
        self.ws = None
        self.http_thread = None
        self.ws_thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def endpoint(self):
        return f"ws://127.0.0.1:{self.ws_port}"

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass  # Qt may close a keep-alive socket when its test process exits.

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def do_PUT(self):
                self.handle_request()

            def handle_request(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    limit = 24 * 1024 * 1024 if self.path == "/v1/chat/completions" else 1048576
                    if not 0 <= length <= limit:
                        self.respond(413, {"error": "Body too large"})
                        return
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise ValueError("Body must be an object")
                    status, result = owner.route(self.command, self.path, body, self.headers)
                except (ValueError, TypeError) as exc:
                    status, result = 400, {"error": str(exc)}
                except LabError as exc:
                    status, result = 409, {"error": exc.code, "message": str(exc)}
                self.respond(status, result)

            def respond(self, status, result):
                is_html = isinstance(result, str)
                data = (result if is_html else json.dumps(result, ensure_ascii=False)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if is_html
                                 else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", self.http_port), Handler)
        self.http.daemon_threads = True
        self.http_port = self.http.server_port
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.loop = asyncio.new_event_loop()
        self.ws_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.ws_thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start_ws(), self.loop).result(timeout=5)
        except Exception:
            self.close()
            raise
        return self

    async def _start_ws(self):
        from websockets.asyncio.server import serve
        self.ws = await serve(self._handle_ws, "127.0.0.1", self.ws_port,
                              max_size=1048576, close_timeout=1, ping_interval=None)
        self.ws_port = self.ws.sockets[0].getsockname()[1]

    async def _handle_ws(self, socket):
        async for message in socket:
            started = time.monotonic()
            request = {}
            try:
                request = json.loads(message)
                if not isinstance(request, dict):
                    raise LabError("INVALID_REQUEST", "Request must be an object")
                if request.get("ver") != "1.0":
                    raise LabError("VERSION_MISMATCH", "Expected protocol 1.0")
                delay = self.ws_delays.get(request.get("cmd"), 0)
                if delay:
                    await asyncio.sleep(delay)
                data = self.state.command(request.get("cmd"), request.get("args") or {})
                response = {"id": request.get("id"), "ok": True, "data": data}
            except (LabError, ValueError, TypeError) as exc:
                response = {"id": request.get("id") if isinstance(request, dict) else None,
                            "ok": False, "err": {"code": getattr(exc, "code", "BAD_JSON"),
                                                "msg": str(exc)}}
            response["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            from websockets.exceptions import ConnectionClosed
            try:
                await socket.send(json.dumps(response))
            except ConnectionClosed:
                return

    def route(self, method, raw_path, body, headers):
        parsed = urlsplit(raw_path)
        path = parsed.path.rstrip("/") or "/"
        with self.state.lock:
            self.state.requests.append({"method": method, "path": path, "time": time.time()})
            return self._route_locked(method, path, parse_qs(parsed.query), body, headers)

    def _route_locked(self, method, path, query, body, headers):
        if path == "/" and method == "GET":
            return 200, DASHBOARD
        if path == "/health":
            return 200, {"ok": True, "simulation": True, "http": self.url, "emulator": self.endpoint}
        if path == "/lab/snapshot" and method == "GET":
            return 200, self.state.snapshot()
        if path == "/lab/faults" and method == "POST":
            for key, value in body.items():
                if key not in self.state.faults or not isinstance(value, int) or not 0 <= value <= 10:
                    return 400, {"error": "Expected create/writeback/readback/auth counters 0..10"}
            self.state.faults.update(body)
            return 200, deepcopy(self.state.faults)
        if path == "/instrument/state" and method == "GET":
            return 200, self.state.command("query_state", {"path": "/state"})
        instrument = {"/instrument/measure": "start_measurement",
                      "/instrument/export": "export_report", "/instrument/scene": "load_scene"}
        if path in instrument and method == "POST":
            return 200, self.state.command(instrument[path], body)
        if path == "/v1/chat/completions" and method == "POST":
            texts = body.get("messages") or []
            content = texts[-1].get("content", "") if texts else ""
            prompt = (content if isinstance(content, str) else
                      "\n".join(item.get("text", "") for item in content if item.get("type") == "text"))
            if "## 待解析用例" in prompt:
                raw = json.loads(prompt.split("## 待解析用例", 1)[1].strip())
                case = drafts(self.http_port).get(int(raw.get("zentao_case_id") or 0))
                if case is None:
                    return 422, {"error": "Local parser only supports supplied lab fixtures"}
            else:
                case = {"category": "unknown", "confidence": 0.1,
                        "evidence": "Simulation fixture; no model analysis was performed.",
                        "suggestion": "Inspect actual trace and injected scenario."}
            return 200, {"model": "simulation-fixture", "choices": [
                {"message": {"role": "assistant", "content": json.dumps(case)}}],
                "usage": {"total_tokens": 0}}
        if path == "/api.php/v1/tokens" and method == "POST":
            if body != {"account": "demo", "password": "demo"}:
                return 401, {"error": "Use demo/demo in this local simulation"}
            self.state.login_count += 1
            return 200, {"token": self.state.token}
        if not path.startswith("/api.php/v1/"):
            return 404, {"error": "Not found"}
        if headers.get("Token") != self.state.token or self.state.fail_once("auth"):
            return 401, {"error": "Simulation token expired"}
        cases = {**raw_cases(self.http_port), **self.case_overrides}
        if path == "/api.php/v1/products/1/testcases" and method == "GET":
            page = max(1, int(query.get("page", ["1"])[0]))
            limit = max(1, min(100, int(query.get("limit", ["100"])[0])))
            return 200, {"testcases": [{k: v for k, v in row.items() if k != "steps"}
                                       for row in list(cases.values())[(page-1)*limit:page*limit]],
                         "total": len(cases)}
        if path == "/api.php/v1/products/1/modules" and method == "GET":
            return 200, {"modules": [{"id": 10, "name": "Local acceptance lab"}]}
        match = re.fullmatch(r"/api.php/v1/testcases/(\d+)", path)
        if match and method == "GET":
            case = cases.get(int(match[1]))
            return (200, case) if case else (404, {"error": "Case not found"})
        if path == "/api.php/v1/testtasks" and method == "POST":
            if self.state.fail_once("create"):
                return 503, {"error": "Injected task creation outage"}
            required = {"product", "name", "build", "begin", "end"}
            if not required <= body.keys() or any(not body[k] for k in required):
                return 422, {"status": "fail", "message": "Missing required testtask fields"}
            try:
                linked = [int(cid) for cid in body.get("cases", "").split(",") if cid]
            except ValueError:
                return 422, {"status": "fail", "message": "Invalid case ids"}
            if any(cid not in cases for cid in linked):
                return 422, {"status": "fail", "message": "Case mapping does not exist"}
            self.state.next_task += 1
            task = {**body, "testcases": [{"id": cid} for cid in linked],
                    "id": self.state.next_task, "status": "wait", "updates": []}
            self.state.tasks[task["id"]] = task
            return 201, deepcopy(task)
        match = re.fullmatch(r"/api.php/v1/testtasks/(\d+)", path)
        if match:
            task = self.state.tasks.get(int(match[1]))
            if task is None:
                return 404, {"error": "Task not found"}
            if method == "GET":
                if self.state.fail_once("readback"):
                    return 503, {"error": "Injected readback outage"}
                return 200, deepcopy(task)
            if method == "PUT":
                if self.state.fail_once("writeback"):
                    return 503, {"error": "Injected writeback outage"}
                if not {"name", "build", "begin", "end", "status", "desc"} <= body.keys():
                    return 200, {"status": "fail", "message": "Incomplete update"}
                if "comment" in body:
                    return 200, {"status": "fail", "message": "Use supported desc field"}
                task.update(body)
                task["updates"].append(deepcopy(body))
                return 200, deepcopy(task)
        return 404, {"error": "Unsupported simulation endpoint"}

    def close(self):
        if self.ws is not None and self.loop is not None and self.loop.is_running():
            async def stop():
                self.ws.close()
                await self.ws.wait_closed()
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=5)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.ws_thread is not None:
            self.ws_thread.join(timeout=5)
        if self.loop is not None:
            self.loop.close()
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
        if self.http_thread is not None:
            self.http_thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


DASHBOARD = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>TestPilot 本地模拟实验室</title><style>
body{font:16px system-ui;background:#f3f6f8;color:#17242b;max-width:1100px;margin:40px auto}
h1{color:#14796e}pre{background:white;padding:24px;border-radius:12px;overflow:auto}
span{color:#6a7880}a{color:#14796e}</style>
<h1>TestPilot 本地模拟实验室</h1><p>模拟被测软件 · 禅道 REST · 模拟器 WebSocket</p>
<p><span>仅用于本地测试；demo / demo；解析接口返回固定样例，不调用真实模型。</span></p>
<p><a href="/health">服务地址</a> · <a href="/lab/snapshot">完整请求及任务记录</a></p>
<h2>设备状态与禅道回写</h2><pre id="data">正在连接…</pre>
<script>async function refresh(){try{let s=await(await fetch('/lab/snapshot')).json();
document.querySelector('#data').textContent=JSON.stringify({state:s.state,tasks:s.tasks,
faults:s.faults,login_count:s.login_count,recent_requests:s.requests.slice(-12)},null,2)}
catch(e){document.querySelector('#data').textContent=String(e)}}refresh();setInterval(refresh,1000)
</script></html>"""

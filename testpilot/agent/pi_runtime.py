"""Pi RPC subprocess runtime.

Pi owns reasoning / tool-call planning only. Built-in tools are disabled. The only tools
visible to the model come from pi_rpc_tools.mjs and proxy back to TestPilot's main RPC
process with a short-lived capability token scoped to one workspace.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from testpilot.core.contracts import HealthReport
from testpilot.core.errors import AbortRequested, AgentError

DEFAULT_TOOLS = (
    "tp_read_file", "tp_find_files", "tp_grep",
    "tp_svn_status", "tp_svn_diff", "tp_svn_log",
    "tp_case_search", "tp_case_detail",
)


class PiAgentRuntime:
    def __init__(self, cfg: dict | None, *, rpc_url: str, rpc_token: str):
        self.cfg = dict(cfg or {})
        self.rpc_url = str(rpc_url)
        self.rpc_token = str(rpc_token)
        self.executable = str(self.cfg.get("executable") or "pi").strip() or "pi"
        self.provider = str(self.cfg.get("provider") or "").strip()
        self.model = str(self.cfg.get("model") or "").strip()
        self.timeout_s = max(10, int(self.cfg.get("timeout_s") or 300))

    def _executable(self) -> str | None:
        raw = os.path.expandvars(os.path.expanduser(self.executable))
        path = Path(raw)
        return str(path.resolve()) if path.is_file() else shutil.which(raw)

    def health(self) -> HealthReport:
        exe = self._executable()
        if not exe:
            return HealthReport(
                False,
                f"Pi 未安装或不可执行: {self.executable!r}；"
                "请安装 @earendil-works/pi-coding-agent 并确认 pi --version",
            )
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True,
                                  timeout=5, check=False)
            text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
            return HealthReport(proc.returncode == 0, text or exe)
        except Exception as exc:
            return HealthReport(False, f"Pi 自检失败: {exc}")

    def run(self, prompt: str, *, cwd: str | None = None,
            tools: list[str] | None = None, timeout_s: int | None = None,
            abort=None, progress=None) -> dict:
        text = str(prompt or "").strip()
        if not text:
            raise AgentError("Agent prompt 不能为空")
        exe = self._executable()
        if not exe:
            raise AgentError(f"Pi 不可用: {self.executable!r}")

        workspace = Path(cwd or ".").expanduser().resolve()
        if not workspace.is_dir():
            raise AgentError(f"Agent 工作目录不存在: {workspace}")

        extension = Path(__file__).with_name("pi_rpc_tools.mjs").resolve()
        if not extension.is_file():
            raise AgentError(f"Pi RPC 工具扩展缺失: {extension}")

        enabled = list(dict.fromkeys(tools or DEFAULT_TOOLS))
        if not enabled:
            raise AgentError("Agent 没有可用的 RPC 工具")

        command = [
            exe, "--mode", "rpc", "--no-session",
            "--no-builtin-tools", "--no-mcp",
            "--no-skills", "--no-context-files", "--no-prompt-templates",
            "--no-extensions", "-e", str(extension),
        ]
        if self.provider:
            command.extend(["--provider", self.provider])
        if self.model:
            command.extend(["--model", self.model])

        env = dict(os.environ)
        env.update({
            "TESTPILOT_AGENT_RPC_URL": self.rpc_url,
            "TESTPILOT_AGENT_RPC_TOKEN": self.rpc_token,
            "TESTPILOT_AGENT_WORKSPACE": str(workspace),
            "TESTPILOT_AGENT_TOOLS": ",".join(enabled),
        })
        limit = max(10, int(timeout_s or self.timeout_s))
        started = time.monotonic()
        q: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        stderr: list[bytes] = []

        proc = subprocess.Popen(
            command, cwd=str(workspace), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        def stdout_reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    q.put(("out", line.rstrip(b"\r\n")))
            finally:
                q.put(("eof", None))

        def stderr_reader():
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr.append(chunk)
                if len(stderr) > 64:
                    del stderr[:-64]

        threading.Thread(target=stdout_reader, daemon=True, name="pi-rpc-out").start()
        threading.Thread(target=stderr_reader, daemon=True, name="pi-rpc-err").start()

        prompt_id = "prompt-" + uuid.uuid4().hex
        final_id = "final-" + uuid.uuid4().hex
        self._send(proc, {"id": prompt_id, "type": "prompt", "message": text})
        accepted = False
        settled = False
        requested_final = False
        event_count = 0
        tool_calls = 0

        try:
            while True:
                if abort is not None:
                    abort.check()
                elapsed = time.monotonic() - started
                if elapsed >= limit:
                    try:
                        self._send(proc, {"type": "abort"})
                    except Exception:
                        pass
                    raise AgentError(f"Pi Agent 超时（{limit}s）")

                try:
                    kind, raw = q.get(timeout=min(0.2, max(0.01, limit - elapsed)))
                except queue.Empty:
                    if proc.poll() is not None:
                        raise self._exit_error(proc, stderr) from None
                    continue
                if kind == "eof":
                    if proc.poll() is not None and not requested_final:
                        raise self._exit_error(proc, stderr)
                    continue
                if raw is None:
                    continue
                try:
                    record = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise AgentError(f"Pi RPC 输出不是合法 JSONL: {raw[:200]!r}") from exc

                event_count += 1
                rtype = record.get("type")
                if rtype == "response" and record.get("id") == prompt_id:
                    if not record.get("success"):
                        raise AgentError(str(record.get("error") or "Pi prompt 被拒绝"))
                    accepted = True
                elif rtype == "tool_execution_start":
                    tool_calls += 1
                    if progress:
                        progress({
                            "phase": "agent_tool",
                            "tool": record.get("toolName"),
                            "tool_calls": tool_calls,
                            "events": event_count,
                        })
                elif rtype == "agent_settled":
                    settled = True

                if accepted and settled and not requested_final:
                    self._send(proc, {"id": final_id, "type": "get_last_assistant_text"})
                    requested_final = True
                    continue

                if rtype == "response" and record.get("id") == final_id:
                    if not record.get("success"):
                        raise AgentError(str(record.get("error") or "读取 Pi 最终回复失败"))
                    data = record.get("data") or {}
                    final_text = str(data.get("text") or "").strip()
                    if not final_text:
                        raise AgentError("Pi Agent 未返回最终文本")
                    return {
                        "text": final_text,
                        "tool_calls": tool_calls,
                        "events": event_count,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
        except AbortRequested:
            try:
                self._send(proc, {"type": "abort"})
            except Exception:
                pass
            raise
        finally:
            _terminate(proc)

    @staticmethod
    def _send(proc, payload: dict) -> None:
        if proc.stdin is None:
            raise AgentError("Pi stdin 已关闭")
        proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        proc.stdin.flush()

    @staticmethod
    def _exit_error(proc, stderr_parts: list[bytes]) -> AgentError:
        detail = b"".join(stderr_parts).decode("utf-8", "replace")[-3000:].strip()
        return AgentError(f"Pi 提前退出(code={proc.poll()}): {detail or '无 stderr'}")


def _terminate(proc) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=1)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

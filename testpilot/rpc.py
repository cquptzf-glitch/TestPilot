"""JSON-RPC 2.0 over WebSocket（127.0.0.1:9301）— 壳 ↔ 引擎 IPC（设计 2.5 / 5.7）。

方法表（与设计 2.5 对齐，重复命名已消歧）：
- ``health`` / ``shutdown``
- 用例：``case.pull`` ``case.sync`` ``case.list`` ``case.detail`` ``case.export``
        ``case.import`` ``case.parse``
- 方案：``suite.list`` ``suite.detail`` ``suite.create`` ``suite.update`` ``suite.delete``
- 任务：``task.create`` ``task.run`` ``task.abort`` ``task.list`` ``task.detail``
        ``task.syncPending``
- 报告四层钻取：``run.list`` ``run.detail`` ``run.caseDetail`` ``run.stepTrace``
  ``run.report`` ``run.compare`` ``run.shotDiff``（设计里的 ``case.detail`` / ``step.detail``
  与用例管理组的 ``case.detail`` 重名，此处收敛为 ``run.*`` 前缀，语义更清晰）
- 人审：``kb.pending`` ``kb.confirm`` ``kb.reject`` ``kb.setDisposition``
        ``kb.caseCtxList`` ``kb.caseCtxSave`` ``kb.caseCtxDelete`` ``kb.envCompat``
- DSL 审阅：``dsl.review`` ``dsl.save`` ``dsl.accept`` ``dsl.reject``
- 配置：``config.describe`` ``config.apply`` ``config.reset`` ``config.overrideText``
- 事件推送：``run.progress`` / ``case.finished`` / ``step.finished``（领域事件原样转发）

执行模型：除 ``shutdown`` / ``task.abort``（只设事件，必须低延迟）外，全部方法
在线程池里执行 —— 短操作走 IO 池、``task.run`` 走独立长任务池。事件循环线程
**绝不**直接触碰 sqlite 连接（跨线程并发使用同一连接的防线在调用侧，不在 sqlite）。
"""
import asyncio
import base64
import json
import logging
import os
import secrets
import hmac
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait

from testpilot.infra.jobs import CaseJobs

from testpilot.core.errors import (AbortRequested, INTERNAL_ERROR, INVALID_PARAMS,
                                   METHOD_NOT_FOUND, PARSE_ERROR, RpcError,
                                   TestPilotError, method_not_found)

log = logging.getLogger("testpilot.rpc")

# 在事件循环线程直接执行的方法（不触 DB、不做 IO）：必须保持低延迟，
# 让 task.run 长时间执行期间 abort/shutdown 仍能即时生效。
_INLINE = frozenset({"shutdown", "task.abort", "job.start", "job.status", "job.cancel"})

# 长任务方法单独走一个执行器：读写类请求不能排在数小时的 task.run 后面
_LONG = frozenset({"task.run"})

_DRAIN_TIMEOUT_S = 15.0     # shutdown 时等待执行中 task.run 收尾的上限
_AUTOMATION_INLINE_PNG_MAX = 128 * 1024
_AUTOMATION_ASSET_KEEP = 6
_AGENT_AUDIT_LIMIT = 200

_AGENT_RPC_METHODS = frozenset({
    "agent.fs.read", "agent.fs.find", "agent.fs.grep",
    "agent.svn.status", "agent.svn.diff", "agent.svn.log",
    "agent.case.search", "agent.case.detail",
})


class RpcService:
    def __init__(self, orchestrator=None, *, token=None):
        self.token = token or os.environ.get("TESTPILOT_RPC_TOKEN") or secrets.token_urlsafe(32)
        self.orchestrator = orchestrator
        self._local = threading.local()
        self._io_orchestrator = None
        self._case_orchestrator = None
        self._automation_sessions = {}
        self._automation_owner = None
        self._agent_tokens: dict[str, dict] = {}
        self._agent_audits: dict[str, list[dict]] = {}
        self._agent_tokens_lock = threading.RLock()
        self._interaction_lock = threading.Lock()
        self._jobs = CaseJobs(self._dispatch_case_job)
        self._run_reserved = threading.Event()
        self._subs: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._unsubscribe = None
        # 短操作（读写/网络）共用单线程执行器：sqlite 连接只允许被串行使用
        # （check_same_thread 只是解除校验），事件循环线程**绝不**直接触库。
        # 长任务（task.run）走独立执行器，读写请求不必排在数小时的 run 后面。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpc-io")
        self._run_executor = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="rpc-run")
        # 每条连接一把发送锁：请求响应与事件推送并发写同一 ws 时避免帧交错
        self._send_locks: dict = {}
        if orchestrator is not None:
            bus = getattr(orchestrator, "bus", None)
            if bus is not None:
                self._unsubscribe = bus.subscribe(self._on_event)

    def _dispatch_case_job(self, method, params, abort, progress):
        from testpilot.engine.orchestrator import TaskOrchestrator
        orch = self.orchestrator
        if isinstance(orch, TaskOrchestrator):
            if self._case_orchestrator is None:
                from testpilot.storage.db import connect
                path = orch.conn.execute("PRAGMA database_list").fetchone()[2]
                if not path:
                    raise TestPilotError("用例后台任务需要文件数据库")
                self._case_orchestrator = TaskOrchestrator(
                    orch.cfg, orch.registry, conn=connect(path),
                    drivers=orch.drivers, bus=orch.bus)
            orch = self._case_orchestrator
        self._local.orchestrator = orch
        if method == "change.svn.generate":
            service = self._change_service(orch)
            service.abort, service.progress = abort, progress
            return service.generate(params)

        previous_abort, previous_progress = orch.cases.abort, orch.cases.progress
        orch.cases.abort, orch.cases.progress = abort, progress
        try:
            return self.dispatch(method, params)
        finally:
            orch.cases.abort, orch.cases.progress = previous_abort, previous_progress

    # ---------- 方法表 ----------

    def _dispatch_io(self, method: str, params: dict):
        from testpilot.engine.orchestrator import TaskOrchestrator
        orch = self.orchestrator
        if isinstance(orch, TaskOrchestrator):
            if self._io_orchestrator is None:
                from testpilot.storage.db import connect
                path = orch.conn.execute("PRAGMA database_list").fetchone()[2]
                if path:
                    self._io_orchestrator = TaskOrchestrator(
                        orch.cfg, orch.registry, conn=connect(path),
                        drivers=orch.drivers, bus=orch.bus)
                    # IO orchestrator owns a separate sqlite connection, but remote
                    # task reconciliation/compensation must share the root run
                    # lifecycle lock or it can mutate task remote state after run_task
                    # already froze an older task snapshot.
                    self._io_orchestrator._run_lock = orch._run_lock
                    self._io_orchestrator._in_run = orch._in_run
            if self._io_orchestrator is not None:
                self._local.orchestrator = self._io_orchestrator
        return self.dispatch(method, params)

    def dispatch(self, method: str, params: dict):
        table = {
            "job.start": lambda p: self._jobs.start(_require(p, "method"), p.get("params", {})),
            "job.status": lambda p: self._jobs.status(p.get("jobId")),
            "job.cancel": lambda p: self._jobs.cancel(_require(p, "jobId")),
            "health": self._health,
            "shutdown": self._shutdown,

            "case.pull": self._case_pull,
            "case.sync": self._case_sync,
            "case.list": self._case_list,
            "case.detail": self._case_detail,
            "case.export": self._case_export,
            "case.import": self._case_import,
            "case.parse": self._case_parse,
            "change.svn.preview": self._change_svn_preview,

            "suite.list": self._suite_list,
            "suite.detail": self._suite_detail,
            "suite.create": self._suite_create,
            "suite.update": self._suite_update,
            "suite.delete": self._suite_delete,

            "task.create": self._task_create,
            "task.run": self._task_run,
            "task.abort": self._task_abort,
            "task.list": self._task_list,
            "task.detail": self._task_detail,
            "task.syncPending": self._task_sync_pending,
            "task.reconcile": lambda p: self._orch.reconcile_remote_task(
                _to_int(p, "taskId"), _to_int(p, "zentaoTaskId")),

            "run.list": self._run_list,
            "run.detail": self._run_detail,
            "run.caseDetail": self._run_case_detail,
            "run.stepTrace": self._run_step_trace,
            "run.report": self._run_report,
            "run.compare": self._run_compare,
            "run.shotDiff": self._run_shot_diff,

            "kb.pending": self._kb_pending,
            "kb.confirm": self._kb_confirm,
            "kb.reject": self._kb_reject,
            "kb.setDisposition": self._kb_set_disposition,
            "kb.caseCtxList": self._kb_case_ctx_list,
            "kb.caseCtxSave": self._kb_case_ctx_save,
            "kb.caseCtxDelete": self._kb_case_ctx_delete,
            "kb.envCompat": self._kb_env_compat,

            "dsl.review": self._dsl_review,
            "dsl.save": self._dsl_save,
            "dsl.accept": self._dsl_accept,
            "dsl.reject": self._dsl_reject,

            "automation.sessions": self._automation_sessions_list,
            "automation.closeSession": self._automation_close_session,
            "automation.tools": self._automation_tools,
            "automation.invoke": self._automation_invoke,
            "automation.history": self._automation_history,
            "automation.draft": self._automation_draft,
            "automation.removeStep": self._automation_remove_step,
            "automation.updateStep": self._automation_update_step,
            "automation.rerunStep": self._automation_rerun_step,
            "automation.moveStep": self._automation_move_step,
            "automation.restore": self._automation_restore,
            "automation.reset": self._automation_reset,

            "agent.fs.read": self._agent_fs_read,
            "agent.fs.find": self._agent_fs_find,
            "agent.fs.grep": self._agent_fs_grep,
            "agent.svn.status": self._agent_svn_status,
            "agent.svn.diff": self._agent_svn_diff,
            "agent.svn.log": self._agent_svn_log,
            "agent.case.search": self._agent_case_search,
            "agent.case.detail": self._agent_case_detail,

            "config.describe": self._config_describe,
            "config.apply": self._config_apply,
            "config.reset": self._config_reset,
            "config.overrideText": self._config_override_text,
        }
        fn = table.get(method)
        if fn is None:
            raise method_not_found(method)
        try:
            return fn(params or {})
        except (RpcError, AbortRequested):
            raise
        except TestPilotError as e:
            raise RpcError(INTERNAL_ERROR, str(e), {"method": method}) from e

    def methods(self) -> list[str]:
        """方法清单（壳启动时自检/文档生成用）。"""
        return sorted({
            "job.start", "job.status", "job.cancel",
            "health", "shutdown", "case.pull", "case.sync", "case.list",
            "case.detail", "case.export", "case.import", "case.parse",
            "change.svn.preview",
            "suite.list", "suite.detail", "suite.create", "suite.update",
            "suite.delete", "task.create", "task.run", "task.abort", "task.list",
            "task.detail", "task.syncPending", "task.reconcile", "run.list", "run.detail",
            "run.caseDetail", "run.stepTrace", "run.report", "run.compare",
            "run.shotDiff",
            "kb.pending", "kb.confirm", "kb.reject", "kb.setDisposition",
            "kb.caseCtxList", "kb.caseCtxSave", "kb.caseCtxDelete",
            "kb.envCompat",
            "dsl.review", "dsl.save", "dsl.accept", "dsl.reject",
            "automation.sessions", "automation.closeSession",
            "automation.tools", "automation.invoke", "automation.history",
            "automation.draft", "automation.removeStep", "automation.updateStep",
            "automation.rerunStep", "automation.moveStep",
            "automation.restore", "automation.reset",
            "config.describe", "config.apply", "config.reset",
            "config.overrideText",
        })

    # ---------- 基础 ----------

    @property
    def _orch(self):
        if self.orchestrator is None:
            raise RpcError(INTERNAL_ERROR, "orchestrator 未装配（引擎未启动）")
        return getattr(self._local, "orchestrator", self.orchestrator)

    def _health(self, _p):
        return self._orch.health()

    def _shutdown(self, p):
        """优雅退出：中止运行中的任务 -> 等执行线程收尾（有上限）-> 请求循环退出。

        不排空就直接关库会让运行中的 task.run 在连接关闭后继续跑，
        后续 SQL 全部报 "Cannot operate on a closed database"。
        """
        orch = self._orch
        self._jobs.cancel_all()
        abort = _bool_param(p, "abortRunning", default=True)
        if abort:
            orch.abort()
        if self._loop is not None and self._stop is not None:
            threading.Thread(target=self._drain_then_stop, daemon=True).start()
        return {"ok": True, "aborted": abort}

    def _drain_then_stop(self) -> None:
        """等 task.run 收尾（abort 后它会在最近的取消点安全结束），再请求退出。"""
        self.drain()
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    def drain(self, timeout: float = _DRAIN_TIMEOUT_S) -> None:
        """等两个执行器排空（有上限）。退出路径必须先排空再关库连接。"""
        done, _ = self._wait_idle(self._run_executor, timeout)
        if not done:
            log.warning(f"等待运行中任务收尾超过 {timeout:.0f}s，继续退出流程")
        self._wait_idle(self._executor, timeout)
        self._jobs.cancel_all()
        self._wait_idle(self._jobs._pool, timeout)

    @staticmethod
    def _wait_idle(executor: ThreadPoolExecutor,
                   timeout: float) -> tuple[bool, object]:
        """提交哨兵任务排队等待之前的任务全部完成；返回 (是否按时完成, 未来)。"""
        try:
            fut = executor.submit(lambda: None)
        except RuntimeError:            # 执行器已关闭
            return True, None
        done, _ = futures_wait([fut], timeout=timeout)
        return done, fut

    # ---------- 用例 ----------

    def _case_pull(self, p):
        ids = _to_int_list(p, "caseIds")
        return self._orch.cases.pull(
            ids, parse=_bool_param(p, "parse", default=False))

    def _case_sync(self, p):
        return self._orch.cases.sync(
            p.get("productId"),
            parse=_bool_param(p, "parse", default=False),
            full=_bool_param(p, "full", default=False))

    def _case_list(self, p):
        from testpilot.storage import repos
        pid = p.get("productId") or (self._orch.cfg.get("zentao") or {}).get("product_id")
        state = repos.get_sync_state(self._orch.conn, "zentao", str(pid or "default"))
        return {"cases": self._orch.cases.list_cases(
            status=p.get("status"), sync_status=p.get("syncStatus")),
            "sync_state": state}

    def _case_detail(self, p):
        return self._orch.cases.show(_require(p, "caseId"))

    def _case_export(self, p):
        return {"content": self._orch.cases.export_case(
            _require(p, "caseId"), p.get("format", "json"))}

    def _case_import(self, p):
        return self._orch.cases.import_draft(
            _require(p, "text"), p.get("format", "json"), case_id=p.get("caseId"))

    def _case_parse(self, p):
        target = p.get("appTarget")
        if target is not None:
            allowed = {"exe", "title_re", "args"}
            if not isinstance(target, dict) or not target or set(target) - allowed:
                raise RpcError(
                    INVALID_PARAMS,
                    "appTarget 仅允许 exe/title_re/args")
            if not (target.get("exe") or target.get("title_re")):
                raise RpcError(INVALID_PARAMS, "appTarget 至少需要 exe 或 title_re")
            for key, value in target.items():
                limit = 4096 if key == "args" else 2048
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise RpcError(
                        INVALID_PARAMS,
                        f"appTarget.{key} 需为 1..{limit} 字符的非空字符串")
            if target.get("args") and not target.get("exe"):
                raise RpcError(INVALID_PARAMS, "appTarget.args 只能与 exe 一起使用")
        all_stale = _bool_param(p, "allStale", default=False)
        scopes = sum(("caseIds" in p, all_stale, bool(p.get("caseId"))))
        if scopes != 1:
            raise RpcError(INVALID_PARAMS, "指定 caseId、caseIds 或 allStale 中的一项")
        if "caseIds" in p:
            ids = p["caseIds"]
            if (not isinstance(ids, list) or not ids or len(ids) > 500
                    or any(not isinstance(cid, str) or not cid.strip() for cid in ids)):
                raise RpcError(INVALID_PARAMS, "caseIds 必须为 1–500 条非空用例 ID 字符串")
            return self._orch.cases.parse_many(ids, app_target=target)
        if all_stale:
            return self._orch.cases.parse_many(None, only_stale=True, app_target=target)
        return self._orch.cases.parse(_require(p, "caseId"), app_target=target)

    # ---------- SVN 变更 -> Agent -> 测试用例 ----------

    def _change_service(self, orch=None):
        from testpilot.agent.change_service import ChangeAnalysisService
        target = orch or self._orch
        return ChangeAnalysisService(
            target.conn, target.cases, target.cfg, self._run_pi_agent)

    def _run_pi_agent(self, *, prompt, workspace, tools, abort=None, progress=None):
        from testpilot.agent.pi_runtime import PiAgentRuntime

        token = self.issue_agent_token(workspace)
        cfg = self._orch.cfg
        rpc_cfg = cfg.get("rpc") or {}
        host = str(rpc_cfg.get("host") or "127.0.0.1")
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        url = f"ws://{host}:{int(rpc_cfg.get('port') or 9301)}"
        def report_terminal(phase: str, error: str | None = None) -> None:
            if progress is None:
                return
            payload = {
                "phase": phase,
                "agent_audit": self.agent_audit(token),
            }
            if error:
                payload["agent_error"] = str(error)[:500]
            try:
                progress(payload)
            except Exception:
                # 诊断上报失败不能覆盖真正的 Agent 异常。
                log.exception("Agent 终态诊断写入后台任务失败")

        try:
            runtime = PiAgentRuntime(
                (cfg.get("agent") or {}).get("pi") or {},
                rpc_url=url, rpc_token=token)
            result = runtime.run(
                prompt, cwd=workspace, tools=tools, abort=abort, progress=progress)
            # 审计由主 RPC 进程产生，而不是信任 Agent 自报；只返回摘要，
            # 不重复携带源码正文 / diff 正文。
            result["audit"] = self.agent_audit(token)
            return result
        except AbortRequested:
            report_terminal("agent_cancelled")
            raise
        except Exception as exc:
            report_terminal("agent_failed", str(exc))
            raise
        finally:
            self.revoke_agent_token(token)

    def _change_svn_preview(self, p):
        return self._change_service().preview(p)

    # ---------- 方案 ----------

    def _suite_list(self, _p):
        return {"suites": self._orch.suites.list()}

    def _suite_detail(self, p):
        ref = _require(p, "suiteId")
        return self._orch.suites.resolve(ref)

    def _suite_create(self, p):
        return self._orch.suites.create(
            name=_require(p, "name"), case_ids=_require(p, "caseIds"),
            description=p.get("description", ""),
            driver_config=p.get("driverConfig"),
            preconditions=p.get("preconditions"))

    def _suite_update(self, p):
        return self._orch.suites.update(
            _to_int(p, "suiteId"), name=p.get("name"),
            case_ids=p.get("caseIds"), description=p.get("description"),
            driver_config=p.get("driverConfig"),
            preconditions=p.get("preconditions"))

    def _suite_delete(self, p):
        sid = _to_int(p, "suiteId")
        self._orch.suites.delete(sid)
        return {"deleted": sid}

    # ---------- 任务 ----------

    def _task_create(self, p):
        return self._orch.create_task({
            "name": _require(p, "name"),
            "suite_id": p.get("suiteId"),
            "case_ids": p.get("caseIds") or [],
            "preconditions": p.get("preconditions"),
            "driver_config": p.get("driverConfig"),
            "force": _bool_param(p, "force", default=False),
            "remote": _bool_param(p, "remote", default=None, allow_none=True),
            "product_id": p.get("productId"),
            "build": p.get("build"), "begin": p.get("begin"), "end": p.get("end"),
            "zentao_task_id": p.get("zentaoTaskId"),
        })

    def _task_run(self, p):
        from testpilot.core.dsl import DriverMode

        task_id = _to_int(p, "taskId")
        run_cfg: dict = {}
        if p.get("mode"):
            try:
                run_cfg["mode"] = DriverMode(str(p["mode"])).value
            except ValueError:
                allowed = ", ".join(mode.value for mode in DriverMode)
                raise RpcError(INVALID_PARAMS,
                               f"非法驱动模式: {p['mode']!r}（可选 {allowed}）") from None
        if p.get("caseRetry") is not None:      # 单次 run 覆盖任务快照的复跑次数
            retry = _to_int(p, "caseRetry")
            if retry < 0 or retry > 3:
                raise RpcError(INVALID_PARAMS, "caseRetry 需为 0..3")
            run_cfg["case_retry"] = retry

        writeback = _bool_param(p, "writeback", default=True)
        if not self._interaction_lock.acquire(blocking=False):
            raise RpcError(INVALID_PARAMS, "自动化探索正在操作界面，请稍后再执行测试")
        try:
            return self._orch.run_task(task_id, run_cfg or None,
                                       writeback=writeback)
        finally:
            self._interaction_lock.release()

    def _task_abort(self, _p):
        stopping = self._orch.abort()
        return {"ok": True, "stopping": bool(stopping)}

    def _task_list(self, _p):
        return {"tasks": self._orch.list_tasks()}

    def _task_detail(self, p):
        return self._orch.task_detail(_to_int(p, "taskId"))

    def _task_sync_pending(self, p):
        # taskId 可选：传了就是行内「单任务补偿」，不传就是工具条上的「补偿待回写任务」
        tid = p.get("taskId")
        return {"results": self._orch.sync_pending_tasks(
            task_id=_to_int(p, "taskId") if tid is not None else None)}

    # ---------- 报告钻取 ----------

    def _run_list(self, p):
        from testpilot.storage.repos import list_runs
        limit = _bounded_int(p, "limit", default=50, max_value=1000)
        if p.get("taskId") is not None:
            from testpilot.storage.repos import _loads
            rows = self._orch.conn.execute(
                "SELECT * FROM test_run WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (_to_int(p, "taskId"), limit)).fetchall()
            runs = []
            for row in rows:
                out = dict(row)
                out["stats"] = _loads(out.pop("stats_json", None), {})
                out["env"] = _loads(out.pop("env_snapshot_json", None), {})
                runs.append(out)
            return {"runs": runs}
        return {"runs": list_runs(self._orch.conn, limit)}

    def _run_detail(self, p):
        from testpilot.engine.stats import case_stability, signature_history
        from testpilot.storage.repos import run_detail
        detail = run_detail(self._orch.conn, _to_int(p, "runId"))
        if detail is None:
            raise RpcError(INVALID_PARAMS, f"run 不存在: {p.get('runId')}")
        # 跨 run 的稳定性按需计算（不落库）：历史 run 的"抖动/必现"会随新 run 变化，
        # 一旦写进 stats 就永远停在当时的结论上。
        detail["stability"] = case_stability(
            self._orch.conn, [c["case_id"] for c in detail["cases"]])
        # 失败签名的历史画像（设计 9.4）：只能对失败用例算，且同样不落库
        for case in detail["cases"]:
            if case.get("failure_signature"):
                case["history"] = signature_history(self._orch.conn,
                                                    case["failure_signature"])
        return detail

    def _run_case_detail(self, p):
        from testpilot.engine.stats import signature_history
        from testpilot.storage.repos import run_case_execs
        case_exec_id = _positive_int_param(p, "caseExecId")
        run_id = _optional_positive_int(p, "runId")
        out = None
        if run_id is not None:
            for row in run_case_execs(self._orch.conn, run_id) or []:
                if row["id"] == case_exec_id:
                    out = dict(row)
                    break
            if out is None:
                raise RpcError(INVALID_PARAMS, f"case_exec 不存在: {case_exec_id}")
        if out is None:
            row = self._orch.conn.execute("SELECT * FROM case_exec WHERE id=?",
                                          (case_exec_id,)).fetchone()
            if row is None:
                raise RpcError(INVALID_PARAMS, f"case_exec 不存在: {case_exec_id}")
            out = dict(row)
        if out.get("failure_signature"):
            out["history"] = signature_history(self._orch.conn,
                                               out["failure_signature"])
        return out

    def _run_step_trace(self, p):
        from testpilot.storage.repos import case_steps
        case_exec_id = _positive_int_param(p, "caseExecId")
        attempt = _optional_positive_int(p, "attempt", max_value=1000)
        row = self._orch.conn.execute(
            "SELECT id FROM case_exec WHERE id=?", (case_exec_id,)).fetchone()
        if row is None:
            raise RpcError(INVALID_PARAMS, f"case_exec 不存在: {case_exec_id}")
        return {"steps": case_steps(self._orch.conn, case_exec_id, attempt)}

    def _run_report(self, p):
        from testpilot.report.reporter import Reporter
        path = Reporter(self._orch.conn, self._orch.cfg).generate(
            _to_int(p, "runId"))
        return {"path": str(path)}

    def _run_compare(self, p):
        """两次 run 的用例级对比（回归收尾：新增失败 / 修复 / 持续失败）。"""
        from testpilot.engine.stats import compare_runs
        try:
            return compare_runs(self._orch.conn, _to_int(p, "baseRunId"),
                                _to_int(p, "targetRunId"))
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _run_shot_diff(self, p):
        """截图差异图（按需生成 + 缓存）—— 设计 14.4「差异模式叠加高亮框」。

        引擎侧预计算（而不是前端叠加）：差异判定要用与验证器**同一套**阈值语义，
        两处各写一份必然漂移；而且像素运算在前端会卡住 UI 线程。
        结果落 ``<shot_root>/<run>/<case>/diff_<step>.png``，命中缓存直接返回。

        before/after 路径以 step_trace 落库值为准（复跑的截图带 attempt 前缀，
        自己拼旧路径会读不到），库里没有时回退到旧命名兜底历史数据。
        """
        from pathlib import Path

        from testpilot.infra.imaging import diff_overlay
        from testpilot.storage.repos import case_steps
        from testpilot.storage.shotstore import ShotStore

        run_id = _positive_int_param(p, "runId")
        case_exec_id = _positive_int_param(p, "caseExecId")
        step_no = _positive_int_param(p, "stepNo")
        attempt = _optional_positive_int(p, "attempt", max_value=1000)
        trace_id = _optional_positive_int(p, "traceId")
        force = _bool_param(p, "force", default=False)

        case_row = self._orch.conn.execute(
            "SELECT run_id FROM case_exec WHERE id=?", (case_exec_id,)).fetchone()
        if case_row is None:
            raise RpcError(INVALID_PARAMS, f"case_exec 不存在: {case_exec_id}")
        actual_run = int(case_row["run_id"])
        if actual_run != run_id:
            raise RpcError(
                INVALID_PARAMS,
                f"caseExecId {case_exec_id} 不属于 run {run_id}（实际 run {actual_run}）")

        store = ShotStore(self._orch.cfg["app"]["screenshot_root"])
        steps = case_steps(self._orch.conn, case_exec_id, attempt)
        step_rows = [s for s in steps if s.get("step_no") == step_no]
        if not step_rows:
            raise RpcError(
                INVALID_PARAMS,
                f"步骤轨迹不存在: caseExecId={case_exec_id} stepNo={step_no}"
                + (f" attempt={attempt}" if attempt is not None else ""))
        if trace_id is not None:
            step_row = next((s for s in step_rows if s["id"] == trace_id), None)
            if step_row is None:
                raise RpcError(
                    INVALID_PARAMS,
                    f"traceId {trace_id} 与当前 case/step/attempt 不匹配")
        else:
            step_row = step_rows[-1]
        attempt_key = str((step_row or {}).get("id") or attempt or "missing")
        rel = f"{run_id}/{case_exec_id}/diff_{attempt_key}_{step_no}.png"
        path = store.resolve(rel)
        meta_rel = f"{run_id}/{case_exec_id}/diff_{attempt_key}_{step_no}.json"
        meta_path = store.resolve(meta_rel)

        if path.exists() and not force:
            import json as _json
            try:
                stats = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:            # 缺 meta 不致命，前端只用图片
                stats = {}
            return {"rel": rel, "cached": True, "stats": stats}

        def _existing(*candidates) -> bytes:
            for cand in candidates:
                if not cand:
                    continue
                full = store.resolve(str(cand))
                if full.exists():
                    return full.read_bytes()
            return b""

        legacy_before = f"{run_id}/{case_exec_id}/{step_no}_before.png"
        legacy_after = f"{run_id}/{case_exec_id}/{step_no}_after.png"
        before_bytes = _existing(
            (step_row or {}).get("screenshot_before_path"), legacy_before)
        after_bytes = _existing(
            (step_row or {}).get("screenshot_path"), legacy_after)
        if not before_bytes or not after_bytes:
            missing = "before" if not before_bytes else "after"
            return {"rel": None, "cached": False, "missing": missing,
                    "stats": {}, "detail": f"缺少 {missing} 截图，无法比对"}

        png, stats = diff_overlay(before_bytes, after_bytes)
        if png is None:
            return {"rel": None, "cached": False, "stats": stats,
                    "detail": "差异计算失败（OpenCV 不可用或截图损坏）"}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        import json as _json
        meta_path.write_text(_json.dumps(stats, ensure_ascii=False),
                             encoding="utf-8")
        return {"rel": rel, "cached": False, "stats": stats}

    # ---------- 知识库 ----------

    def _kb_pending(self, _p):
        from testpilot.storage.repos import kb_pending
        return {"entries": kb_pending(self._orch.conn)}

    def _kb_confirm(self, p):
        entry_id = _positive_int_param(p, "entryId")
        try:
            self._orch.kb.confirm(entry_id, "active")
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e
        return {"entryId": entry_id, "status": "active"}

    def _kb_reject(self, p):
        entry_id = _positive_int_param(p, "entryId")
        try:
            self._orch.kb.confirm(entry_id, "rejected")
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e
        return {"entryId": entry_id, "status": "rejected"}

    def _kb_set_disposition(self, p):
        """人审阶段修正处置策略（设计 3.3）：写入 payload.disposition，命中后按此执行。"""
        try:
            return self._orch.kb.set_disposition(
                _positive_int_param(p, "entryId"),
                str(_require(p, "disposition")),
                **{key: p[key] for key in ("category", "suggestion", "evidence")
                   if key in p})
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _kb_case_ctx_list(self, p):
        """用例上下文（case_ctx）列表：KBReview 的人审维护数据源。"""
        return {"entries": self._orch.kb.list_case_ctx(p.get("caseId"))}

    def _kb_case_ctx_save(self, p):
        """写入/更新用例上下文：字段缺席表示不改，显式空值表示清除。"""
        case_id = _require(p, "caseId")
        payload = p.get("payload")
        if not isinstance(payload, dict):
            payload = {k: p[k] for k in ("scene", "pitfalls", "notes") if k in p}
        if not payload:
            raise RpcError(INVALID_PARAMS, "至少提供 scene/pitfalls/notes 之一")
        try:
            return self._orch.kb.save_case_ctx(case_id, payload)
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _kb_case_ctx_delete(self, p):
        try:
            return self._orch.kb.delete_case_ctx(_require(p, "caseId"))
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _kb_env_compat(self, p):
        """环境通过率矩阵（env_compat）：按方案取各被测版本的历史对照。"""
        return {"entries": self._orch.kb.env_compat_matrix(
            test_app_version=p.get("testAppVersion"),
            suite_id=p.get("suiteId"),
            case_ids=p.get("caseIds"),
            limit=_bounded_int(p, "limit", default=12, max_value=100))}

    # ---------- DSL 审阅 ----------

    def _dsl_review(self, p):
        return self._orch.cases.show(_require(p, "caseId"))

    def _dsl_save(self, p):
        try:
            return self._orch.cases.import_draft(
                _require(p, "text"), p.get("format", "json"),
                case_id=p.get("caseId"))
        except TestPilotError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _dsl_accept(self, p):
        case_id = _require(p, "caseId")
        try:
            if p.get("expectedRevision"):
                current = self._orch.cases.show(case_id)
                if current.get("revision") != p["expectedRevision"]:
                    raise RpcError(INVALID_PARAMS, "用例已变更，请重新加载后审核")
            if p.get("text") is not None:
                self._orch.cases.import_draft(
                    p["text"], p.get("format", "json"), case_id)
            return self._orch.cases.accept(case_id)
        except RpcError:
            raise
        except TestPilotError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _dsl_reject(self, p):
        try:
            return self._orch.cases.reject(_require(p, "caseId"))
        except TestPilotError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from e

    # ---------- 自动化探索（桌面 UI / Agent 共用） ----------

    def _automation_session_id(self, p: dict | None = None) -> str:
        raw = str((p or {}).get("sessionId") or "default").strip()
        if not raw or len(raw) > 64:
            raise RpcError(INVALID_PARAMS, "automation.sessionId 长度需为 1..64")
        if any(not (ch.isalnum() or ch in "._-") for ch in raw):
            raise RpcError(INVALID_PARAMS,
                           "automation.sessionId 只允许字母、数字、点、下划线和连字符")
        return raw

    def _automation_pool(self):
        """返回当前 orchestrator 对应的探索会话池；owner 变化时整体失效。"""
        orch = self._orch
        owner = id(orch)
        if self._automation_owner != owner:
            for session in self._automation_sessions.values():
                try:
                    session.reset()
                except Exception:
                    pass
            self._automation_sessions = {}
            self._automation_owner = owner
        return self._automation_sessions

    def _automation(self, p: dict | None = None):
        """按 sessionId 懒创建探索会话；默认 sessionId=default 保持旧调用兼容。"""
        from testpilot.explore.session import ExplorationSession
        from testpilot.explore.toolbox import ToolBox

        session_id = self._automation_session_id(p)
        pool = self._automation_pool()
        session = pool.get(session_id)
        if session is None:
            if len(pool) >= 16:
                raise RpcError(INVALID_PARAMS, "探索会话过多（上限 16），请先关闭不用的会话")
            orch = self._orch
            # RPC IO 线程会创建一个复用正式驱动的 _io_orchestrator；Explore
            # 必须从根 orchestrator 创建独立驱动，不能 deepcopy 真实 UIA wrapper。
            source = self.orchestrator or orch
            create_drivers = getattr(source, "create_explore_drivers", None)
            drivers = create_drivers() if callable(create_drivers)                 else orch.create_explore_drivers()
            from testpilot.engine.verifier import Verifier
            verifier = Verifier(
                drivers,
                getattr(source, "emulator", getattr(orch, "emulator", None)),
                getattr(source, "llm", getattr(orch, "llm", None)),
                source.cfg.get("driver", {}),
            )
            toolbox = ToolBox(
                drivers,
                emulator=getattr(source, "emulator", getattr(orch, "emulator", None)),
                verifier=verifier,
            )
            session = ExplorationSession(toolbox)
            pool[session_id] = session
        return session

    def _automation_sessions_list(self, _p):
        pool = self._automation_pool()
        return {"sessions": [
            {
                "session_id": session_id,
                "case_id": session.case_id,
                "title": session.title,
                "history_count": len(session.steps),
                "snapshot_id": session.toolbox.computer.snapshot_id,
            }
            for session_id, session in sorted(pool.items())
        ]}

    def _automation_close_session(self, p):
        session_id = self._automation_session_id(p)
        pool = self._automation_pool()
        session = pool.pop(session_id, None)
        if session is not None:
            session.reset()
        self._clear_automation_assets(session_id)
        return {"closed": session is not None, "session_id": session_id}

    def _automation_guard(self) -> None:
        """回归执行期间禁止探索动作，避免两条链路同时抢 UI/输入设备。"""
        root = self.orchestrator
        running = getattr(root, "_in_run", None)
        if running is not None and running.is_set():
            raise RpcError(INVALID_PARAMS, "测试任务执行中，自动化探索暂不可操作界面")

    def _automation_tools(self, _p):
        session = self._automation(_p)
        return {"session_id": self._automation_session_id(_p), "tools": session.toolbox.list_tools(),
                "drivers": sorted(session.toolbox.drivers),
                "emulator": session.toolbox.emulator is not None,
                "snapshot_id": session.toolbox.computer.snapshot_id}

    def _automation_invoke(self, p):
        self._automation_guard()
        if not self._interaction_lock.acquire(blocking=False):
            raise RpcError(INVALID_PARAMS, "测试任务正在执行，自动化探索暂不可操作界面")
        try:
            tool = str(_require(p, "tool"))
            args = p.get("args") or {}
            if not isinstance(args, dict):
                raise RpcError(INVALID_PARAMS, "automation.invoke 的 args 需为对象")
            session = self._automation(p)
            known = {item["name"] for item in session.toolbox.list_tools()}
            if tool not in known:
                raise RpcError(INVALID_PARAMS, f"当前环境不可用的自动化工具: {tool}")
            result = session.call(tool, args)
            result = self._automation_transport_result(p, result)
            return {"tool": tool, "result": result,
                    "history_count": len(session.steps)}
        except TestPilotError:
            # ExplorationSession 已经把失败调用写入 history；保留原业务异常语义。
            raise
        finally:
            self._interaction_lock.release()

    def _automation_transport_result(self, p: dict, result):
        """大截图改为短期 asset 引用；小图保持 inline 兼容旧客户端。"""
        if not isinstance(result, dict):
            return result
        encoded = result.get("png_base64")
        if not isinstance(encoded, str) or not encoded:
            return result
        try:
            png = base64.b64decode(encoded)
        except Exception:
            return result
        out = dict(result)
        out["screenshot_bytes"] = len(png)
        if len(png) <= _AUTOMATION_INLINE_PNG_MAX:
            out["screenshot_transport"] = "inline"
            return out

        session_id = self._automation_session_id(p)
        root = Path(self._orch.cfg["app"]["screenshot_root"]).expanduser().resolve()
        folder = root / "_explore" / session_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            snapshot_id = int(out.get("snapshot_id") or 0)
            path = folder / f"snapshot_{snapshot_id}_{secrets.token_hex(4)}.png"
            path.write_bytes(png)
            out.pop("png_base64", None)
            out["asset_path"] = str(path.resolve())
            out["screenshot_transport"] = "asset"
            self._cleanup_automation_assets(folder)
        except OSError as e:
            log.warning(f"探索截图临时资产写入失败，回退 inline: {e}")
            out["screenshot_transport"] = "inline"
        return out

    def _clear_automation_assets(self, session_id: str) -> None:
        root = Path(self._orch.cfg["app"]["screenshot_root"]).expanduser().resolve()
        folder = root / "_explore" / session_id
        try:
            if not folder.exists():
                return
            for path in folder.glob("*.png"):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                folder.rmdir()
            except OSError:
                pass
        except OSError:
            pass

    @staticmethod
    def _cleanup_automation_assets(folder: Path) -> None:
        try:
            files = sorted(folder.glob("*.png"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
            for old in files[_AUTOMATION_ASSET_KEEP:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass
    def _automation_history(self, _p):
        session = self._automation(_p)
        return {"session": session.to_dict(),
                "snapshot_id": session.toolbox.computer.snapshot_id}

    def _automation_draft(self, p):
        session = self._automation(p)
        if p.get("caseId") is not None:
            session.case_id = str(p["caseId"]).strip() or "TC-EXPLORE"
        if p.get("title") is not None:
            session.title = str(p["title"]).strip() or "探索路径"
        if p.get("notes") is not None:
            session.notes = str(p["notes"])
        if p.get("preconditions") is not None:
            if not isinstance(p["preconditions"], list):
                raise RpcError(INVALID_PARAMS, "preconditions 需为字符串数组")
            session.preconditions = [str(v) for v in p["preconditions"] if str(v).strip()]
        case = session.to_case()
        return {"case": case, "text": session.to_draft_text(p.get("format", "json")),
                "history_count": len(session.steps)}

    def _automation_remove_step(self, p):
        session = self._automation(p)
        removed = session.remove_step(_to_int(p, "index"))
        return {"removed": removed.to_dict(), "history_count": len(session.steps)}

    def _automation_update_step(self, p):
        session = self._automation(p)
        args = p.get("args")
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "automation.updateStep 的 args 需为对象")
        step = session.update_step(_to_int(p, "index"), args)
        return {"step": step.to_dict(), "history_count": len(session.steps)}

    def _automation_rerun_step(self, p):
        self._automation_guard()
        if not self._interaction_lock.acquire(blocking=False):
            raise RpcError(INVALID_PARAMS, "测试任务正在执行，自动化探索暂不可操作界面")
        try:
            args = p.get("args")
            if args is not None and not isinstance(args, dict):
                raise RpcError(INVALID_PARAMS, "automation.rerunStep 的 args 需为对象")
            index = _to_int(p, "index")
            session = self._automation(p)
            result = session.rerun_step(index, args)
            result = self._automation_transport_result(p, result)
            return {"rerun_of": index, "result": result,
                    "history_count": len(session.steps)}
        finally:
            self._interaction_lock.release()

    def _automation_move_step(self, p):
        session = self._automation(p)
        steps = session.move_step(_to_int(p, "index"), _to_int(p, "toIndex"))
        return {"steps": [step.to_dict() for step in steps],
                "history_count": len(steps)}

    def _automation_restore(self, p):
        from testpilot.explore.session import ExplorationSession

        data = p.get("session")
        if not isinstance(data, dict):
            raise RpcError(INVALID_PARAMS, "automation.restore 的 session 需为对象")
        raw_steps = data.get("steps") or []
        if not isinstance(raw_steps, list):
            raise RpcError(INVALID_PARAMS, "automation.restore.session.steps 需为数组")
        if len(raw_steps) > 2000:
            raise RpcError(INVALID_PARAMS, "探索会话步骤过多（上限 2000）")

        current = self._automation(p)
        # 历史可以恢复，短生命周期 UI 句柄绝不能恢复；恢复后必须重新 observe/query。
        current.toolbox.reset_session()
        restored = ExplorationSession.from_dict(data, toolbox=current.toolbox)
        self._automation_pool()[self._automation_session_id(p)] = restored
        return {"session": restored.to_dict(),
                "history_count": len(restored.steps),
                "snapshot_id": restored.toolbox.computer.snapshot_id}

    def _automation_reset(self, p):
        session = self._automation(p)
        session.reset()
        session.case_id = str(p.get("caseId") or "TC-EXPLORE")
        session.title = str(p.get("title") or "探索路径")
        session.notes = str(p.get("notes") or "")
        self._clear_automation_assets(self._automation_session_id(p))
        return {"ok": True, "snapshot_id": session.toolbox.computer.snapshot_id}

    # ---------- Agent Tool Host（所有 Agent 工具都在主 RPC 进程执行） ----------

    def issue_agent_token(self, workspace: str | Path) -> str:
        """签发一次 Agent 会话的最小能力 Token。

        Token 只能访问 _AGENT_RPC_METHODS，并绑定一个 canonical workspace；
        Pi 子进程拿不到桌面主 RPC Token。
        """
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise RpcError(INVALID_PARAMS, f"Agent 工作区不存在: {root}")
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_hex(8)
        with self._agent_tokens_lock:
            self._agent_tokens[token] = {
                "workspace": str(root),
                "session_id": session_id,
            }
            self._agent_audits[session_id] = []
        return token

    def agent_audit(self, token: str) -> list[dict]:
        """返回当前 Agent capability 会话的结构化工具审计快照。"""
        with self._agent_tokens_lock:
            scope = self._agent_tokens.get(str(token))
            if not scope:
                return []
            session_id = str(scope.get("session_id") or "")
            return [dict(item) for item in self._agent_audits.get(session_id, [])]

    def revoke_agent_token(self, token: str) -> None:
        with self._agent_tokens_lock:
            scope = self._agent_tokens.pop(str(token), None)
            if scope:
                self._agent_audits.pop(str(scope.get("session_id") or ""), None)

    @staticmethod
    def _agent_audit_params(params: dict) -> dict:
        """只记录输入摘要；capability scope / workspace 以及大正文都不进入审计。"""
        out = {}
        for key, value in (params or {}).items():
            if key in ("_agentScope", "workspace"):
                continue
            if isinstance(value, str):
                out[key] = value if len(value) <= 200 else value[:197] + "..."
            elif isinstance(value, (bool, int, float)) or value is None:
                out[key] = value
            elif isinstance(value, (list, tuple)):
                items = list(value)[:20]
                out[key] = [
                    (item if isinstance(item, (bool, int, float)) or item is None
                     else str(item)[:120])
                    for item in items
                ]
                if len(value) > len(items):
                    out[f"{key}_truncated"] = len(value) - len(items)
            else:
                out[key] = str(value)[:200]
        return out

    @staticmethod
    def _agent_audit_result(method: str, result) -> dict:
        """把工具返回压缩为元数据，避免把源码 / diff / 用例正文复制进轨迹。"""
        if not isinstance(result, dict):
            return {"type": type(result).__name__}
        keys = {
            "agent.fs.read": ("path", "offset", "lines", "total_lines"),
            "agent.fs.find": ("count", "truncated"),
            "agent.fs.grep": ("count", "scanned_files", "truncated"),
            "agent.svn.status": ("count",),
            "agent.svn.diff": ("chars", "truncated", "agent_limit_chars"),
            "agent.svn.log": ("count",),
            "agent.case.search": ("count", "truncated"),
            "agent.case.detail": ("case_id", "title", "status", "sync_status"),
        }.get(method, ())
        out = {key: result.get(key) for key in keys if key in result}
        if method == "agent.svn.diff":
            out["path_count"] = len(result.get("paths") or [])
        elif method == "agent.case.detail":
            out["step_count"] = len(result.get("steps") or [])
        return out

    def _record_agent_audit(self, scope: dict, method: str, params: dict, *,
                            result=None, error: str | None = None,
                            elapsed_ms: int = 0) -> None:
        session_id = str((scope or {}).get("session_id") or "")
        if not session_id:
            return
        with self._agent_tokens_lock:
            audit = self._agent_audits.get(session_id)
            if audit is None:
                return
            seq = int(audit[-1]["seq"]) + 1 if audit else 1
            entry = {
                "seq": seq,
                "method": method,
                "params": self._agent_audit_params(params),
                "ok": error is None,
                "elapsed_ms": max(0, int(elapsed_ms)),
            }
            if error is None:
                entry["result"] = self._agent_audit_result(method, result)
            else:
                entry["error"] = str(error)[:500]
            audit.append(entry)
            if len(audit) > _AGENT_AUDIT_LIMIT:
                del audit[:-_AGENT_AUDIT_LIMIT]

    def _agent_scope(self, p: dict) -> dict:
        scope = p.get("_agentScope")
        if not isinstance(scope, dict) or not scope.get("workspace"):
            raise RpcError(INVALID_PARAMS, "Agent 工具必须通过受限 Agent RPC 会话调用")
        return scope

    def _agent_workspace(self, p: dict) -> Path:
        return Path(self._agent_scope(p)["workspace"]).resolve()

    def _agent_path(self, p: dict, value, *, must_file=False) -> Path:
        root = self._agent_workspace(p)
        raw = Path(str(value or "").strip())
        path = (raw if raw.is_absolute() else root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RpcError(INVALID_PARAMS, f"Agent 路径越出工作区: {value!r}") from exc
        if must_file and not path.is_file():
            raise RpcError(INVALID_PARAMS, f"文件不存在: {value!r}")
        return path

    def _agent_fs_read(self, p):
        path = self._agent_path(p, _require(p, "path"), must_file=True)
        size = path.stat().st_size
        if size > 2 * 1024 * 1024:
            raise RpcError(INVALID_PARAMS, f"Agent 单文件读取上限 2MiB: {path.name}")
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            raise RpcError(INVALID_PARAMS, f"不允许把二进制文件送给 Agent: {path.name}")
        lines = raw.decode("utf-8", "replace").splitlines()
        offset = _bounded_int(p, "offset", default=1, max_value=10_000_000)
        limit = _bounded_int(p, "limit", default=300, max_value=2000)
        selected = lines[offset - 1: offset - 1 + limit]
        log.info(f"agent.fs.read {path} lines={offset}..{offset + len(selected) - 1}")
        return {
            "path": path.relative_to(self._agent_workspace(p)).as_posix(),
            "offset": offset, "lines": len(selected), "total_lines": len(lines),
            "content": "\n".join(f"{offset + i}: {line}" for i, line in enumerate(selected)),
        }

    def _agent_fs_find(self, p):
        root = self._agent_workspace(p)
        pattern = str(_require(p, "pattern")).strip().replace("\\", "/")
        if len(pattern) > 300 or pattern.startswith("/") or ".." in Path(pattern).parts:
            raise RpcError(INVALID_PARAMS, "Agent find pattern 非法")
        limit = _bounded_int(p, "limit", default=200, max_value=500)
        out = []
        for path in root.glob(pattern):
            try:
                rel = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part in (".svn", ".git", "node_modules") for part in rel.parts):
                continue
            out.append({"path": rel.as_posix(), "type": "dir" if path.is_dir() else "file"})
            if len(out) >= limit:
                break
        log.info(f"agent.fs.find workspace={root} pattern={pattern!r} count={len(out)}")
        return {"items": out, "count": len(out), "truncated": len(out) >= limit}

    def _agent_fs_grep(self, p):
        root = self._agent_workspace(p)
        query = str(_require(p, "query"))
        if len(query) > 500:
            raise RpcError(INVALID_PARAMS, "Agent grep 查询过长")
        glob = str(p.get("glob") or "**/*").replace("\\", "/")
        if glob.startswith("/") or ".." in Path(glob).parts:
            raise RpcError(INVALID_PARAMS, "Agent grep glob 非法")
        limit = _bounded_int(p, "limit", default=100, max_value=300)
        use_regex = _bool_param(p, "regex", default=False)
        try:
            pattern = re.compile(query, re.IGNORECASE) if use_regex else None
        except re.error as exc:
            raise RpcError(INVALID_PARAMS, f"Agent grep 正则非法: {exc}") from exc
        needle = query.lower()
        out, scanned = [], 0
        for path in root.glob(glob):
            if len(out) >= limit or scanned >= 2000:
                break
            if not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part in (".svn", ".git", "node_modules") for part in rel.parts):
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            scanned += 1
            if b"\x00" in raw[:4096]:
                continue
            for no, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                matched = bool(pattern.search(line)) if pattern else needle in line.lower()
                if matched:
                    out.append({"path": rel.as_posix(), "line": no, "text": line[:500]})
                    if len(out) >= limit:
                        break
        log.info(f"agent.fs.grep workspace={root} query={query[:80]!r} hits={len(out)}")
        return {"matches": out, "count": len(out), "scanned_files": scanned,
                "truncated": len(out) >= limit}

    def _agent_svn_status(self, p):
        from testpilot.adapters.svn_adapter import SvnAdapter
        root = self._agent_workspace(p)
        out = SvnAdapter(self._orch.cfg.get("svn", {})).status(root)
        log.info(f"agent.svn.status workspace={root} count={len(out)}")
        return {"items": out, "count": len(out)}

    def _agent_svn_diff(self, p):
        from testpilot.adapters.svn_adapter import SvnAdapter
        root = self._agent_workspace(p)
        paths = p.get("paths")
        if paths is not None and (not isinstance(paths, list) or len(paths) > 50):
            raise RpcError(INVALID_PARAMS, "paths 需为最多 50 条路径")
        try:
            requested = int(p.get("max_chars") or 120_000)
        except (TypeError, ValueError):
            raise RpcError(INVALID_PARAMS, "max_chars 需为整数") from None
        max_chars = max(10_000, min(requested, 200_000))
        out = SvnAdapter(self._orch.cfg.get("svn", {})).diff(
            root, from_revision=p.get("from_revision"), to_revision=p.get("to_revision"),
            paths=paths, max_chars=max_chars)
        out["agent_limit_chars"] = max_chars
        log.info(f"agent.svn.diff workspace={root} chars={out.get('chars')}")
        return out

    def _agent_svn_log(self, p):
        from testpilot.adapters.svn_adapter import SvnAdapter
        root = self._agent_workspace(p)
        out = SvnAdapter(self._orch.cfg.get("svn", {})).log(
            root, from_revision=p.get("from_revision"), to_revision=p.get("to_revision"),
            limit=_bounded_int(p, "limit", default=20, max_value=200))
        return {"items": out, "count": len(out)}

    def _agent_case_search(self, p):
        from testpilot.storage import repos
        query = str(_require(p, "query")).strip().lower()
        limit = _bounded_int(p, "limit", default=20, max_value=50)
        out = []
        for row in repos.list_cases(self._orch.conn):
            raw = row.get("raw") or {}
            hay = " ".join([
                str(row.get("case_id") or ""), str(row.get("title") or ""),
                str(raw.get("steps_text") or ""), str(row.get("notes") or ""),
            ]).lower()
            if query not in hay:
                continue
            out.append({
                "case_id": row["case_id"], "zentao_case_id": row.get("zentao_case_id"),
                "title": row.get("title"), "status": row.get("status"),
                "sync_status": row.get("sync_status"), "step_count": len(row.get("steps") or []),
                "source_text": str(raw.get("steps_text") or "")[:1200],
            })
            if len(out) >= limit:
                break
        return {"cases": out, "count": len(out), "truncated": len(out) >= limit}

    def _agent_case_detail(self, p):
        case_id = str(_require(p, "case_id")).strip()
        detail = self._orch.cases.show(case_id)
        # 控制 Agent 上下文体积；完整图片/轨迹不通过此工具暴露。
        if len(str(detail.get("source_text") or "")) > 8000:
            detail["source_text"] = str(detail["source_text"])[:8000] + "\n...[truncated]"
        return detail

    # ---------- 配置（设置页） ----------

    def _config_service(self):
        from testpilot.config.service import ConfigService
        return ConfigService(self._orch.cfg)

    def _config_describe(self, _p):
        return self._config_service().describe()

    def _config_apply(self, p):
        values = p.get("values") or {}
        if not isinstance(values, dict):
            raise RpcError(INVALID_PARAMS, "values 需为对象")
        try:
            return self._config_service().apply(values)
        except ValueError as e:               # ConfigError（面向用户可读）
            raise RpcError(INVALID_PARAMS, str(e)) from e

    def _config_reset(self, _p):
        try:
            return self._config_service().apply({}, reset=True)
        except ValueError as e:
            raise RpcError(INTERNAL_ERROR, str(e)) from e

    def _config_override_text(self, _p):
        """覆盖层原文（设置页的「高级」页签直接展示 YAML，便于核对实际写入内容）。"""
        svc = self._config_service()
        if not svc.path.exists():
            return {"path": str(svc.path), "exists": False, "text": ""}
        return {"path": str(svc.path), "exists": True,
                "text": svc.path.read_text(encoding="utf-8")}

    # ---------- 事件推送 ----------

    def _on_event(self, evt) -> None:
        data = evt.to_dict()          # {"event": ..., "payload": ...}
        self.publish(data["event"], data["payload"])

    def publish(self, event: str, payload: dict) -> None:
        """线程安全地把事件推给所有订阅的 WebSocket（可能来自执行线程）。"""
        if not self._subs or self._loop is None:
            return
        # default=str 与响应路径同口径：payload 混入非 JSON 原生类型时
        # 不能让整条事件对所有订阅者静默丢失
        msg = json.dumps({"jsonrpc": "2.0", "method": event,
                          "params": payload}, ensure_ascii=False, default=str)
        for ws in list(self._subs):
            try:
                self._loop.call_soon_threadsafe(self._spawn_send, ws, msg)
            except Exception:      # 连接已失效
                self._subs.discard(ws)

    def _spawn_send(self, ws, msg: str) -> None:
        asyncio.ensure_future(self._safe_send(ws, msg))

    async def _safe_send(self, ws, msg: str) -> None:
        lock = self._send_locks.get(ws)
        try:
            if lock is not None:
                async with lock:
                    await ws.send(msg)
            else:
                await ws.send(msg)
        except Exception:
            self._subs.discard(ws)

    # ---------- 服务循环 ----------

    def _agent_connection_scope(self, supplied: str = "",
                                request_path: str = "") -> dict | None:
        """Resolve a short-lived Agent capability from header or WS query.

        Desktop clients keep using the long-lived Bearer token. Pi extensions use the
        browser-compatible WebSocket API, which cannot set custom headers, so the Agent
        token is also accepted as the agent_token query on this loopback-only endpoint.
        """
        token = ""
        prefix = "Bearer "
        if isinstance(supplied, str) and supplied.startswith(prefix):
            token = supplied[len(prefix):]
        if not token and request_path:
            try:
                values = parse_qs(urlsplit(str(request_path)).query).get("agent_token") or []
                token = str(values[0]) if values else ""
            except (TypeError, ValueError):
                token = ""
        if not token:
            return None
        with self._agent_tokens_lock:
            scope = self._agent_tokens.get(token)
            return dict(scope) if scope is not None else None

    async def serve(self, host: str, port: int) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async def handler(ws, path=None):     # 兼容 websockets 新旧 handler 签名
            request = getattr(ws, "request", None)
            headers = getattr(request, "headers", None) or getattr(ws, "request_headers", {})
            supplied = headers.get("Authorization", "") if headers is not None else ""
            request_path = (getattr(request, "path", None) or path
                            or getattr(ws, "path", "") or "")
            agent_scope = self._agent_connection_scope(supplied, request_path)
            if agent_scope is None:
                self._subs.add(ws)
            lock = asyncio.Lock()
            self._send_locks[ws] = lock
            # 每帧独立处理：task.run 可能执行数小时，若整条连接串行消费，
            # 运行期间 task.abort / health / shutdown 将永远排队到 run 结束才生效。
            pending: set[asyncio.Task] = set()

            async def respond(raw) -> None:
                resp = await self._handle_raw(raw, agent_scope=agent_scope)
                if resp is None:              # notification：不回包
                    return
                async with lock:
                    await ws.send(json.dumps(resp, ensure_ascii=False, default=str))

            log.info(f"壳已连接: {getattr(ws, 'remote_address', '?')}")
            try:
                async for raw in ws:
                    t = asyncio.ensure_future(respond(raw))
                    pending.add(t)
                    t.add_done_callback(pending.discard)
            except Exception as e:            # 连接异常仅记录
                log.info(f"壳连接结束: {e}")
            finally:
                # 等在途响应写完再关连接，避免壳收到截断的应答
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self._subs.discard(ws)
                self._send_locks.pop(ws, None)

        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("RPC 仅允许监听本机回环地址")

        async def authorize(connection, request):
            headers = getattr(request, "headers", request)
            origin = headers.get("Origin")
            supplied = headers.get("Authorization", "")
            request_path = (getattr(request, "path", "") or
                            (connection if isinstance(connection, str) else ""))
            allowed = origin in (None, "tauri://localhost", "http://tauri.localhost",
                                 "https://tauri.localhost")
            main_ok = hmac.compare_digest(supplied, f"Bearer {self.token}")
            agent_ok = self._agent_connection_scope(supplied, request_path) is not None
            if allowed and (main_ok or agent_ok):
                return None
            if hasattr(request, "headers"):
                return connection.respond(403, "RPC authorization required")
            return (403, [], b"RPC authorization required")

        async with websockets.serve(handler, host, port, process_request=authorize):
            log.info(f"RPC 服务已启动: ws://{host}:{port}")
            await self._stop.wait()
        log.info("RPC 服务已停止")

    async def _handle_raw(self, raw, agent_scope: dict | None = None) -> dict | None:
        req = {}
        try:
            req = json.loads(raw)
        except ValueError:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": PARSE_ERROR, "message": "parse error"}}
        if not isinstance(req, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": PARSE_ERROR, "message": "请求必须是对象"}}
        req_id = req.get("id")
        is_notification = "id" not in req     # JSON-RPC 2.0：无 id 的请求不回包
        method = str(req.get("method", ""))
        params = req.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):    # positional 数组本服务不支持，明确拒绝
            # 通知没有 id：即使参数非法也不回包（JSON-RPC 2.0 规范）
            if is_notification:
                return None
            return self._err(req_id, INVALID_PARAMS,
                             "params 需为对象（不支持位置参数）")
        if agent_scope is not None:
            if method not in _AGENT_RPC_METHODS:
                return None if is_notification else self._err(
                    req_id, METHOD_NOT_FOUND, "Agent RPC capability 不允许该方法")
            params = dict(params)
            params["_agentScope"] = agent_scope
        reserved = False
        audit_started = time.monotonic() if agent_scope is not None else None
        audit_result = None
        audit_error = None
        try:
            if method in _INLINE:
                result = self.dispatch(method, params)
            else:
                # 除 abort/shutdown 外全部派发到专用线程：事件循环线程绝不直接
                # 触碰 sqlite 连接（否则读请求会与运行中的 task.run 并发使用
                # 同一连接，还会在读阻塞时卡住 abort/事件推送）。
                if method in _LONG:
                    if self._run_reserved.is_set():
                        raise RpcError(INVALID_PARAMS, "已有任务正在执行")
                    self._run_reserved.set()
                    reserved = True
                pool = self._run_executor if method in _LONG else self._executor
                result = await asyncio.get_running_loop().run_in_executor(
                    pool, self.dispatch if method in _LONG else self._dispatch_io,
                    method, params)
            audit_result = result
            return None if is_notification else \
                {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as e:
            audit_error = e.message
            return None if is_notification else self._err(req_id, e.code,
                                                          e.message, e.data)
        except Exception as e:
            audit_error = str(e)
            log.exception(f"RPC 方法异常: {method}")
            return None if is_notification else \
                self._err(req_id, INTERNAL_ERROR, str(e))
        finally:
            if (agent_scope is not None and audit_started is not None
                    and method in _AGENT_RPC_METHODS):
                self._record_agent_audit(
                    agent_scope, method, params, result=audit_result,
                    error=audit_error,
                    elapsed_ms=round((time.monotonic() - audit_started) * 1000),
                )
            if reserved:
                self._run_reserved.clear()

    @staticmethod
    def _err(req_id, code: int, message: str, data=None) -> dict:
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": req_id, "error": err}

    def close(self) -> None:
        self._jobs.close()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        # 正常退出路径由 _drain_then_stop 先排空；这里只是兜底回收线程资源
        if self._io_orchestrator is not None:
            self._io_orchestrator.close()
        if self._case_orchestrator is not None:
            self._case_orchestrator.close()
        for session in self._automation_sessions.values():
            try:
                session.reset()
            except Exception:
                pass
        self._automation_sessions = {}
        self._automation_owner = None
        with self._agent_tokens_lock:
            self._agent_tokens.clear()
        self._executor.shutdown(wait=False)
        self._run_executor.shutdown(wait=False)


def _require(params: dict, key: str):
    value = params.get(key)
    if value is None or value == "":
        raise RpcError(INVALID_PARAMS, f"缺少必填参数: {key}")
    return value


def _bool_param(params: dict, key: str, *, default=False,
                allow_none: bool = False):
    """Strict JSON boolean parameter.

    Strings such as "false" are truthy in Python and can accidentally enable
    external side effects. Only actual JSON booleans are accepted. For tri-state
    options such as task.remote, an omitted/null value can remain None so the
    application-level configuration still supplies the default.
    """
    if key not in params or params.get(key) is None:
        if allow_none:
            return None
        return bool(default)
    value = params[key]
    if not isinstance(value, bool):
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需为布尔值 true/false（得到 {value!r}）")
    return value


def _parse_int_value(value, key: str) -> int:
    """Parse one integer value without Python's bool/fraction truncation traps."""
    if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()):
        raise RpcError(INVALID_PARAMS, f"参数 {key} 需为整数（得到 {value!r}）")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需为整数（得到 {value!r}）") from None


def _to_int(params: dict, key: str) -> int:
    """整型参数统一入口：类型/取值错误一律 INVALID_PARAMS。"""
    return _parse_int_value(_require(params, key), key)


def _positive_int_param(params: dict, key: str) -> int:
    parsed = _to_int(params, key)
    if parsed <= 0:
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需为正整数（得到 {parsed}）")
    return parsed


def _optional_positive_int(params: dict, key: str,
                           max_value: int | None = None) -> int | None:
    if key not in params or params.get(key) in (None, ""):
        return None
    parsed = _parse_int_value(params[key], key)
    if parsed <= 0:
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需为正整数（得到 {parsed}）")
    if max_value is not None and parsed > max_value:
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需 <= {max_value}（得到 {parsed}）")
    return parsed


def _bounded_int(params: dict, key: str, *, default: int,
                 min_value: int = 1, max_value: int | None = None) -> int:
    """Optional bounded integer RPC parameter.

    Numeric strings remain accepted for compatibility. Booleans, fractional
    floats, zero/negative values and malformed text are rejected as
    INVALID_PARAMS instead of leaking ValueError or SQLite LIMIT=-1 semantics.
    Values above max_value are clamped to preserve the historical query-limit
    behavior while keeping response size bounded.
    """
    value = params.get(key, default)
    if value is None or value == "":
        value = default
    parsed = _parse_int_value(value, key)
    if parsed < min_value:
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需 >= {min_value}（得到 {parsed}）")
    if max_value is not None:
        parsed = min(parsed, max_value)
    return parsed


def _to_int_list(params: dict, key: str) -> list[int]:
    """整型列表参数：壳传数组；容忍 "1,2" 形式的字符串（历史调用方）。"""
    value = _require(params, key)
    items = value if isinstance(value, (list, tuple)) else \
        [p for p in str(value).split(",") if p.strip()]
    out: list[int] = []
    for item in items:
        try:
            out.append(_parse_int_value(item, key))
        except RpcError:
            raise RpcError(INVALID_PARAMS,
                           f"参数 {key} 含非整数项: {item!r}") from None
    return out

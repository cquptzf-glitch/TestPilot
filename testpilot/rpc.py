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
import json
import logging
import os
import secrets
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait

from testpilot.infra.jobs import CaseJobs

from testpilot.core.errors import (AbortRequested, INTERNAL_ERROR, INVALID_PARAMS, PARSE_ERROR,
                                   RpcError, TestPilotError, method_not_found)

log = logging.getLogger("testpilot.rpc")

# 在事件循环线程直接执行的方法（不触 DB、不做 IO）：必须保持低延迟，
# 让 task.run 长时间执行期间 abort/shutdown 仍能即时生效。
_INLINE = frozenset({"shutdown", "task.abort", "job.start", "job.status", "job.cancel"})

# 长任务方法单独走一个执行器：读写类请求不能排在数小时的 task.run 后面
_LONG = frozenset({"task.run"})

_DRAIN_TIMEOUT_S = 15.0     # shutdown 时等待执行中 task.run 收尾的上限


class RpcService:
    def __init__(self, orchestrator=None, *, token=None):
        self.token = token or os.environ.get("TESTPILOT_RPC_TOKEN") or secrets.token_urlsafe(32)
        self.orchestrator = orchestrator
        self._local = threading.local()
        self._io_orchestrator = None
        self._case_orchestrator = None
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
            "suite.list", "suite.detail", "suite.create", "suite.update",
            "suite.delete", "task.create", "task.run", "task.abort", "task.list",
            "task.detail", "task.syncPending", "task.reconcile", "run.list", "run.detail",
            "run.caseDetail", "run.stepTrace", "run.report", "run.compare",
            "run.shotDiff",
            "kb.pending", "kb.confirm", "kb.reject", "kb.setDisposition",
            "kb.caseCtxList", "kb.caseCtxSave", "kb.caseCtxDelete",
            "kb.envCompat",
            "dsl.review", "dsl.save", "dsl.accept", "dsl.reject",
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
        abort = bool(p.get("abortRunning", True))
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
        return self._orch.cases.pull(ids, parse=bool(p.get("parse")))

    def _case_sync(self, p):
        return self._orch.cases.sync(p.get("productId"), parse=bool(p.get("parse")))

    def _case_list(self, p):
        return {"cases": self._orch.cases.list_cases(
            status=p.get("status"), sync_status=p.get("syncStatus"))}

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
            if (not isinstance(target, dict) or set(target) - {"exe", "title_re"}
                    or not target or any(not isinstance(v, str) or not v.strip()
                                         or len(v) > 2048 for v in target.values())):
                raise RpcError(INVALID_PARAMS, "appTarget 需为包含 exe 或 title_re 的非空字符串对象")
        scopes = sum(("caseIds" in p, bool(p.get("allStale")), bool(p.get("caseId"))))
        if scopes != 1:
            raise RpcError(INVALID_PARAMS, "指定 caseId、caseIds 或 allStale 中的一项")
        if "caseIds" in p:
            ids = p["caseIds"]
            if (not isinstance(ids, list) or not ids or len(ids) > 500
                    or any(not isinstance(cid, str) or not cid.strip() for cid in ids)):
                raise RpcError(INVALID_PARAMS, "caseIds 必须为 1–500 条非空用例 ID 字符串")
            return self._orch.cases.parse_many(ids, app_target=target)
        if p.get("allStale"):
            return self._orch.cases.parse_many(None, only_stale=True, app_target=target)
        return self._orch.cases.parse(_require(p, "caseId"), app_target=target)

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
            "force": bool(p.get("force")),
            "remote": p.get("remote"),
            "product_id": p.get("productId"),
            "build": p.get("build"), "begin": p.get("begin"), "end": p.get("end"),
            "zentao_task_id": p.get("zentaoTaskId"),
        })

    def _task_run(self, p):
        task_id = _to_int(p, "taskId")
        run_cfg: dict = {}
        if p.get("mode"):
            run_cfg["mode"] = p["mode"]
        if p.get("caseRetry") is not None:      # 单次 run 覆盖任务快照的复跑次数
            run_cfg["case_retry"] = _to_int(p, "caseRetry")
        return self._orch.run_task(task_id, run_cfg or None,
                                   writeback=bool(p.get("writeback", True)))

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
        if p.get("taskId") is not None:
            from testpilot.storage.repos import _loads
            rows = self._orch.conn.execute(
                "SELECT * FROM test_run WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (_to_int(p, "taskId"), min(1000, int(p.get("limit", 50))))).fetchall()
            runs = []
            for row in rows:
                out = dict(row)
                out["stats"] = _loads(out.pop("stats_json", None), {})
                out["env"] = _loads(out.pop("env_snapshot_json", None), {})
                runs.append(out)
            return {"runs": runs}
        return {"runs": list_runs(self._orch.conn, int(p.get("limit", 50)))}

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
        case_exec_id = _to_int(p, "caseExecId")
        run_id = p.get("runId")
        out = None
        if run_id is not None:
            for row in run_case_execs(self._orch.conn, int(run_id)) or []:
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
        attempt = p.get("attempt")
        return {"steps": case_steps(self._orch.conn,
                                    _to_int(p, "caseExecId"),
                                    int(attempt) if attempt else None)}

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

        run_id = _to_int(p, "runId")
        case_exec_id = _to_int(p, "caseExecId")
        step_no = _to_int(p, "stepNo")
        store = ShotStore(self._orch.cfg["app"]["screenshot_root"])

        attempt = int(p.get("attempt") or 0) or None
        steps = case_steps(self._orch.conn, case_exec_id, attempt)
        step_rows = [s for s in steps if s.get("step_no") == step_no]
        step_row = next((s for s in step_rows if s["id"] == p.get("traceId")), None)
        if step_row is None and step_rows:
            step_row = step_rows[-1]
        attempt_key = str((step_row or {}).get("id") or attempt or "missing")
        rel = f"{run_id}/{case_exec_id}/diff_{attempt_key}_{step_no}.png"
        path = store.resolve(rel)
        meta_rel = f"{run_id}/{case_exec_id}/diff_{attempt_key}_{step_no}.json"
        meta_path = store.resolve(meta_rel)

        if path.exists() and not p.get("force"):
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
        entry_id = _to_int(p, "entryId")
        self._orch.kb.confirm(entry_id, "active")
        return {"entryId": entry_id, "status": "active"}

    def _kb_reject(self, p):
        entry_id = _to_int(p, "entryId")
        self._orch.kb.confirm(entry_id, "rejected")
        return {"entryId": entry_id, "status": "rejected"}

    def _kb_set_disposition(self, p):
        """人审阶段修正处置策略（设计 3.3）：写入 payload.disposition，命中后按此执行。"""
        return self._orch.kb.set_disposition(
            _to_int(p, "entryId"), str(_require(p, "disposition")),
            **{key: p[key] for key in ("category", "suggestion", "evidence") if key in p})

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
        return self._orch.kb.save_case_ctx(case_id, payload)

    def _kb_case_ctx_delete(self, p):
        return self._orch.kb.delete_case_ctx(_require(p, "caseId"))

    def _kb_env_compat(self, p):
        """环境通过率矩阵（env_compat）：按方案取各被测版本的历史对照。"""
        return {"entries": self._orch.kb.env_compat_matrix(
            test_app_version=p.get("testAppVersion"),
            suite_id=p.get("suiteId"),
            case_ids=p.get("caseIds"),
            limit=int(p.get("limit", 12) or 12))}

    # ---------- DSL 审阅 ----------

    def _dsl_review(self, p):
        return self._orch.cases.show(_require(p, "caseId"))

    def _dsl_save(self, p):
        return self._orch.cases.import_draft(
            _require(p, "text"), p.get("format", "json"), case_id=p.get("caseId"))

    def _dsl_accept(self, p):
        case_id = _require(p, "caseId")
        if p.get("expectedRevision"):
            current = self._orch.cases.show(case_id)
            if current.get("revision") != p["expectedRevision"]:
                raise RpcError(INVALID_PARAMS, "用例已变更，请重新加载后审核")
        if p.get("text") is not None:
            self._orch.cases.import_draft(p["text"], p.get("format", "json"), case_id)
        return self._orch.cases.accept(case_id)

    def _dsl_reject(self, p):
        return self._orch.cases.reject(_require(p, "caseId"))

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

    async def serve(self, host: str, port: int) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async def handler(ws, path=None):     # 兼容 websockets 新旧 handler 签名
            self._subs.add(ws)
            lock = asyncio.Lock()
            self._send_locks[ws] = lock
            # 每帧独立处理：task.run 可能执行数小时，若整条连接串行消费，
            # 运行期间 task.abort / health / shutdown 将永远排队到 run 结束才生效。
            pending: set[asyncio.Task] = set()

            async def respond(raw) -> None:
                resp = await self._handle_raw(raw)
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
            allowed = origin in (None, "tauri://localhost", "http://tauri.localhost",
                                 "https://tauri.localhost")
            if allowed and hmac.compare_digest(supplied, f"Bearer {self.token}"):
                return None
            if hasattr(request, "headers"):
                return connection.respond(403, "RPC authorization required")
            return (403, [], b"RPC authorization required")

        async with websockets.serve(handler, host, port, process_request=authorize):
            log.info(f"RPC 服务已启动: ws://{host}:{port}")
            await self._stop.wait()
        log.info("RPC 服务已停止")

    async def _handle_raw(self, raw) -> dict | None:
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
        reserved = False
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
            return None if is_notification else \
                {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as e:
            return None if is_notification else self._err(req_id, e.code,
                                                          e.message, e.data)
        except Exception as e:
            log.exception(f"RPC 方法异常: {method}")
            return None if is_notification else \
                self._err(req_id, INTERNAL_ERROR, str(e))
        finally:
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
        self._executor.shutdown(wait=False)
        self._run_executor.shutdown(wait=False)


def _require(params: dict, key: str):
    value = params.get(key)
    if value is None or value == "":
        raise RpcError(INVALID_PARAMS, f"缺少必填参数: {key}")
    return value


def _to_int(params: dict, key: str) -> int:
    """整型参数统一入口：类型/取值错误一律 INVALID_PARAMS，而不是兜成 INTERNAL_ERROR。"""
    value = _require(params, key)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RpcError(INVALID_PARAMS,
                       f"参数 {key} 需为整数（得到 {value!r}）") from None


def _to_int_list(params: dict, key: str) -> list[int]:
    """整型列表参数：壳传数组；容忍 "1,2" 形式的字符串（历史调用方）。"""
    value = _require(params, key)
    items = value if isinstance(value, (list, tuple)) else \
        [p for p in str(value).split(",") if p.strip()]
    out: list[int] = []
    for item in items:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise RpcError(INVALID_PARAMS,
                           f"参数 {key} 含非整数项: {item!r}") from None
    return out

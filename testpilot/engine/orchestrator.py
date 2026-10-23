"""任务编排 — 建任务、驱动装配（注册表+DI）、执行、禅道回写、报告（设计 3.2 / 8.11）。

M1 形态：**本地优先**
- 建任务默认只落本地（``sync_status='local'``）；配置允许且禅道可用时才建禅道任务并回填
  ``zentao_task_id``（M2-1 建任务链路已就绪，M1 不强依赖网络）；
- run 结束时禅道回写只发**任务级汇总**；失败重试耗尽则把任务标记 ``sync_pending``，
  由 CLI/RPC 的补偿入口重推（设计 3.5）；
- 顶层编排是唯一持有 DB 连接与生命周期的地方，子组件全部构造期注入。
"""
import logging
import platform
import threading
from pathlib import Path

from testpilot.core.contracts import ExecContext
from testpilot.core.errors import AbortRequested, TestPilotError, ZentaoError
from testpilot.core.events import RunFinished
from testpilot.engine.stats import driver_stats
from testpilot.infra.events import EventBus
from testpilot.infra import dpi
from testpilot.infra.logsys import bind_case, bind_run, extract_run_log

log = logging.getLogger("testpilot.orchestrator")

DEFAULT_STATS = ("pass", "fail", "blocked", "skip", "error", "kb_hit_cases",
                 "retry_cases", "retry_passed")

#: 复跑只针对"可能是环境抖动"的失败：pass 不必复跑，skip 是协作中止，blocked
#  是 KB 已知失败的确定性命中（复跑必然再次命中同一条目），复跑都没有意义。
RETRYABLE_STATUSES = frozenset({"fail", "error"})

#: 复跑次数上限：配置写错（比如填了 99）不该把一个用例跑成小时级
MAX_CASE_RETRY = 3


class TaskOrchestrator:
    def __init__(self, cfg: dict, registry, *, conn=None, zentao=None, llm=None,
                 emulator=None, drivers=None, bus=None, recorder=None):
        # 延迟导入避免 GUI/网络依赖在纯查询场景被加载
        from testpilot.adapters.llm_gateway import LlmGateway
        from testpilot.adapters.zentao_adapter import ZentaoAdapter
        from testpilot.case_parser import CaseService
        from testpilot.drivers.emulator_adapter import EmulatorAdapter
        from testpilot.engine.executor import Executor
        from testpilot.engine.judge import Judge
        from testpilot.engine.router import Router
        from testpilot.engine.verifier import Verifier
        from testpilot.knowledge.service import KnowledgeService
        from testpilot.storage.db import connect
        from testpilot.storage.recorder import Recorder
        from testpilot.suites import SuiteService

        self.cfg = cfg
        self.registry = registry
        self.conn = conn if conn is not None else connect(cfg["app"]["db"])
        self.recorder = recorder if recorder is not None else Recorder(
            self.conn, cfg["app"]["screenshot_root"])
        self._run_lock = threading.Lock()
        self._frozen_cases = {}
        self._abort = threading.Event()          # 协作中止总开关（每次 run 前重置）
        self._in_run = threading.Event()         # 是否有 run 正在执行（abort 的反馈依据）
        self._active_run_id: int | None = None    # begin_run 成功后的 run 级兜底句柄
        self.bus = bus if bus is not None else EventBus()

        # ---- 依赖装配：全部构造期注入（缺省按 cfg 构造） ----
        self.zentao = zentao if zentao is not None else ZentaoAdapter(cfg["zentao"])
        self.llm = llm if llm is not None else LlmGateway({
            **cfg["llm"], "vision": cfg.get("vision", {}).get("vlm", {})})
        self.emulator = emulator if emulator is not None else EmulatorAdapter(
            cfg["emulator"])
        self.kb = KnowledgeService(self.conn, cfg["knowledge"])

        self._drivers_injected = drivers is not None
        if drivers is None:
            # vision 驱动需要 LLM 网关（VLM 定位/视觉断言）：走注册表工厂绑定注入，
            # 而不是运行时属性后置注入（工程约定：依赖构造期完成装配）。
            from testpilot.drivers.vision_driver import VisionDriver
            if not registry.has_factory("vision"):
                registry.bind_factory(
                    "vision", lambda dcfg: VisionDriver(dcfg, self.llm))
            drivers = {}
            for name in registry.names(kind="interaction"):
                try:
                    drivers[name] = registry.create(name, cfg.get(name, {}))
                except Exception as e:      # 依赖未装（缺 pywinauto/cv2）不阻断装配
                    log.warning(f"驱动 {name} 装配跳过: {e}")
        self.drivers = drivers

        self.cases = CaseService(self.conn, self.zentao, self.llm, cfg,
                                 capabilities_provider=self._emulator_capabilities)
        self.suites = SuiteService(self.conn, self.cases)
        self.router = Router(cfg["driver"], self.kb.load_router_stats,
                             fail_streak_n=int(cfg["knowledge"].get(
                                 "router_fail_streak", 3)))
        self.verifier = Verifier(self.drivers, self.emulator, self.llm,
                                 cfg["driver"])
        self.judge = Judge(self.llm, self.kb, cfg)
        self.executor = Executor(self.drivers, self.router, self.verifier,
                                 self.judge, self.kb, self.recorder,
                                 self.emulator, self.llm, cfg["driver"],
                                 bus=self.bus)

    def create_explore_drivers(self) -> dict:
        """为一个 Explore session 创建独立 interaction driver 集合。

        UIADriver 持有当前窗口句柄/缓存，多个探索会话复用同一实例会互相覆盖
        当前窗口。正常装配走 registry 重建；测试/高级调用方显式注入 drivers 时，
        用 deepcopy 保留替身语义并隔离 calls/窗口状态。
        """
        if self._drivers_injected:
            import copy
            try:
                return {name: copy.deepcopy(driver)
                        for name, driver in self.drivers.items()}
            except Exception as e:
                raise TestPilotError(
                    f"显式注入的驱动无法为 Explore session 独立复制: {e}") from e

        drivers = {}
        for name in self.registry.names(kind="interaction"):
            try:
                drivers[name] = self.registry.create(name, self.cfg.get(name, {}))
            except Exception as e:
                log.warning(f"Explore 驱动 {name} 独立装配跳过: {e}")
        return drivers

    # ---------- 对外 API（CLI 与 RPC 共用） ----------

    def create_task(self, spec: dict) -> dict:
        """建任务：方案展开为快照 -> 可执行性门禁 -> 本地落库（+可选禅道建任务）。"""
        from testpilot.storage import repos

        name = str(spec.get("name") or "").strip()
        if not name:
            raise TestPilotError("任务名不能为空")

        raw_case_ids = spec.get("case_ids")
        direct_case_ids = _string_list_arg(
            [] if raw_case_ids is None else raw_case_ids, "case_ids")
        suite_id = spec.get("suite_id")
        if suite_id is not None:
            suite_id = _positive_int_arg(suite_id, "suite_id")
        force = spec.get("force", False)
        if not isinstance(force, bool):
            raise TestPilotError("force 必须是布尔值")
        remote = spec.get("remote")
        if remote is not None and not isinstance(remote, bool):
            raise TestPilotError("remote 必须是布尔值或 None")
        if spec.get("zentao_task_id") is not None:
            spec = dict(spec)
            spec["zentao_task_id"] = _positive_int_arg(
                spec["zentao_task_id"], "zentao_task_id")
        if suite_id is not None and direct_case_ids:
            raise TestPilotError("suite_id 与 case_ids 只能指定一种，避免任务范围歧义")

        explicit_preconditions = (
            _string_list_arg(spec.get("preconditions"), "preconditions")
            if spec.get("preconditions") is not None else None
        )
        raw_driver = spec.get("driver_config")
        if raw_driver is not None and not isinstance(raw_driver, dict):
            raise TestPilotError("driver_config 必须是对象")
        explicit_driver = dict(raw_driver or {})
        if explicit_driver.get("mode") is not None:
            from testpilot.core.dsl import DriverMode
            try:
                explicit_driver["mode"] = DriverMode(str(explicit_driver["mode"])).value
            except ValueError:
                allowed = ", ".join(mode.value for mode in DriverMode)
                raise TestPilotError(
                    f"非法驱动模式: {explicit_driver['mode']!r}（可选 {allowed}）") from None
        driver_config = dict(self.cfg["driver"])

        if suite_id is not None:
            snap = self.suites.expand(int(suite_id))
            case_ids = list(dict.fromkeys(snap["case_ids"]))
            preconditions = (explicit_preconditions if explicit_preconditions is not None
                             else list(snap["preconditions"]))
            # 优先级：全局默认 < 方案默认 < 本次任务显式选择。
            driver_config.update(snap["driver_config"])
        else:
            case_ids = list(dict.fromkeys(direct_case_ids))
            preconditions = explicit_preconditions

        driver_config.update(explicit_driver)

        if not case_ids:
            raise TestPilotError("任务至少需要一条用例（--suite 或 --case）")

        if preconditions is None:
            # 直接按用例建任务时也需要前置场景：与方案口径一致，由成员用例汇总。
            preconditions = self.cases.preconditions_of(case_ids)

        bad = self.cases.not_runnable(case_ids)
        if bad and not force:
            detail = "; ".join(f"{b['case_id']}({b['reason']})" for b in bad[:5])
            raise TestPilotError(
                f"存在不可执行用例，请先人审/解析，或显式 --force: {detail}")

        import json
        want_remote = remote
        if want_remote is None:
            want_remote = bool(self.cfg["zentao"].get("create_task", False))
        remote_spec = {key: spec.get(key) for key in
                       ("product_id", "build", "begin", "end", "owner", "execution",
                        "zentao_task_id")}
        remote_spec.update(name=name, case_ids=case_ids)
        mapped, unmapped = [], []
        for cid in case_ids:
            row = repos.get_case(self.conn, cid)
            zid = (row or {}).get("zentao_case_id")
            if zid is None:
                unmapped.append(cid)
            else:
                mapped.append(zid)
        remote_spec["zentao_case_ids"] = mapped
        if want_remote and unmapped:
            raise TestPilotError(f"这些用例没有禅道 ID 映射，不能创建远端关联: {unmapped}")
        # Persist intent before the external POST: a crash must never cause an automatic duplicate.
        task_id = repos.create_task_record(
            self.conn, name=name, case_ids=case_ids, suite_id=suite_id,
            preconditions=preconditions, driver_config=driver_config,
            sync_status="sync_pending" if want_remote else "local")
        remote_spec["creation_attempted"] = bool(want_remote)
        self.conn.execute("UPDATE test_task SET remote_spec_json=? WHERE id=?",
                          (json.dumps(remote_spec), task_id))
        self.conn.commit()
        remote_spec["creation_attempted"] = False
        request_spec = {**remote_spec, "remote": want_remote}
        zentao_task_id, sync_status = self._create_remote_task(name, request_spec, case_ids)
        remote_spec.update(creation_attempted=False,
                           creation_uncertain=request_spec.get("_creation_uncertain", False))
        self.conn.execute("UPDATE test_task SET remote_spec_json=? WHERE id=?",
                          (json.dumps(remote_spec), task_id))
        repos.set_task_sync(self.conn, task_id, sync_status, zentao_task_id)
        log.info(f"任务已创建: 本地#{task_id} 禅道#{zentao_task_id} "
                 f"cases={len(case_ids)} sync={sync_status}")
        return {"task_id": task_id, "zentao_task_id": zentao_task_id,
                "sync_status": sync_status, "case_ids": case_ids,
                "preconditions": preconditions, "not_runnable": bad}

    def _create_remote_task(self, name: str, spec: dict,
                            case_ids: list[str]) -> tuple[int | None, str]:
        """禅道建任务（可选）：配置开启且禅道可用才走；失败降级为本地任务。"""
        # remote=None 表示"未显式指定"：CLI 不带 --remote / RPC 未传时按配置
        # zentao.create_task 决定（dict.get 的默认值分支在调用方恒传键时不生效，
        # 必须显式判 None 才能让设置页的开关真正起作用）。
        want_remote = spec.get("remote")
        if want_remote is None:
            want_remote = bool(self.cfg["zentao"].get("create_task", False))
        if not want_remote:
            return None, "local"
        if not self.zentao.is_configured():
            log.warning("禅道未配置（base_url 仍为示例地址），任务仅落本地")
            return None, "local"
        try:
            zid = self.zentao.create_testtask({**spec, "name": name, "case_ids": case_ids})
            return zid, "synced"
        except ZentaoError as e:
            spec["_creation_uncertain"] = bool(getattr(e, "creation_uncertain", False))
            log.error(f"禅道建任务失败，降级为本地任务: {e}")
            return getattr(e, "task_id", None), "sync_pending"

    def run_task(self, task_id: int, run_cfg: dict | None = None,
                 writeback: bool = True) -> dict:
        """执行任务：环境快照 -> 场景预加载 -> 逐用例执行 -> 收尾（报告/回写）。"""
        from testpilot.report.reporter import Reporter
        from testpilot.storage import repos

        task = repos.get_task(self.conn, task_id)
        if task is None:
            raise TestPilotError(f"任务不存在: {task_id}")

        # 复位中止开关必须与"是否有 run 在执行"的可见状态同步变更：
        # abort 在两次 run 之间到达时会被下一次 run 的 clear 静默吞掉，
        # abort() 借助 _in_run 让调用方至少能得到"没有可中止的 run"的反馈。
        if not self._run_lock.acquire(blocking=False):
            raise TestPilotError("已有任务正在执行，请先中止或等待完成")
        self._in_run.set()
        try:
            return self._run_task_inner(task, run_cfg, writeback, Reporter)
        except Exception as e:
            # begin_run 之后任何未预期的 run 级异常都必须把数据库从 running 收尾。
            run_id = self._active_run_id
            if run_id is not None:
                fatal_stats = {
                    "status": "error",
                    "run_id": run_id,
                    "task_id": task_id,
                    "fatal_error": str(e),
                }
                try:
                    row = self.conn.execute(
                        "SELECT status FROM test_run WHERE id=?", (run_id,)).fetchone()
                    if row is not None and row["status"] == "running":
                        self.recorder.finish_run(run_id, fatal_stats)
                except Exception:
                    log.exception(f"run 兜底收尾失败: run#{run_id}")
                self.bus.emit(RunFinished.of(
                    run_id, {"phase": "finished", "stats": fatal_stats}))
            raise
        finally:
            self._active_run_id = None
            self._frozen_cases = {}
            self._in_run.clear()
            self._run_lock.release()

    def _run_task_inner(self, task: dict, run_cfg: dict | None, writeback: bool,
                        reporter_cls) -> dict:
        task_id = task["id"]
        self._abort.clear()                       # 新一轮 run 重置中止开关
        self._frozen_cases = {}
        for cid in task["case_ids"]:
            try:
                self._frozen_cases[cid] = self.cases.to_testcase(cid).model_copy(deep=True)
            except TestPilotError as e:
                self._frozen_cases[cid] = e
        env = self._collect_env(task, run_cfg)
        env["dsl_versions"] = {cid: case.version
                               for cid, case in self._frozen_cases.items()
                               if not isinstance(case, Exception)}
        env["dsl_cases"] = {
            cid: case.model_dump(mode="json") for cid, case in self._frozen_cases.items()
            if not isinstance(case, Exception)}
        env["effective_driver_config"] = {**task["driver_config"], **(run_cfg or {})}
        evidence_before = getattr(self.recorder, "evidence_errors", 0)
        run_id = self.recorder.begin_run({**env, "task_id": task_id})
        self._active_run_id = run_id
        bind_run(run_id)
        log.info(f"run 开始: run#{run_id} task#{task_id} "
                 f"cases={len(task['case_ids'])}")
        self.bus.emit(RunFinished.of(run_id, {"phase": "start", "task_id": task_id}))

        case_pres = {p for case in self._frozen_cases.values() if not isinstance(case, Exception)
                     for p in case.preconditions}
        shared = {**task, "preconditions": [p for p in task.get("preconditions", [])
                                           if p not in case_pres]}
        self._shared_preconditions = shared["preconditions"]
        preload_errors = self._preload_scenes(shared)
        mode = (run_cfg or {}).get("mode") or task["driver_config"].get("mode")
        hints = self.kb.prefetch(task["case_ids"], driver_mode=mode)
        llm_before = _llm_usage(self.llm)

        retry_n = _case_retry_n(task, run_cfg, self.cfg)
        if retry_n:
            log.info(f"失败用例复跑已启用: 每个失败用例最多再跑 {retry_n} 次")
        stats = {k: 0 for k in DEFAULT_STATS}
        stats["total"] = 0
        case_statuses: dict[str, str] = {}      # 供 env_compat 通过率矩阵汇总
        for case_id in task["case_ids"]:
            if self._abort.is_set():
                log.warning(f"收到中止请求，run#{run_id} 停止派发剩余用例")
                break
            try:
                self._run_one_case(run_id, case_id, hints, stats, case_statuses,
                                   retry_n)
            except TestPilotError as e:
                # 单用例前置门禁失败（未 active / DSL 缺失）不得连带整个 run 崩掉
                log.error(f"用例无法执行: {case_id} {e}")
                stats["error"] += 1
                stats["total"] += 1
                case_statuses[case_id] = "error"
                self._record_case_error(run_id, case_id, str(e))
            except Exception as e:
                # 编排层的最后防线：意外异常记为该用例 error 并继续（执行器内部
                # 已兜底一次，这里保住 run 级收尾）。失败隔离不等于静默，完整堆栈入日志。
                log.exception(f"用例执行意外异常(已隔离): {case_id} {e}")
                stats["error"] += 1
                stats["total"] += 1
                case_statuses[case_id] = "error"
                self._record_case_error(run_id, case_id, f"未预期异常: {e}")
            self.bus.emit(RunFinished.of(run_id, {"phase": "case_done",
                                                  "stats": dict(stats)}))

        # 驱动降级统计（设计 10 章指标）：从本次 run 的轨迹链汇总，报告/UI 直接消费
        stats["drivers"] = driver_stats(self.conn, run_id)
        stats["evidence_errors"] = getattr(self.recorder, "evidence_errors", 0) - evidence_before
        stats["evidence_incomplete"] = stats["evidence_errors"] > 0
        stats["status"] = ("aborted" if self._abort.is_set() else
                           "incomplete" if stats["evidence_incomplete"] else "completed")
        stats["run_id"] = run_id
        stats["task_id"] = task_id
        usage = _llm_usage(self.llm)
        stats["llm_usage"] = _usage_delta(llm_before, usage)
        # env_compat 通过率矩阵（M3 知识库第 4 类条目）：每次 run 汇总一次，
        # 失败只记日志 —— 知识统计不该让回归本身失败。
        env_compat = self._record_env_compat(env, task, run_id, case_statuses)
        if env_compat:
            stats["env_compat"] = env_compat
        if preload_errors:
            stats["preload_errors"] = preload_errors
        # 回写结果必须在 finish_run **之前**算出来：finish_run 会把 stats 定格进库，
        # 之后再补字段只会留在内存里（报告从库里读 -> 永远看不到"禅道回写"）。
        if writeback:
            try:
                stats["writeback"] = self._writeback(task, run_id, stats)
            except Exception as e:
                # 远端集成属于增强链：本地测试已完成时，未知回写异常不能让 run
                # 留在 running/error。记录错误并把任务留给补偿入口。
                log.exception(f"禅道回写意外异常(不阻断本地 run): run#{run_id}")
                stats["writeback"] = "error"
                stats["writeback_error"] = str(e)
                try:
                    from testpilot.storage import repos
                    repos.set_task_sync(self.conn, task["id"], "sync_pending")
                except Exception:
                    log.exception("写回 sync_pending 失败")
        else:
            stats["writeback"] = "skipped"
        self.recorder.finish_run(run_id, stats)

        # 报告是收尾的增强信息：生成失败（跨盘符/磁盘满等）不该把"执行成功"的
        # run_task 整个判成异常（RPC 会报错，但 run 其实已成功收尾）。
        try:
            report_path = reporter_cls(self.conn, self.cfg).generate(run_id)
            stats["report"] = str(report_path)
            stats["run_log_lines"] = self._extract_log(run_id, report_path)
            # 报告路径二次落库：finish_run 之后再补的字段只会留在内存里
            self.recorder.finish_run(run_id, stats)
        except Exception as e:
            log.exception(f"报告生成失败(不阻断 run 结果): run#{run_id}")
            stats["report_error"] = str(e)
        self.bus.emit(RunFinished.of(run_id, {"phase": "finished", "stats": stats}))
        log.info(f"run 结束: run#{run_id} {stats['status']} "
                 f"pass={stats['pass']}/{stats['total']}")
        return stats

    def _run_one_case(self, run_id: int, case_id: str, hints, stats: dict,
                      case_statuses: dict[str, str] | None = None,
                      retry_n: int = 0) -> None:
        """执行一条用例（必要时复跑）。

        复跑语义（flake 过滤）：
        - 只对 ``fail/error`` 复跑（RETRYABLE_STATUSES；blocked 是 KB 已知失败的
          确定性命中，skip 是协作中止，都不复跑），最多 ``retry_n`` 次；协作中止后不再复跑；
        - **复用同一个 case_exec 行**：最终状态/签名按"最后一次尝试"落库，
          每次尝试的摘要写进 ``case_exec.retry_json``，轨迹用 ``step_trace.attempt`` 区分
          —— 这样 run 统计、稳定性、对比这些既有口径全都不受复跑影响（一条用例一票）；
        - 复跑通过 = 疑似偶发：首次失败的证据（签名/归因）由 retry_json 保留，
          不会被"最终通过就清空失败标记"的既有语义吞掉。
        """
        from testpilot.infra.abort import AbortSignal

        case = self._frozen_cases.get(case_id)
        if isinstance(case, Exception):
            raise case
        if case is None:
            case = self.cases.to_testcase(case_id)
        case = case.model_copy(deep=True)
        bind_case(case.case_id)
        case_exec_id = self.recorder.begin_case(run_id, case)
        attempts: list[dict] = []

        def _tally(status: str) -> None:
            """按最终状态计数（一条用例只计一票）。"""
            key = status if status in stats else "error"
            stats[key] += 1
            stats["total"] += 1
            if case_statuses is not None:
                case_statuses[case_id] = status

        if not case.preconditions:
            case.preconditions = list(getattr(self, "_shared_preconditions", []))
        for attempt in range(1, retry_n + 2):
            ctx = ExecContext(run_id=run_id, case_exec_id=case_exec_id, case=case,
                              abort=AbortSignal(self._abort), attempt=attempt)
            try:
                result = self.executor.run_case(case, ctx, hints)
            except AbortRequested:
                # 兜底：run_case 内部已自行收尾（含正常返回的路径），只有异常逃逸时才走到这里
                self.recorder.finish_case(case_exec_id, "skip", None, None, 0, None)
                attempts.append({"attempt": attempt, "status": "skip",
                                 "duration_ms": 0, "failure_signature": None,
                                 "kb_hit_id": None, "root_cause": None})
                _record_attempts(self.recorder, case_exec_id, attempts, stats)
                _tally("skip")
                log.warning(f"用例被中止: {case_id}")
                return

            attempts.append(_attempt_summary(attempt, result))
            if result.status not in RETRYABLE_STATUSES or attempt > retry_n:
                break
            if self._abort.is_set():
                log.warning(f"已收到中止请求，不再复跑: {case_id}")
                break
            log.info(f"用例失败复跑（第 {attempt + 1} 次）: {case_id} "
                     f"上次={result.status}")

        _record_attempts(self.recorder, case_exec_id, attempts, stats)
        _tally(result.status)
        if len(attempts) > 1 and result.status == "pass":
            stats["retry_passed"] += 1
            log.info(f"复跑后通过（疑似偶发）: {case_id} 共 {len(attempts)} 次")
        if result.kb_hit:
            # KB 命中率 = 命中已知失败库的用例占比（Runs 列表与报告直接用）
            stats["kb_hit_cases"] += 1

    def _record_case_error(self, run_id: int, case_id: str, message: str) -> None:
        """记录一条"无法执行"的用例（前置门禁失败），保持轨迹完整可解释。"""
        from testpilot.core.contracts import StepResult
        from testpilot.core.dsl import TestCase
        from testpilot.storage import repos

        row = repos.get_case(self.conn, case_id)
        if row is None:
            return                       # 本地不存在 -> 无法满足 case_exec 外键
        case = TestCase(case_id=case_id, zentao_case_id=row.get("zentao_case_id"),
                        title=row.get("title", ""), version=row.get("version", 0))
        case_exec_id = self.recorder.begin_case(run_id, case)
        self.recorder.record_step(case_exec_id, StepResult(
            step_no=1, step_id="precheck", action="precheck", status="fail",
            error={"type": "dsl_error", "message": message}))
        self.recorder.finish_case(
            case_exec_id, "error", None, None, 0,
            {"category": "dsl_error", "source": "precheck", "confidence": 1.0,
             "evidence": message, "suggestion": "先完成用例人审（case accept）",
             "needs_review": True})

    def _record_env_compat(self, env: dict, task: dict, run_id: int,
                           case_statuses: dict[str, str]) -> dict:
        """把本次 run 的结果并入 env_compat 通过率矩阵（失败不阻断 run 收尾）。"""
        try:
            return self.kb.record_env_compat(
                run_id=run_id, test_app_version=env.get("test_app_version"),
                suite_id=env.get("suite_id"), case_ids=list(task["case_ids"]),
                statuses=case_statuses, driver_mode=env.get("driver_mode"),
                emulator_version=env.get("emulator_version"),
                condition={key: env.get(key) for key in
                           ("dsl_versions", "driver_mode", "case_retry", "emulator_version",
                            "effective_driver_config")})
        except Exception as e:      # 知识统计不该让回归本身失败
            log.warning(f"env_compat 汇总失败(不阻断): {e}")
            return {}

    def _writeback(self, task: dict, run_id: int, stats: dict) -> str:
        """禅道任务级汇总回写；失败 -> 本地标记 sync_pending（补偿入口）。"""
        from testpilot.storage import repos

        zid = task.get("zentao_task_id")
        if not zid:
            # 区分“本来就是本地任务”和“用户要求远端，但创建仍待补偿”。
            # 两者都没有远端 ID，但报告/RunDetail 的语义完全不同。
            return "sync_pending" if task.get("sync_status") == "sync_pending" \
                else "local_only"
        if stats.get("status") != "completed":
            return "not_completed"
        try:
            self._verify_remote_task(task)
            self.zentao.update_task_status(zid, "done", stats)
        except ZentaoError as e:
            log.error(f"禅道回写失败(已重试)，标记 sync_pending: {e}")
            repos.set_task_sync(self.conn, task["id"], "sync_pending")
            return "sync_pending"
        repos.set_task_sync(self.conn, task["id"], "synced")
        return "synced"

    def reconcile_remote_task(self, task_id: int, zentao_task_id: int):
        """人工绑定/核对远端任务；与正式 run 互斥，避免执行快照读取旧远端状态。"""
        if not self._run_lock.acquire(blocking=False):
            raise TestPilotError("测试任务执行中，暂不能核对远端测试单")
        try:
            import json
            from testpilot.storage import repos
            task = repos.get_task(self.conn, task_id)
            if task is None:
                raise TestPilotError("本地任务不存在")
            row = self.conn.execute(
                "SELECT remote_spec_json FROM test_task WHERE id=?",
                (task_id,)).fetchone()
            spec = json.loads(row[0]) if row and row[0] else {}
            verifier = getattr(self.zentao, "verify_testtask", None)
            if verifier is None:
                raise TestPilotError("适配器不支持远端核对")
            verifier(zentao_task_id, spec)
            spec.update(zentao_task_id=zentao_task_id,
                        creation_attempted=False, creation_uncertain=False)
            self.conn.execute("UPDATE test_task SET remote_spec_json=? WHERE id=?",
                              (json.dumps(spec), task_id))
            repos.set_task_sync(
                self.conn, task_id, "sync_pending", zentao_task_id)
            results = self._sync_pending_tasks_unlocked(task_id)
            saved = repos.get_task(self.conn, task_id)
            return {"task_id": task_id, "zentao_task_id": zentao_task_id,
                    "sync_status": saved["sync_status"] if saved else "sync_pending",
                    "results": results}
        finally:
            self._run_lock.release()

    def _verify_remote_task(self, task):
        import json
        verifier = getattr(self.zentao, "verify_testtask", None)
        if verifier is None:
            return
        row = self.conn.execute("SELECT remote_spec_json FROM test_task WHERE id=?",
                                (task["id"],)).fetchone()
        spec = json.loads(row[0]) if row and row[0] else {}
        if "zentao_case_ids" not in spec:
            from testpilot.storage import repos
            records = [repos.get_case(self.conn, cid) for cid in task["case_ids"]]
            if any(not r or r.get("zentao_case_id") is None for r in records):
                raise ZentaoError("旧任务缺少禅道用例 ID 映射，请重新建任务")
            spec["zentao_case_ids"] = [r["zentao_case_id"] for r in records]
        verifier(task["zentao_task_id"], spec)

    def sync_pending_tasks(self, task_id: int | None = None) -> list[dict]:
        """补偿入口：与正式 run 互斥，防止远端绑定与执行快照发生状态撕裂。"""
        if not self._run_lock.acquire(blocking=False):
            raise TestPilotError("测试任务执行中，暂不能补偿远端同步")
        try:
            return self._sync_pending_tasks_unlocked(task_id)
        finally:
            self._run_lock.release()

    def _sync_pending_tasks_unlocked(
            self, task_id: int | None = None) -> list[dict]:
        """重推 sync_pending；调用方必须已经持有 _run_lock。"""
        from testpilot.storage import repos

        out = []
        for task in repos.list_tasks(self.conn):
            if task["sync_status"] != "sync_pending":
                continue
            if task_id is not None and task["id"] != task_id:
                continue
            if not task.get("zentao_task_id"):
                import json
                row = self.conn.execute("SELECT remote_spec_json FROM test_task WHERE id=?",
                                        (task["id"],)).fetchone()
                spec = json.loads(row[0]) if row and row[0] else None
                if not spec:
                    out.append({"task_id": task["id"], "result": "no_zentao_id"})
                    continue
                # Fail-closed at the orchestration boundary: after a process crash or
                # an uncertain POST response we do not know whether Zentao created the
                # remote task. Never rely on a specific adapter to prevent a duplicate.
                # A caller-supplied existing ID is safe to verify/rebind, so it bypasses
                # this gate and create_testtask() will take its verify-only path.
                existing_remote = spec.get("zentao_task_id")
                if not existing_remote and (
                        spec.get("creation_uncertain") or
                        spec.get("creation_attempted")):
                    out.append({
                        "task_id": task["id"],
                        "result": "creation_uncertain",
                        "action": "reconcile_remote_id",
                    })
                    continue
                try:
                    zid = self.zentao.create_testtask(spec)
                    repos.set_task_sync(self.conn, task["id"], "sync_pending", zid)
                    task["zentao_task_id"] = zid
                except ZentaoError as e:
                    created_id = getattr(e, "task_id", None)
                    if created_id:
                        repos.set_task_sync(self.conn, task["id"], "sync_pending", created_id)
                    out.append({"task_id": task["id"], "result": f"failed: {e}"})
                    continue
            # 远端测试单已经创建/核对成功，而任务尚未执行时，没有任何
            # “待回写结果”。此时同步状态应回到 synced；否则 UI 会永久提示补偿。
            last_run = self.last_run_of_task(task["id"])
            if last_run is None:
                repos.set_task_sync(
                    self.conn, task["id"], "synced", task.get("zentao_task_id"))
                out.append({"task_id": task["id"], "result": "remote_ready"})
                continue
            if not (last_run.get("stats") or {}).get("total"):
                out.append({"task_id": task["id"], "result": "no_run_stats",
                            "run_id": last_run["id"],
                            "status": last_run["status"]})
                continue
            stats = last_run["stats"]
            if last_run["status"] != "completed" or stats.get("status") != "completed":
                out.append({"task_id": task["id"], "result": "not_completed",
                            "run_id": last_run["id"], "status": last_run["status"]})
                continue
            try:
                self._verify_remote_task(task)
                self.zentao.update_task_status(task["zentao_task_id"], "done", stats)
                repos.set_task_sync(self.conn, task["id"], "synced")
                out.append({"task_id": task["id"], "result": "synced"})
            except ZentaoError as e:
                out.append({"task_id": task["id"], "result": f"failed: {e}"})
        return out

    def abort(self) -> bool:
        """协作中止：当前 run 安全收尾（已完成轨迹全部保留）。

        返回是否有 run 正在执行 —— 没有时调用方（RPC/CLI）能如实告知，
        而不是恒报 ok 把"没东西可中止"伪装成"已中止"。
        """
        in_flight = self._in_run.is_set()
        self._abort.set()
        log.warning("已发出中止请求（当前 run 安全收尾）")
        return in_flight

    # ---------- 查询 ----------

    def list_tasks(self) -> list[dict]:
        from testpilot.storage import repos
        return repos.list_tasks(self.conn)

    def task_detail(self, task_id: int) -> dict | None:
        from testpilot.storage import repos
        task = repos.get_task(self.conn, task_id)
        if task is not None:
            task["runs"] = repos.list_runs(self.conn, limit=200, task_id=task_id)
        return task

    def last_run_of_task(self, task_id: int) -> dict | None:
        """该任务最近一次 run（直查，不受"全库最新 N 条"窗口限制）。"""
        import json as _json
        row = self.conn.execute(
            "SELECT id, task_id, started_at, finished_at, status, stats_json"
            " FROM test_run WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["stats"] = _json.loads(out.pop("stats_json") or "{}")
        except (TypeError, ValueError):
            out["stats"] = {}
        return out

    def health(self) -> dict:
        """引擎自检：DB / 驱动 / 禅道 / 模拟器（供 health RPC 与 CLI doctor）。"""
        from testpilot.storage.repos import list_runs

        drivers = {}
        for name, drv in self.drivers.items():
            try:
                rep = drv.health()
                drivers[name] = {"ok": rep.ok, "detail": rep.detail}
            except Exception as e:
                drivers[name] = {"ok": False, "detail": str(e)}
        try:
            list_runs(self.conn, limit=1)
            db_ok, db_detail = True, str(self.cfg["app"]["db"])
        except Exception as e:
            db_ok, db_detail = False, str(e)

        try:
            from testpilot.adapters.svn_adapter import SvnAdapter
            svn_health = SvnAdapter(self.cfg.get("svn", {})).health()
        except Exception as e:
            svn_health = {"ok": False, "detail": str(e)}
        try:
            from testpilot.agent.pi_runtime import PiAgentRuntime
            agent_rep = PiAgentRuntime(
                (self.cfg.get("agent") or {}).get("pi") or {},
                rpc_url="ws://127.0.0.1:0", rpc_token="health-only").health()
            agent_health = {"ok": agent_rep.ok, "detail": agent_rep.detail}
        except Exception as e:
            agent_health = {"ok": False, "detail": str(e)}
        # 待回写任务数：壳侧导航的"禅道告警"徽章数据源（前端此前读的字段
        # 引擎从不产出，告警点恒不亮）
        try:
            sync_pending = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM test_task WHERE sync_status="
                "'sync_pending'").fetchone()["n"])
        except Exception:
            sync_pending = 0
        return {
            "ok": db_ok and all(d["ok"] for d in drivers.values()) if drivers
            else db_ok,
            "db": {"ok": db_ok, "detail": db_detail},
            "drivers": drivers,
            "zentao": {"configured": self.zentao.is_configured()},
            "agent": agent_health,
            "svn": svn_health,
            "sync_pending": sync_pending,
            "emulator": {"connected": getattr(self.emulator, "connected", False)},
            "paths": self.resource_paths(),
            # 资源路径：桌面壳据此注册读取白名单（截图/报告/DB）
            # DPI 屏幅与坐标空间一致性告警（设计 13 章：高 DPI 坐标错位）
            "dpi": dpi.describe(),
            "warnings": dpi.warnings(),
        }

    def resource_paths(self) -> dict:
        """供桌面壳注册资源白名单的**绝对路径**。

        为什么必须由引擎提供、而不是前端自己拼：壳把 sidecar 的 cwd 设成数据根，
        但**壳进程自己的 cwd 不是数据根** —— 前端拿到相对路径（``./data/shots``）
        再交给 Rust 的 ``canonicalize()``，会按壳的 cwd 解析成完全错误的位置，
        表现为"截图/报告永远读不出来"。

        为什么必须 ``mkdir``：Rust 侧 ``canonicalize()`` 对**不存在的路径会失败**，
        该根就注册不上。``report_root`` 在首次生成报告前并不存在，所以这里补建。
        建目录本身无害（它们是本就要写入的产出目录）。
        """
        out: dict[str, str] = {}
        for key in ("db", "screenshot_root", "report_root"):
            raw = (self.cfg.get("app") or {}).get(key)
            if not raw:
                continue
            p = Path(str(raw)).expanduser()
            try:
                if p.suffix:                      # db 是文件：建父目录，并解析其绝对路径
                    p.parent.mkdir(parents=True, exist_ok=True)
                    out[key] = str(p.resolve())
                else:                             # 其余是目录：建目录本身
                    p.mkdir(parents=True, exist_ok=True)
                    out[key] = str(p.resolve())
            except OSError as e:
                # 建不出来（权限/路径非法）不该让 health 整体失败 —— 前端会看到该键缺失
                log.warning(f"资源路径不可用（{key}={raw}）：{e}")
        return out

    def close(self) -> None:
        try:
            if hasattr(self.emulator, "close"):
                self.emulator.close()
        finally:
            try:
                self.conn.close()
            except Exception:  # 已关闭/共享连接
                pass

    # ---------- 内部 ----------

    def _emulator_capabilities(self) -> dict | None:
        """能力清单（供解析 prompt 白名单注入）；模拟器离线时返回 None，不阻塞解析。"""
        try_once = getattr(self.emulator, "try_capabilities", None)
        if callable(try_once):
            return try_once() or None
        try:
            return self.emulator.capabilities or None
        except Exception:
            return None

    def _preload_scenes(self, task: dict) -> list[dict]:
        """模拟器场景预加载：方案 preconditions 在 run 开始时统一加载，
        用例内 ``device.scene`` 仍可覆盖/切换；加载失败不阻断（记录在 run 统计）。"""
        errors = []
        for preset in task.get("preconditions") or []:
            text = str(preset)
            if not (text.startswith("模拟器场景:") or text.lower().startswith("scene:")):
                continue
            scene = text.split(":", 1)[1].strip()
            try:
                self.emulator.load_scene(scene)
                log.info(f"预加载模拟器场景: {scene}")
            except Exception as e:
                log.warning(f"场景预加载失败: {scene} {e}")
                errors.append({"scene": scene, "error": str(e)})
        return errors

    def _collect_env(self, task: dict, run_cfg: dict | None) -> dict:
        """环境快照：机器/驱动模式/屏幕/模拟器协议/用例 DSL 版本集（归因可比性）。"""
        from testpilot.storage import repos

        dsl_versions = {}
        for cid in task["case_ids"]:
            row = repos.get_case(self.conn, cid)
            if row:
                dsl_versions[cid] = row["version"]
        env = {
            "machine": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "driver_mode": (run_cfg or {}).get("mode")
            or task["driver_config"].get("mode", self.cfg["driver"]["mode"]),
            # 复跑策略入快照：开过复跑的 run 与没开的 run 不该直接比通过率
            "case_retry": _case_retry_n(task, run_cfg, self.cfg),
            "task_name": task.get("name"),
            "suite_id": task.get("suite_id"),
            "case_ids": list(task["case_ids"]),
            "dsl_versions": dsl_versions,
            "test_app_version": self._test_app_version(),
            "emulator_version": self._emulator_version(),
            "screen": _screen_size(),
            "dpi": dpi.describe(),
        }
        return env

    def _test_app_version(self) -> str:
        """被测 exe 版本（M2-5）：配置显式值优先，其次读 exe 文件版本，最后 unknown。"""
        from testpilot.infra.winver import file_version

        configured = str(self.cfg.get("app", {}).get("test_app_version") or "").strip()
        if configured:
            return configured
        exe = str(self.cfg.get("app", {}).get("test_app_exe") or "").strip()
        if exe:
            detected = file_version(exe)
            if detected:
                return detected
            log.info(f"被测程序版本探测失败，env_snapshot 记 unknown: {exe}")
        return "unknown"

    def _emulator_version(self) -> str:
        try:
            if getattr(self.emulator, "connected", False):
                return self.emulator.protocol_version()
        except Exception as e:
            log.debug(f"模拟器版本探测失败: {e}")
        return "unknown"

    def _extract_log(self, run_id: int, report_path) -> int:
        """把本 run 的日志行抽到报告目录（设计 7.5）。"""
        from pathlib import Path

        log_cfg = self.cfg.get("log", {})
        if not log_cfg.get("per_run_file"):
            return 0
        try:
            return extract_run_log(
                Path(log_cfg.get("dir", "./logs")) / "testpilot.log", run_id,
                Path(report_path).parent / "run.log")
        except Exception as e:
            log.warning(f"run 日志抽取失败(不阻断): {e}")
            return 0


def _case_retry_n(task: dict, run_cfg: dict | None, cfg: dict) -> int:
    """失败用例复跑次数（0=关闭）：单次 run 覆盖任务快照，任务快照覆盖全局配置。

    三级覆盖与驱动模式一致（``run_cfg`` > ``task.driver_config`` > ``cfg.driver``），
    因此方案级 ``--driver case_retry=1`` 与单次 ``task run --retry 1`` 都能生效。
    """
    raw = (run_cfg or {}).get("case_retry")
    if raw is None:
        raw = (task.get("driver_config") or {}).get("case_retry")
    if raw is None:
        raw = (cfg.get("driver") or {}).get("case_retry", 0)
    try:
        return max(0, min(int(raw), MAX_CASE_RETRY))
    except (TypeError, ValueError):
        log.warning(f"case_retry 配置非法，按 0 处理: {raw!r}")
        return 0


def _attempt_summary(attempt: int, result) -> dict:
    """一次尝试的摘要 —— 复跑通过后，首次失败的签名与归因只能从这里看到。"""
    return {
        "attempt": attempt,
        "status": result.status,
        "duration_ms": int(result.duration_ms or 0),
        "failure_signature": result.failure_signature,
        "kb_hit_id": result.kb_hit,
        "root_cause": result.root_cause,
    }


def _record_attempts(recorder, case_exec_id: int, attempts: list[dict],
                     stats: dict) -> None:
    """把尝试摘要落库（单次尝试不写，保持正常 run 的库干净），并累计复跑用例数。"""
    if len(attempts) <= 1:
        return
    recorder.record_attempts(case_exec_id, attempts)
    stats["retry_cases"] += 1


def _screen_size() -> str:
    size = dpi.screen_size()
    return f"{size[0]}x{size[1]}" if size else "unknown"


def _llm_usage(llm) -> dict:
    """LLM 计量快照（LlmPort 未强制该方法，缺失时按 0 计）。"""
    fn = getattr(llm, "usage_snapshot", None)
    if not callable(fn):
        return {}
    try:
        return dict(fn() or {})
    except Exception:
        return {}


def _usage_delta(before: dict, after: dict) -> dict:
    if not after:
        return {}
    return {k: int(after.get(k, 0)) - int((before or {}).get(k, 0)) for k in after}



def _positive_int_arg(value, field: str) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise TestPilotError(f"{field} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise TestPilotError(f"{field} 必须是正整数") from None
    if parsed <= 0:
        raise TestPilotError(f"{field} 必须是正整数")
    return parsed


def _string_list_arg(value, field: str) -> list[str]:
    """Normalize an application-level string list without iterating raw strings."""
    if not isinstance(value, (list, tuple)):
        raise TestPilotError(f"{field} 必须是字符串数组")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise TestPilotError(f"{field} 必须只包含非空字符串")
        text = item.strip()
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out

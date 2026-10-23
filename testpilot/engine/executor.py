"""执行引擎 — 用例主循环、双驱动调度、失败策略执行（设计 8.1）。

v1.3 含设备状态快照；M1 增：目标描述符入轨迹、协作取消可安全收尾、
事件推送（step/case 级）、能力门禁（驱动不支持的动作用 ActionError 而非 AttributeError）。
"""
import logging
import time

from testpilot.core.contracts import (CaseExecResult, Driver, ExecContext,
                                      LocatedElement, RecorderPort, StepResult)
from testpilot.core.dsl import OnFailure, Step, TestCase, execution_errors
from testpilot.core.errors import (AbortRequested, ActionError, AppError,
                                   DeviceError, LocateError)
from testpilot.core.events import CaseFinished, StepFinished
from testpilot.engine.router import DriverPlan
from testpilot.engine.verifier import Verifier
from testpilot.infra.events import EventBus
from testpilot.knowledge.signature import FailureSignature

log = logging.getLogger("testpilot.executor")

# 状态常量
# - 步骤级（StepResult.status）：pass | fail | blocked | skipped
# - 用例级（CaseExecResult.status）：pass | fail | blocked | skip | error（与 case_exec.status 同域）
PASS, FAIL, BLOCKED = "pass", "fail", "blocked"
SKIPPED = "skipped"      # 步骤被跳过
CASE_SKIP = "skip"       # 用例被跳过（因协作取消或 KB skip 处置）


def _device_cmd(step: Step) -> str:
    """回执里的指令名：device.call 取 args.cmd，device.scene 固定 load_scene。"""
    if step.action == "device.scene":
        return "load_scene"
    return str(step.args.get("cmd") or step.action)


def _attempt(driver: str, ok: bool, ms: int, error: str | None = None) -> dict:
    """驱动尝试链的一条记录（``step_trace.driver_chain_json`` 的元素）。

    ``ok`` 是该驱动的**完整尝试**结果（定位 + 动作）；定位失败但换驱动成功
    的步骤，链长 >= 2 且末位 ok=True —— 这就是"降级"（如 ``uia→vision``）。
    """
    return {"driver": driver, "ok": bool(ok), "ms": int(ms or 0), "error": error}


class Executor:
    def __init__(self, drivers: dict[str, Driver], router, verifier: Verifier,
                 judge, kb, recorder: RecorderPort, emulator, llm, cfg: dict,
                 bus: EventBus | None = None):
        # 依赖全部构造期注入，不做属性后置注入
        self.drivers = drivers
        self.router = router
        self.verifier = verifier
        self.judge = judge
        self.kb = kb
        self.recorder = recorder
        self.emulator = emulator
        self.llm = llm
        self.cfg = cfg
        self.bus = bus or EventBus()   # 无订阅者时为空转

    # ---------- 用例入口 ----------

    def run_case(self, case: TestCase, ctx: ExecContext, hints) -> CaseExecResult:
        t0 = time.monotonic()
        ctx.case = case
        result = CaseExecResult(case_id=case.case_id, status=PASS)
        log.info(f"用例开始: {case.case_id}")

        try:
            self._apply_case_ctx(case, ctx, hints)
            for step_no, step in enumerate(case.steps, start=1):
                if ctx.abort.requested:
                    result.status = CASE_SKIP
                    log.warning(f"收到中止请求，用例 {case.case_id} 剩余步骤跳过")
                    break

                res = self._run_step(step_no, step, ctx, hints)
                if res.status == SKIPPED:
                    result.status = CASE_SKIP
                    self._publish_step(ctx, result, res)
                    log.warning(f"收到中止请求，用例 {case.case_id} 剩余步骤跳过")
                    break
                if res.status == PASS:
                    self._publish_step(ctx, result, res)
                    continue

                # 失败策略必须在落库/推事件**之前**执行：KB 命中改判
                # （fail -> blocked/skipped）与 kb_hit 要体现在 step_trace 与
                # step.finished 事件里，否则轨迹和用例级状态口径不一致。
                action = self._apply_failure_policy(step, res, ctx, result)
                self._publish_step(ctx, result, res)
                if action == "break":
                    break
                if action == "abort":
                    result.status = "error"
                    break
                if action == "retry":
                    ctx.extra["step_retry"] = 1
                    res2 = self._run_step(step_no, step, ctx, hints)
                    ctx.extra.pop("step_retry", None)
                    if res2.status != PASS:
                        # 重试只有一次：res2 再失败时允许重跑归因（第二次失败的
                        # 原因可能与第一次不同），但不再产生新的 retry
                        action2 = self._apply_failure_policy(step, res2, ctx,
                                                             result,
                                                             allow_retry=False)
                        self._publish_step(ctx, result, res2)
                        if action2 == "abort":
                            result.status = "error"
                            break
                        break
                    self._publish_step(ctx, result, res2)
        except AbortRequested:
            raise
        except DeviceError as e:
            result.status = BLOCKED
            result.root_cause = {"category": "env", "source": "precondition",
                                 "evidence": str(e), "needs_review": True}
        except Exception as e:
            # 驱动/适配层漏出的意外异常（未转译成 TestPilotError，如 pywinauto 启动失败）
            # 也必须安全收尾：转成用例 error，不让 case_exec 悬挂 'running'，
            # 也不击穿任务级收尾（失败隔离是设计 1.3 的核心原则）。
            log.exception(f"用例执行意外异常(已隔离): {case.case_id}")
            result.status = "error"
            result.root_cause = {
                "category": "unexpected_error", "source": "executor",
                "confidence": 1.0, "evidence": str(e),
                "suggestion": "查看引擎日志定位未转译的驱动/适配层异常",
                "needs_review": True, "similar": [],
            }

        result.status = self._aggregate_status(result)
        if result.status in (PASS, CASE_SKIP):
            # 用例最终通过/跳过时不留下"失败签名/命中/归因"（失败但重试成功的细节
            # 仍完整保留在 step_trace 里，报告与回放可见）
            result.failure_signature = None
            result.kb_hit = None
            result.root_cause = None
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        log.info(f"用例结束: {case.case_id} -> {result.status} "
                 f"({result.duration_ms}ms)")
        # 谁执行谁收尾：case_exec 记录由本方法闭合（否则中途返回会留下 'running' 悬挂行，
        # 报告/看板会把"正在跑"和"跑完了"混为一谈）。
        self.recorder.finish_case(
            ctx.case_exec_id, result.status, result.failure_signature,
            result.kb_hit, result.duration_ms, result.root_cause)
        self.bus.emit(CaseFinished.of(ctx.run_id, ctx.case_exec_id, case.case_id,
                                      result.status, result.duration_ms,
                                      result.failure_signature))
        return result

    def _publish_step(self, ctx: ExecContext, result: CaseExecResult,
                      res: StepResult) -> None:
        """步骤轨迹落库 + 事件推送 + 内存收集（主循环的统一出口，保证三者总是成对）。"""
        self._record(ctx, res)
        result.steps.append(res)
        self.bus.emit(StepFinished.of(ctx.run_id, ctx.case_exec_id, res.step_no,
                                      res.step_id, res.status, res.driver))

    def _apply_case_ctx(self, case: TestCase, ctx: ExecContext, hints) -> None:
        """注入用例上下文（人审维护的 ``case_ctx``）：已知坑供归因，场景做前置加载。

        场景加载失败**不阻断**用例（记在 ``ctx.extra``，报告可见）——上下文是辅助信息，
        把它当硬门禁会让一次笔误变成执行风险。
        """
        entry = None
        for item in getattr(hints, "case_ctx", None) or []:
            if item.get("case_id") == case.case_id:
                entry = item
                break
        entry = entry or {}
        ctx.extra["case_ctx"] = entry
        scenes = [p.split(":", 1)[1].strip() for p in case.preconditions
                  if p.startswith("模拟器场景:") or p.lower().startswith("scene:")]
        scene = str(entry.get("scene") or (scenes[-1] if scenes else "")).strip()
        if not scene:
            return
        if self.emulator is None:
            raise DeviceError("用例前置场景需要模拟器端口")
        try:
            self.emulator.load_scene(scene)
            log.info(f"case_ctx 场景预加载: {scene}",
                     extra={"case_id": case.case_id})
        except Exception as e:
            ctx.extra["case_scene_error"] = f"{scene}: {e}"
            raise DeviceError(f"用例前置场景加载失败: {scene}: {e}") from e

    # ---------- 单步骤执行 ----------

    def _record(self, ctx: ExecContext, res: StepResult) -> None:
        # 带 attempt：复跑时同一用例会有多份轨迹，靠它区分（读取端默认只看最后一次）
        self.recorder.record_step(ctx.case_exec_id, res, ctx.attempt)

    def _run_step(self, step_no: int, step: Step, ctx: ExecContext,
                  hints) -> StepResult:
        res = StepResult(step_no=step_no, step_id=step.id, action=step.action,
                         status=PASS, source_text=step.source_text,
                         target_json=step.target.model_dump(mode="json")
                         if step.target else None)
        try:
            problems = execution_errors(step)
            if problems:
                res.status = FAIL
                res.error = {"type": "dsl_error", "message": "；".join(problems)}
                return res
            if step.action.startswith(("device.", "app.")):
                before = self._grab(ctx) if step.verify.type != "none" else None
                res = (self._run_device_step(res, step, ctx)
                       if step.action.startswith("device.")
                       else self._run_app_step(res, step, ctx))
                if res.status == PASS and step.verify.type != "none":
                    after = self._grab(ctx)
                    res.verify = self.verifier.verify(step, ctx, before, after)
                    if res.verify.status != "pass":
                        res.status = BLOCKED if res.verify.status == "unknown" else FAIL
                        res.error = {"type": "verify_unknown" if res.status == BLOCKED
                                     else "verify_failed", "detail": res.verify.detail}
                return res
            if step.action.startswith("assert."):
                return self._run_assert_step(res, step, ctx)
            if step.action == "sleep":
                return self._run_sleep_step(res, step, ctx)
            return self._run_ui_step(res, step, ctx, hints)
        except AbortRequested:
            res.status = SKIPPED
            res.error = {"type": "aborted", "message": "执行被中止"}
            return res

    def _run_sleep_step(self, res: StepResult, step: Step,
                        ctx: ExecContext) -> StepResult:
        """硬等待（慎用；DSL 规范保留该动作）。分片轮询以便及时响应取消。"""
        total = int(step.args.get("ms", step.timeout_ms))
        left = total / 1000
        while left > 0:
            ctx.abort.check()
            chunk = min(0.2, left)
            time.sleep(chunk)
            left -= chunk
        res.act_ms = total
        return res

    def _run_ui_step(self, res: StepResult, step: Step, ctx: ExecContext,
                     hints) -> StepResult:
        # ui.wait state=gone 语义特殊：控件**已经消失**正是目标状态，不能走
        # 常规"先定位后动作"（定位失败会被误判为步骤失败）
        if step.action == "ui.wait" \
                and str(step.args.get("state", "exists")) == "gone":
            return self._run_wait_gone(res, step, ctx, hints)

        plan = self.router.decide(step, hints)
        shot_before = self._grab(ctx)
        if shot_before is not None:
            res.screenshot_before = self.recorder.save_screenshot(
                ctx.run_id, ctx.case_exec_id, res.step_no, self._shot_phase(ctx, "before"),
                shot_before.png, attempt=ctx.attempt)

        if not self._locate_and_act(plan, step, ctx, res):
            return res   # status 已由 _locate_and_act 置为 fail

        shot_after = self._grab(ctx)
        if shot_after is not None:
            res.screenshot_after = self.recorder.save_screenshot(
                ctx.run_id, ctx.case_exec_id, res.step_no, self._shot_phase(ctx, "after"),
                shot_after.png, attempt=ctx.attempt)

        vr = self.verifier.verify(step, ctx, shot_before, shot_after)
        res.verify = vr
        if vr.status == "fail":
            res.status = FAIL
            res.error = {"type": "verify_failed",
                         "expect": step.verify.expect, "detail": vr.detail}
        elif vr.status == "unknown":
            # 低置信不硬判：步骤通过但标记人工复核
            res.status = BLOCKED
            res.error = {"type": "verify_unknown", "detail": vr.detail}

        # 学习反馈：元素库自动积累（M3-1）
        # 路由统计的读写时机在 _locate_and_act（按每次驱动尝试记录成败）
        if step.target and res.driver and res.status == PASS:
            self.kb.learn_element(step.target.key, {
                "driver": res.driver,
                "locate_ms": res.locate_ms,
                "locator": step.target.primary.model_dump(mode="json"),
                "prefer_vision": step.target.vision_only_hint,
            })
        return res

    def _run_wait_gone(self, res: StepResult, step: Step, ctx: ExecContext,
                       hints) -> StepResult:
        """ui.wait state=gone：定位失败即视为已消失；定位到则等其消失。

        语义（与"等待条件成立"一致）：目标状态是"控件不在"，所以——
        - 短窗口内定位不到（LocateError）→ 已经消失，pass；
        - 定位到了 → 调驱动 wait_gone 等它消失，超时才 fail；
        - 驱动不可用（如**被测程序未启动**抛 AppError）→ 不算目标达成，
          换下一驱动，全失败按步骤失败 —— 否则"没启动被测程序"会恒判通过。
        """
        plan = self.router.decide(step, hints)
        chain: list[dict] = []
        if step.target is None:
            res.error = {"type": "dsl_error", "message": "ui.wait 缺少 target"}
            res.status = FAIL
            return res

        primary = step.target.primary
        fallback = step.target.fallback
        capable = 0
        for driver_name in plan.attempts:
            drv = self.drivers.get(driver_name)
            if drv is None:
                continue
            caps = getattr(getattr(drv, "manifest", None), "capabilities", None)
            if caps is not None and "wait_gone" not in caps:
                log.info(f"驱动 {driver_name} 不支持 wait_gone，跳过")
                continue
            capable += 1
            loc = primary
            if driver_name != "uia" and fallback is not None \
                    and fallback.type in ("visual", "template"):
                loc = fallback
            t0 = time.monotonic()
            try:
                el = drv.locate(loc, ctx, timeout_ms=min(step.timeout_ms, 2000))
            except LocateError:
                # 已经消失 = 目标达成
                res.locate_ms = int((time.monotonic() - t0) * 1000)
                res.driver = driver_name
                chain.append(_attempt(driver_name, True, res.locate_ms))
                res.driver_chain = chain
                return res
            except AbortRequested:
                raise
            except Exception:
                continue  # 该驱动当前不可用（未附加/依赖缺失），换下一个
            res.driver = driver_name
            res.locate_ms = int((time.monotonic() - t0) * 1000)
            res.act_ms = 0
            gone = drv.wait_gone(el, ctx, step.timeout_ms)
            res.act_ms = int((time.monotonic() - t0) * 1000) - res.locate_ms
            chain.append(_attempt(driver_name, gone,
                                  res.locate_ms + res.act_ms,
                                  None if gone else "wait_gone_timeout"))
            res.driver_chain = chain
            if gone:
                return res
            res.error = {"type": "wait_gone_timeout",
                         "message": f"等待控件消失超时: {step.target.key}"}
            res.status = FAIL
            return res
        res.driver_chain = chain
        res.error = {
            "type": "capability_missing" if capable == 0 else "locate_timeout",
            "message": (
                f"当前驱动均不支持 wait_gone: {step.target.key}"
                if capable == 0
                else f"无可用的 wait_gone 驱动: {step.target.key}"
            ),
        }
        res.status = FAIL
        return res

    def _locate_and_act(self, plan: DriverPlan, step: Step, ctx: ExecContext,
                        res: StepResult) -> bool:
        """按计划顺序尝试驱动；定位自带超时，引擎不重复重试。"""
        if step.target is None:
            res.error = {"type": "dsl_error", "message": "交互步骤缺少 target"}
            res.status = FAIL
            return False

        primary = step.target.primary
        fallback = step.target.fallback
        chain: list[dict] = []      # 驱动尝试链（降级统计的数据源，设计 10 章）

        for driver_name in plan.attempts:
            drv = self.drivers.get(driver_name)
            if drv is None:
                log.warning(f"驱动未装配，跳过: {driver_name}")
                continue
            loc = primary
            if driver_name != "uia" and fallback is not None \
                    and fallback.type in ("visual", "template"):
                loc = fallback   # 视觉/Template 驱动优先使用视觉描述符

            t_loc = time.monotonic()
            try:
                el = drv.locate(loc, ctx, timeout_ms=step.timeout_ms)
            except LocateError as e:
                res.locate_ms = int((time.monotonic() - t_loc) * 1000)
                res.driver = driver_name
                chain.append(_attempt(driver_name, False, res.locate_ms,
                                      "locate_timeout"))
                # 路由学习反馈：定位失败按"每次驱动尝试"累加 fail_streak（M3-5）
                self.kb.feedback_router(step.target.key, driver_name, False,
                                        res.locate_ms)
                log.warning(f"定位失败，尝试下一驱动: driver={driver_name} "
                            f"target={step.target.key} {e}")
                continue
            res.driver = driver_name
            res.locate_ms = int((time.monotonic() - t_loc) * 1000)
            log.info("定位成功", extra={"driver": driver_name,
                                        "target_key": step.target.key,
                                        "locate_ms": res.locate_ms})

            t_act = time.monotonic()
            try:
                self._do_action(drv, el, step, ctx)
            except (ActionError, AppError) as e:
                res.act_ms = int((time.monotonic() - t_act) * 1000)
                res.error = {"type": "action_failed", "driver": driver_name,
                             "message": str(e)}
                chain.append(_attempt(driver_name, False,
                                      res.locate_ms + res.act_ms,
                                      "action_failed"))
                res.driver_chain = chain
                # 一次驱动尝试只反馈一次最终结果。定位已成功但动作失败仍算该
                # 驱动尝试失败；耗时字段保持“定位耗时”口径，不把动作时间混入。
                self.kb.feedback_router(step.target.key, driver_name, False,
                                        res.locate_ms)
                res.status = FAIL
                return False
            res.act_ms = int((time.monotonic() - t_act) * 1000)
            chain.append(_attempt(driver_name, True,
                                  res.locate_ms + res.act_ms))
            res.driver_chain = chain
            self.kb.feedback_router(step.target.key, driver_name, True,
                                    res.locate_ms)
            self._maybe_learn_template(drv, loc, el, ctx)
            return True

        res.driver_chain = chain
        res.error = {"type": "locate_timeout",
                     "message": f"全部驱动未定位: {step.target.key}"}
        res.status = FAIL
        return False

    def _do_action(self, drv: Driver, el: LocatedElement, step: Step,
                   ctx: ExecContext) -> None:
        ctx.abort.check()
        if step.action == "ui.click":
            self._require(drv, "click")
            drv.click(el, ctx)
        elif step.action == "ui.input":
            self._require(drv, "input")
            if step.args.get("clear", False):
                # 清空能力不在 Driver 协议内（仅 UIA 具备）；用 callable 判定，
                # 视觉驱动走这里会得到明确的 DSL 修正提示而不是 AttributeError。
                clear = getattr(drv, "select_all_and_delete", None)
                if not callable(clear):
                    raise ActionError(
                        f"驱动 {drv.manifest.name} 不支持 clear 输入，"
                        f"请在 DSL 中指定 uia 驱动")
                clear(el, ctx)
            drv.input_text(el, step.value or "", ctx)
        elif step.action == "ui.select":
            self._require(drv, "select")
            drv.select(el, step.value or "", ctx)
        elif step.action == "ui.toggle":
            self._require(drv, "toggle")
            drv.toggle(el, step.value == "true", ctx)
        elif step.action == "ui.keypress":
            self._require(drv, "keypress")
            drv.keypress(el, step.value or "", ctx)
        elif step.action == "ui.right_click":
            self._require(drv, "right_click")
            drv.right_click(el, ctx)
        elif step.action == "ui.scroll":
            self._require(drv, "scroll")
            drv.scroll(
                el,
                str(step.args.get("direction") or ""),
                int(step.args.get("amount", 1)),
                ctx,
            )
        elif step.action == "ui.drag":
            self._require(drv, "drag")
            drv.drag(
                el,
                int(step.args.get("dx", 0)),
                int(step.args.get("dy", 0)),
                int(step.args.get("duration_ms", 300)),
                ctx,
            )
        elif step.action == "ui.wait":
            state = str(step.args.get("state", "exists"))
            if state == "gone":
                self._require(drv, "wait_gone")
                if not drv.wait_gone(el, ctx, step.timeout_ms):
                    raise ActionError("等待控件消失超时")
            elif state != "exists":
                raise ActionError(f"未知 ui.wait state: {state}")
            # exists：locate 的轮询已保证控件出现，无需额外动作
        elif step.action == "vision.click":
            self._require(drv, "click")
            drv.click(el, ctx)
        else:
            raise ActionError(f"未知 action: {step.action}")

    @staticmethod
    def _maybe_learn_template(drv: Driver, loc, el: LocatedElement,
                              ctx: ExecContext) -> None:
        """视觉驱动 L2 命中后沉淀模板（L2->L1 成本闭环，M3-3）。

        只有真正的视觉语义定位（desc 非空）才学习；模板本身命中时无需重复学。
        学习失败绝不影响执行主干（驱动内部已兜底，这里再保一层）。
        """
        learn = getattr(drv, "learn_visual_template", None)
        if not callable(learn) or not getattr(loc, "desc", None):
            return
        try:
            learn(loc.desc, el.rect, ctx)
        except Exception as e:
            log.warning(f"模板沉淀失败(不阻断): {e}")

    @staticmethod
    def _require(drv: Driver, capability: str) -> None:
        caps = getattr(getattr(drv, "manifest", None), "capabilities", None)
        if caps is not None and capability not in caps:
            raise ActionError(
                f"驱动 {drv.manifest.name} 不支持能力: {capability}")

    # ---------- 设备/断言/应用步骤 ----------

    def _run_device_step(self, res: StepResult, step: Step,
                         ctx: ExecContext) -> StepResult:
        """device 步骤 = 模拟器同步控制点：同步调用、指令级超时、回执校验；
        执行后抓取模拟器状态快照入轨迹（回放时可见"发什么指令、设备变什么状态"）。"""
        t0 = time.monotonic()
        if self.emulator is None:
            res.status = FAIL
            res.driver = "emulator"
            res.device_receipt = {"step_id": step.id,
                                  "cmd": _device_cmd(step), "ok": False,
                                  "ms": None, "error": "模拟器端口未装配"}
            res.error = {"type": "device_error", "message": "模拟器端口未装配"}
            return res
        try:
            if step.action == "device.scene":
                scene_id = step.args.get("scene_id")
                if not scene_id:
                    raise DeviceError("device.scene 缺少 args.scene_id")
                self.emulator.load_scene(scene_id)
                res.device_receipt = {"step_id": step.id, "cmd": "load_scene",
                                      "ok": True, "ms": None}
            elif step.action == "device.call":
                cmd = step.args.get("cmd")
                if not cmd:
                    raise DeviceError("device.call 缺少 args.cmd")
                resp = self.emulator.call(cmd, step.args.get("args") or {},
                                          step.args.get("timeout_s"))
                res.device_receipt = {"step_id": step.id, "cmd": cmd,
                                      "ok": resp.get("ok"),
                                      "ms": resp.get("elapsed_ms")}
                if resp.get("ok") is False:
                    # 模拟器协议允许"送达但执行失败"的回执（{ok:false, err}）：
                    # 必须判失败，不能把 ok=false 写进回执后仍把步骤记 pass
                    raise DeviceError(
                        str(resp.get("err") or "模拟器指令回执 ok=false"))
            else:
                raise DeviceError(f"未知 device 动作: {step.action}")
            res.driver = "emulator"
            res.act_ms = int((time.monotonic() - t0) * 1000)
            # 回执耗时以引擎实测为准（FakeEmulator/旧模拟器可能没有 elapsed_ms）
            if res.device_receipt and res.device_receipt.get("ms") is None:
                res.device_receipt["ms"] = res.act_ms
            self._remember_receipt(ctx, res.device_receipt)
            res.driver_chain = [_attempt("emulator", True, res.act_ms)]
            self._snapshot_device(res)
        except DeviceError as e:
            res.status = FAIL
            res.driver = "emulator"
            res.act_ms = int((time.monotonic() - t0) * 1000)
            res.device_receipt = {"step_id": step.id,
                                  "cmd": _device_cmd(step),
                                  "ok": False, "ms": res.act_ms,
                                  "error": str(e)}
            self._remember_receipt(ctx, res.device_receipt)
            res.driver_chain = [_attempt("emulator", False, res.act_ms,
                                         "device_error")]
            res.error = {"type": "device_error", "message": str(e)}
        return res

    def _remember_receipt(self, ctx: ExecContext, receipt: dict) -> None:
        """回执同时进 case 上下文（兼容既有消费方）与 step_trace（M5+ 回放）。"""
        if receipt:
            ctx.extra.setdefault("device_receipts", []).append(dict(receipt))

    def _snapshot_device(self, res: StepResult) -> None:
        """状态快照（轻量摘要；失败取证可用 emulator.history() 全量回放）。"""
        try:
            snap = self.emulator.query_state("/state/summary")
            if snap is not None:
                res.device_state = snap
        except DeviceError:
            pass   # 快照失败不影响步骤判定

    def _run_assert_step(self, res: StepResult, step: Step,
                         ctx: ExecContext) -> StepResult:
        if step.verify is not None and step.verify.type == "screenshot_diff":
            # assert 步骤没有 before 截图可比较：静默放行会造成"断言恒通过"的假绿，
            # 必须显式判失败并提示正确用法（diff 断言挂在 ui 步骤的 verify 上）
            res.status = FAIL
            res.error = {"type": "dsl_error",
                         "message": "assert 步骤不支持 screenshot_diff 断言"
                                    "（没有 before 截图可比较）；"
                                    "请把 diff 断言写在 ui 步骤的 verify 上"}
            return res
        shot = self._grab(ctx)
        if shot is not None:
            res.screenshot_after = self.recorder.save_screenshot(
                ctx.run_id, ctx.case_exec_id, res.step_no, self._shot_phase(ctx, "after"), shot.png,
                attempt=ctx.attempt)
        vr = self.verifier.verify(step, ctx, None, shot)
        res.verify = vr
        if vr.status == "fail":
            res.status = FAIL
            res.error = {"type": "verify_failed", "detail": vr.detail}
        elif vr.status == "unknown":
            res.status = BLOCKED
            res.error = {"type": "verify_unknown", "detail": vr.detail}
        return res

    def _run_app_step(self, res: StepResult, step: Step,
                      ctx: ExecContext) -> StepResult:
        uia = self.drivers.get("uia")
        t0 = time.monotonic()
        if uia is None:
            res.status = FAIL
            res.driver = "uia"
            res.error = {"type": "app_error", "message": "UIA 驱动未装配"}
            return res
        try:
            if step.action == "app.launch":
                rect = uia.launch(step.args, timeout_s=step.timeout_ms / 1000)
                ctx.window_rect = rect
            elif step.action == "app.close":
                uia.close(step.args.get("mode", "graceful"))
                ctx.window_rect = None
            elif step.action == "app.wait_ready":
                if not uia.wait_ready(ctx, step.timeout_ms):
                    raise AppError("被测程序就绪超时")
            else:
                raise AppError(f"未知 app 动作: {step.action}")
            res.driver = "uia"
            res.act_ms = int((time.monotonic() - t0) * 1000)
            res.driver_chain = [_attempt("uia", True, res.act_ms)]
        except AppError as e:
            res.status = FAIL
            res.driver = "uia"
            res.act_ms = int((time.monotonic() - t0) * 1000)
            res.driver_chain = [_attempt("uia", False, res.act_ms, "app_error")]
            res.error = {"type": "app_error", "message": str(e)}
        return res

    # ---------- 失败策略 ----------

    def _apply_failure_policy(self, step: Step, res: StepResult,
                              ctx: ExecContext, result: CaseExecResult,
                              allow_retry: bool = True) -> str:
        """返回 continue | break | abort | retry。失败时先查知识库，命中按既定处置。

        只有**精确命中**才自动改判执行行为（block/skip/retry）；模糊与语义召回
        按"候选建议"处理（设计 9.2：命中结果仍需人审），不阻断、不重试。
        ``allow_retry=False`` 表示本次失败已是重试（不再产生新的 retry）。
        """
        if res.status == BLOCKED and (res.error or {}).get("type") == "verify_unknown":
            result.status = BLOCKED
            result.root_cause = {"category": "unknown", "source": "verification",
                                 "needs_review": True,
                                 "evidence": res.error.get("detail", "")}
            return "break"
        sig = FailureSignature.from_result(res, ctx)
        result.failure_signature = sig.digest()

        hit = self.kb.match_failure(sig)
        exact = hit is not None and getattr(hit, "match_kind", "exact") == "exact"
        if exact and hit.disposition == "block_known":
            res.status = BLOCKED
            res.kb_hit = hit.entry_id
            result.status = BLOCKED
            result.kb_hit = hit.entry_id
            result.root_cause = {
                "category": hit.category, "source": "kb_exact",
                "confidence": hit.confidence, "suggestion": hit.suggestion,
                "evidence": f"命中已知失败 entry#{hit.entry_id}",
                "needs_review": False, "similar": [],
            }
            log.info("命中已知失败，标记 blocked",
                     extra={"sig": result.failure_signature, "kb_hit": hit.entry_id})
            return "break"

        if exact and hit.disposition == "skip":
            # 人审既定处置"已知失败无需执行"：该步按跳过收尾（用例级 skip），
            # 轨迹里保留失败详情与 KB 命中，供回放核对
            res.status = SKIPPED
            res.kb_hit = hit.entry_id
            result.status = CASE_SKIP
            result.kb_hit = hit.entry_id
            result.root_cause = {
                "category": hit.category, "source": "kb_exact",
                "confidence": hit.confidence, "suggestion": hit.suggestion,
                "evidence": f"命中已知失败 entry#{hit.entry_id}，处置=skip（跳过执行）",
                "needs_review": False, "similar": [],
            }
            log.info("命中已知失败，按 skip 处置跳过",
                     extra={"sig": result.failure_signature, "kb_hit": hit.entry_id})
            return "break"

        if exact and hit.disposition == "retry_ready":
            # 人审既定处置"复跑可自愈"：重试一次该步骤（主循环只重试一次，
            # 仍失败按普通失败走既有归因/策略链路）。这是 KB 命中后唯一
            # 会改变执行行为的处置策略（设计 3.3 / knowledge 命中语义）。
            if allow_retry:
                res.kb_hit = hit.entry_id
                result.kb_hit = hit.entry_id
                log.info("命中已知失败，按 retry_ready 处置复跑一次",
                         extra={"sig": result.failure_signature,
                                "kb_hit": hit.entry_id})
                return "retry"
            # 已是重试后的失败：不再循环，落入下方归因（kb_hit 仍记录）
            res.kb_hit = hit.entry_id
            result.kb_hit = hit.entry_id

        report = self.judge.analyze(res, sig, hit, ctx)
        result.root_cause = {
            "category": report.category, "source": report.source,
            "confidence": report.confidence, "evidence": report.evidence,
            "suggestion": report.suggestion,
            "needs_review": report.needs_review,
            "similar": report.similar,          # 相似历史对照（M4-5）
        }
        if report.category == "app_crash":
            result.status = "error"
            return "abort"

        # 重试策略：步骤级 on_failure=retry，或全局 driver.retry_on_failure
        # 且本次失败属于"可自愈"的定位超时（重试只发生一次，见 run_case 主循环）
        transient = (res.error or {}).get("type") == "locate_timeout"
        if allow_retry and (step.on_failure == OnFailure.RETRY or (
                self.cfg.get("retry_on_failure") and transient)):
            return "retry"
        if step.on_failure == OnFailure.CONTINUE:
            if result.status == PASS:
                result.status = FAIL
            return "continue"
        if result.status == PASS:
            result.status = FAIL
        return "break"

    def _aggregate_status(self, result: CaseExecResult) -> str:
        """按**每个步骤的最后一次尝试**汇总（retry 成功后该步不再是 fail）。"""
        if result.status in (BLOCKED, "error", CASE_SKIP):
            return result.status
        last: dict[int, str] = {}
        for s in result.steps:
            last[s.step_no] = s.status
        statuses = list(last.values())
        if FAIL in statuses:
            return FAIL
        if BLOCKED in statuses:
            return BLOCKED
        if SKIPPED in statuses:
            return CASE_SKIP
        return result.status

    # ---------- 截图 ----------

    @staticmethod
    def _shot_phase(ctx, phase):
        retry = ctx.extra.get("step_retry")
        return f"retry{retry}_{phase}" if retry else phase

    def _grab(self, ctx: ExecContext):
        """按偏好顺序取第一个具备 grab 能力的驱动截图；失败不阻断执行。"""
        for name in ("vision", "uia"):
            drv = self.drivers.get(name)
            if drv is None:
                continue
            caps = getattr(getattr(drv, "manifest", None), "capabilities", None)
            if caps is not None and "grab" not in caps:
                continue
            try:
                return drv.grab(ctx.window_rect, ctx)
            except Exception as e:
                log.warning(f"截图失败({name}): {e}")
        return None

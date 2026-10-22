"""执行引擎单测 — 用例主循环、双驱动、失败策略、取消、轨迹（设计 8.1 / 4.3）。

本组测试用真实 Recorder/KnowledgeService/Router/Verifier/Judge + 假驱动/假模拟器/
假 LLM，覆盖 DB 落库真实行为（轨迹字段、路由统计、知识入库）。
"""
import threading

import pytest

from testpilot.core.contracts import ExecContext
from testpilot.engine.executor import Executor
from testpilot.engine.judge import Judge
from testpilot.engine.router import Router
from testpilot.engine.verifier import Verifier
from testpilot.infra.abort import AbortSignal
from testpilot.infra.events import EventBus
from testpilot.knowledge.service import FailureKnowledge, KnowledgeService
from testpilot.knowledge.signature import FailureSignature
from testpilot.storage import repos
from testpilot.storage.recorder import Recorder
from tests.conftest import (FakeDriver, FakeEmulator, FakeLlm, FakeVisionDriver,
                            FlakyDriver, make_case, make_step)


# ---------- 夹具 ----------

class Env:
    """一套真实引擎 + 假依赖；``rebuild()`` 在改了驱动/模式后重建执行器。"""

    def __init__(self, cfg, db, tmp_path):
        self.cfg = cfg
        self.db = db
        self.tmp = tmp_path
        self.kb = KnowledgeService(db, cfg["knowledge"])
        self.emulator = FakeEmulator()
        self.llm = FakeLlm()
        self.drivers = {"uia": FakeDriver("uia"), "vision": FakeDriver("vision")}
        self.recorder = Recorder(db, tmp_path / "shots")
        self.rebuild()

    def rebuild(self, bus=None):
        router = Router(self.cfg["driver"], self.kb.load_router_stats,
                        fail_streak_n=3)
        verifier = Verifier(self.drivers, self.emulator, self.llm,
                            self.cfg["driver"])
        judge = Judge(self.llm, self.kb, self.cfg)
        self.executor = Executor(self.drivers, router, verifier, judge, self.kb,
                                 self.recorder, self.emulator, self.llm,
                                 self.cfg["driver"], bus=bus)
        return self.executor


@pytest.fixture()
def env(cfg, db_conn, tmp_path):
    e = Env(cfg, db_conn, tmp_path)
    repos.create_task_record(db_conn, name="t", case_ids=["TC-1"])
    return e


def _start(e, case, abort_event=None):
    repos.create_case(e.db, case_id=case.case_id,
                      zentao_case_id=case.zentao_case_id, title=case.title,
                      raw=None, zentao_updated_at="t")
    run_id = e.recorder.begin_run({"task_id": 1, "driver_mode": "auto"})
    ce = e.recorder.begin_case(run_id, case)
    ctx = ExecContext(run_id=run_id, case_exec_id=ce, case=case,
                      abort=AbortSignal(abort_event))
    return run_id, ce, ctx


# ---------- 用例 ----------

def test_all_pass_records_trace_and_learns_element(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="app.launch", key="app.window",
                  args={"exe": "target.exe"}, source_text="启动被测程序"),
        make_step(id="s2"),
    ])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    assert [s.status for s in result.steps] == ["pass", "pass"]
    assert env.drivers["uia"].launched == {"exe": "target.exe"}

    trace = repos.case_steps(env.db, ce)
    assert len(trace) == 2
    click = trace[1]
    assert click["driver"] == "uia"
    assert click["action"] == "ui.click"
    assert click["source_text"] == "点击开始检测"
    assert click["target"]["key"] == "btn.start_detect"
    assert click["verify_result"] == "pass"
    assert click["screenshot_path"]          # before/after 至少一张

    # 元素知识自动积累 + 路由统计成功计数
    assert env.kb.query_element("btn.start_detect") is not None
    stats = env.kb.load_router_stats("btn.start_detect")
    assert stats["uia"].success_count == 1

    row = env.db.execute("SELECT * FROM case_exec WHERE id=?", (ce,)).fetchone()
    assert row["status"] == "pass"
    assert row["duration_ms"] >= 0
    assert row["failure_signature"] is None
    assert row["root_cause_json"] is None


def test_all_drivers_fail_yields_fail_and_pending_kb(env):
    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"
    assert result.failure_signature
    assert result.root_cause["category"] == "uia_change"
    step = result.steps[0]
    assert step.status == "fail"
    assert step.error["type"] == "locate_timeout"
    assert step.driver == "vision"          # 最后尝试的驱动

    row = env.db.execute("SELECT * FROM case_exec WHERE id=?", (ce,)).fetchone()
    assert row["status"] == "fail"
    assert row["failure_signature"] == result.failure_signature

    pending = repos.kb_pending(env.db)
    assert len(pending) == 1
    assert pending[0]["payload"]["category"] == "uia_change"

    trace = repos.case_steps(env.db, ce)
    assert trace[0]["error"]["type"] == "locate_timeout"
    assert trace[0]["driver"] == "vision"


def test_uia_fail_vision_success_marks_driver_vision(env):
    env.drivers["uia"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    assert result.steps[0].driver == "vision"
    assert repos.case_steps(env.db, ce)[0]["driver"] == "vision"
    assert env.kb.load_router_stats("btn.start_detect")["vision"].success_count == 1


def test_known_failure_blocks_case(env):
    sig = FailureSignature("locate_timeout", "ui.click", "btn.start_detect", "main")
    entry_id = env.kb.save_failure(FailureKnowledge(
        entry_id=0, signature=sig.digest(), category="uia_change",
        disposition="block_known", suggestion="控件改名了，更新 DSL",
        confidence=0.9, error_type=sig.error_type, action=sig.action,
        target_key=sig.target_key, page_key=sig.page_key))
    env.kb.confirm(entry_id, "active")

    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "blocked"
    assert result.kb_hit == entry_id
    assert result.root_cause["source"] == "kb_exact"
    row = env.db.execute("SELECT * FROM case_exec WHERE id=?", (ce,)).fetchone()
    assert row["status"] == "blocked" and row["kb_hit_id"] == entry_id
    # 命中已知失败不再新建 pending
    assert repos.kb_pending(env.db) == []


def test_on_failure_continue_keeps_running_steps(env):
    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[
        make_step(id="s1", on_failure="continue"),
        make_step(id="s2", on_failure="continue"),
    ])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"
    assert len(result.steps) == 2                   # 失败后继续
    assert len(repos.case_steps(env.db, ce)) == 2


def test_on_failure_retry_succeeds_second_time(env):
    env.cfg["driver"]["mode"] = "uia_only"
    env.drivers = {"uia": FlakyDriver(fail_times=1)}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s2", on_failure="retry")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    assert len(result.steps) == 2
    assert [s.status for s in result.steps] == ["fail", "pass"]


def test_global_retry_on_failure_applies_to_locate_timeout(env):
    """driver.retry_on_failure=true 时，定位超时类失败自动重试一次（步骤仍是默认 abort）。"""
    env.cfg["driver"]["mode"] = "uia_only"
    env.cfg["driver"]["retry_on_failure"] = True
    env.drivers = {"uia": FlakyDriver(fail_times=1)}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    assert [s.status for s in result.steps] == ["fail", "pass"]


def test_global_retry_does_not_apply_to_verify_failure(env):
    """全局重试只针对瞬时可自愈的定位超时；断言失败不得盲目重试。"""
    env.cfg["driver"]["retry_on_failure"] = True
    env.drivers["uia"].text = "结果: S999"
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="assert.ui",
                  verify={"type": "text", "expect": "S001", "op": "contains"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"
    assert len(result.steps) == 1


def test_retry_still_failing_breaks(env):
    env.cfg["driver"]["mode"] = "uia_only"
    env.drivers = {"uia": FakeDriver("uia", fail_locate=True)}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s2", on_failure="retry")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert len(result.steps) == 2
    assert [s.status for s in result.steps] == ["fail", "fail"]


def test_abort_marks_remaining_steps_skipped(env):
    event = threading.Event()

    class Aborting(FakeDriver):
        def click(self, el, ctx):
            event.set()
            return super().click(el, ctx)

    env.drivers = {"uia": Aborting("uia")}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s1"), make_step(id="s2")])
    run_id, ce, ctx = _start(env, case, abort_event=event)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "skip"                 # 用例级：skip（步骤级为 skipped）
    assert result.steps[0].status == "skipped"
    assert result.steps[0].error["type"] == "aborted"
    assert len(repos.case_steps(env.db, ce)) == 1      # 只记录已发生的那一步


def test_abort_before_first_step_skips_everything(env):
    event = threading.Event()
    event.set()
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case, abort_event=event)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "skip"
    assert result.steps == []


def test_verify_failed_marks_step_fail(env):
    env.drivers["uia"].text = "结果: S999"
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="assert.ui",
                  verify={"type": "text", "expect": "S001", "op": "contains"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].error["type"] == "verify_failed"
    assert result.root_cause["category"] == "uia_change"  # low-confidence rule escalates to LLM


def test_low_confidence_vision_assert_blocks_with_review_marker(env):
    env.llm.vision_result = {"pass": True, "confidence": 0.2, "reason": "模糊"}
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="assert.vision",
                  verify={"type": "vision", "expect": "有绿标"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "blocked"
    assert result.steps[0].error["type"] == "verify_unknown"
    trace = repos.case_steps(env.db, ce)
    assert trace[0]["verify_result"] == "unknown"


def test_device_steps_record_state_snapshot(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="device.scene", key="dev.scene",
                  args={"scene_id": "normal_sample_a"}, source_text="装载场景"),
        make_step(id="s2", action="device.call", key="dev.set",
                  args={"cmd": "set_pressure", "args": {"value": 100}}),
        make_step(id="s3", action="assert.state", key="dev.state",
                  args={"path": "/state/run", "expect": "completed"},
                  verify={"type": "state"}),
    ])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    assert env.emulator.scenes == ["normal_sample_a"]
    assert ("set_pressure", {"value": 100}) in env.emulator.calls
    trace = repos.case_steps(env.db, ce)
    assert [t["driver"] for t in trace] == ["emulator", "emulator", None]
    assert trace[1]["device_state"] == {"run": "completed", "grid": "a"}
    assert ctx.extra["device_receipts"][1]["cmd"] == "set_pressure"
    # 指令回执随轨迹落库（设计 14.3）：cmd/ok/ms 齐备
    assert trace[1]["receipt"] == {"step_id": "s2", "cmd": "set_pressure",
                                   "ok": True, "ms": 7}   # FakeEmulator 固定 7ms
    assert trace[0]["receipt"]["cmd"] == "load_scene"


def test_device_failure_receipt_persisted(env):
    """失败也要留回执：回放时能看到"发过什么指令、为什么失败"。"""
    env.emulator.fail = True
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="device.scene", key="dev",
                  args={"scene_id": "s"}, on_failure="abort")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"
    trace = repos.case_steps(env.db, ce)
    receipt = trace[0]["receipt"]
    assert receipt["cmd"] == "load_scene" and receipt["ok"] is False
    assert "fake" in receipt["error"] and isinstance(receipt["ms"], int)


def test_device_call_without_cmd_is_error(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="device.call", key="dev", args={})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].error["type"] == "dsl_error"
    assert "args.cmd" in result.steps[0].error["message"]


def test_device_error_from_emulator(env):
    env.emulator.fail = True
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="device.scene", key="dev",
                  args={"scene_id": "s"}, on_failure="abort")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.root_cause["category"] == "env"


def test_snapshot_failure_does_not_fail_step(env):
    class NoSnapshot(FakeEmulator):
        def query_state(self, path):
            from testpilot.core.errors import DeviceError
            raise DeviceError("no snapshot")

    env.emulator = NoSnapshot()
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="device.scene", key="dev",
                  args={"scene_id": "s"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"
    assert result.steps[0].device_state is None


def test_app_launch_without_uia_driver_fails(env):
    env.drivers = {"vision": FakeDriver("vision")}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="app.launch", key="app.window",
                  args={"exe": "x.exe"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].error["type"] == "app_error"


def test_scroll_and_drag_actions_reach_driver(env):
    env.cfg["driver"]["mode"] = "uia_only"
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.scroll",
                  args={"direction": "down", "amount": 4}),
        make_step(id="s2", action="ui.drag",
                  args={"dx": 80, "dy": -10, "duration_ms": 250}),
    ])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    calls = env.drivers["uia"].calls
    assert ("scroll", ("down", 4)) in calls
    assert ("drag", (80, -10, 250)) in calls


def test_scroll_capability_gate_blocks_unsupported_driver(env):
    env.cfg["driver"]["mode"] = "uia_only"
    env.drivers = {"uia": FakeDriver("uia", capabilities={"click", "grab"})}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.scroll",
                  args={"direction": "down", "amount": 2}),
    ])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"
    assert result.steps[0].error["type"] == "action_failed"
    assert "scroll" in result.steps[0].error["message"]


def test_capability_gate_blocks_unsupported_action(env):
    env.cfg["driver"]["mode"] = "vision_only"
    env.drivers = {"vision": FakeVisionDriver()}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.select", value="A")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    step = result.steps[0]
    assert step.status == "fail"
    assert step.error["type"] == "action_failed"
    assert "不支持能力" in step.error["message"]


def test_clear_input_requires_uia(env):
    env.cfg["driver"]["mode"] = "vision_only"
    env.drivers = {"vision": FakeVisionDriver()}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.input", value="S001",
                  args={"clear": True})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.steps[0].error["type"] == "action_failed"
    assert "clear" in result.steps[0].error["message"]


def test_input_with_clear_calls_uia_helper(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.input", value="S001", args={"clear": True})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"
    kinds = [c[0] for c in env.drivers["uia"].calls]
    assert "input" in kinds


def test_wait_gone_timeout_is_action_error(env):
    class NeverGone(FakeDriver):
        def wait_gone(self, el, ctx, timeout_ms):
            return False

    env.drivers = {"uia": NeverGone("uia")}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.wait", args={"state": "gone"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.steps[0].error["type"] == "wait_gone_timeout"


def test_wait_gone_already_vanished_is_pass(env):
    """ui.wait state=gone 核心语义：控件**已经消失**（定位失败）= 目标达成 = pass。

    旧实现先走常规定位，控件消失后定位失败被误判为步骤失败（实机发现）。"""
    from testpilot.core.errors import LocateError

    class Vanished(FakeDriver):
        def locate(self, loc, ctx, timeout_ms=None):
            raise LocateError("控件不在（已消失）", confidence=None)

        def wait_gone(self, el, ctx, timeout_ms):  # pragma: no cover - 不应到达
            raise AssertionError("locate 失败后不应再调 wait_gone")

    env.drivers = {"uia": Vanished("uia")}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.wait", args={"state": "gone"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"
    assert result.steps[0].status == "pass"
    assert result.steps[0].driver == "uia"


def test_wait_gone_eventually_vanishes_is_pass(env):
    class GoneInTime(FakeDriver):
        def wait_gone(self, el, ctx, timeout_ms):
            return True

    env.drivers = {"uia": GoneInTime("uia")}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.wait", args={"state": "gone"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"


def test_unknown_action_is_action_error(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.magic")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.steps[0].error["type"] == "dsl_error"
    assert "未知动作" in result.steps[0].error["message"]


def test_interaction_step_without_target_is_dsl_error(env):
    from testpilot.core.dsl import Step
    case = make_case("TC-1", steps=[Step(id="s1", action="ui.click")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.steps[0].error["type"] == "dsl_error"


def test_sleep_step_and_duration(env):
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="sleep", args={"ms": 20})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"
    assert result.steps[0].act_ms >= 15


def test_events_emitted_for_steps_and_case(env):
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e.event))
    env.rebuild(bus=bus)
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    env.executor.run_case(case, ctx, None)
    assert "step.finished" in seen
    assert "case.finished" in seen


def test_event_subscriber_exception_does_not_break_execution(env):
    bus = EventBus()
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    env.rebuild(bus=bus)
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    assert env.executor.run_case(case, ctx, None).status == "pass"


def test_vision_locate_triggers_template_learning(env):
    """视觉命中后，引擎回调驱动的模板沉淀接口（L2->L1 成本闭环，M3-3）。"""

    class LearningDriver(FakeDriver):
        def __init__(self):
            super().__init__("vision")
            self.learned = []

        def learn_visual_template(self, desc, rect, ctx):
            self.learned.append((desc, rect))
            return "x.png"

    drv = LearningDriver()
    env.cfg["driver"]["mode"] = "vision_only"
    env.drivers = {"vision": drv}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "pass"
    # 视觉驱动使用 fallback 描述符定位，学习也应基于同一个 desc
    assert drv.learned == [("开始检测按钮", (10, 20, 100, 30))]


def test_template_learning_failure_does_not_break_step(env):
    class BoomLearn(FakeDriver):
        def __init__(self):
            super().__init__("vision")

        def learn_visual_template(self, desc, rect, ctx):
            raise RuntimeError("disk full")

    env.cfg["driver"]["mode"] = "vision_only"
    env.drivers = {"vision": BoomLearn()}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    assert env.executor.run_case(case, ctx, None).status == "pass"


def test_template_learning_skipped_for_uia_driver(env):
    """UIA 命中不该触发视觉模板沉淀（无该接口 -> 静默跳过）。"""
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    assert env.executor.run_case(case, ctx, None).status == "pass"
    assert not hasattr(env.drivers["uia"], "learn_visual_template")


def test_grab_failure_does_not_block_step(env):
    class NoGrab(FakeDriver):
        def grab(self, rect, ctx):
            raise RuntimeError("屏幕锁了")

    env.drivers = {"uia": NoGrab("uia"), "vision": NoGrab("vision")}
    env.rebuild()
    case = make_case("TC-1", steps=[make_step(id="s1")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "pass"
    trace = repos.case_steps(env.db, ce)
    assert trace[0]["screenshot_path"] is None


# ---------- 回归：v1.8 审查修复 ----------

def test_wait_gone_app_not_attached_is_fail_not_false_pass(env):
    """被测程序未启动（AppError）≠ "控件已消失"：不能误判 pass（假阳性）。"""
    from testpilot.core.errors import AppError

    class NotAttached(FakeDriver):
        def locate(self, loc, ctx, timeout_ms=None):
            raise AppError("尚未附加/启动被测窗口")

    env.drivers = {"uia": NotAttached("uia"), "vision": NotAttached("vision")}
    env.rebuild()
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="ui.wait", args={"state": "gone"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].error["type"] == "locate_timeout"


def test_blocked_case_records_step_kb_hit(env):
    """KB 命中改判发生在轨迹落库之前：step_trace.step_kb_hit 必须非空
    （旧实现先落库后改判，整列恒为 NULL）。"""
    sig = FailureSignature("locate_timeout", "ui.click", "btn.start_detect", "main")
    entry_id = env.kb.save_failure(FailureKnowledge(
        entry_id=0, signature=sig.digest(), category="uia_change",
        disposition="block_known", suggestion="更新 DSL", confidence=0.9,
        error_type=sig.error_type, action=sig.action,
        target_key=sig.target_key, page_key=sig.page_key))
    env.kb.confirm(entry_id, "active")
    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "blocked"
    trace = repos.case_steps(env.db, ce)
    assert trace[0]["step_kb_hit"] == entry_id


def test_fuzzy_known_failure_does_not_auto_block(env):
    """模糊命中是"候选建议"（设计 9.2：仍需人审），不得像精确命中那样
    自动把用例改判 blocked —— 只有精确命中才触发自动处置。"""
    near = FailureSignature("locate_timeout", "ui.click", "btn.start_detect_v2", "main")
    entry_id = env.kb.save_failure(FailureKnowledge(
        entry_id=0, signature=near.digest(), category="uia_change",
        disposition="block_known", suggestion="控件改名了", confidence=0.95,
        error_type=near.error_type, action=near.action,
        target_key=near.target_key, page_key=near.page_key))
    env.kb.confirm(entry_id, "active")

    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "fail"                     # 不是 blocked
    assert result.root_cause["source"] == "kb_fuzzy"
    assert result.root_cause["needs_review"] is True


def test_kb_skip_disposition_skips_case(env):
    """处置策略 skip（人审：已知失败无需执行）：命中后用例按 skip 收尾。"""
    sig = FailureSignature("locate_timeout", "ui.click", "btn.start_detect", "main")
    entry_id = env.kb.save_failure(FailureKnowledge(
        entry_id=0, signature=sig.digest(), category="uia_change",
        disposition="skip", suggestion="已知失败，本轮跳过", confidence=1.0,
        error_type=sig.error_type, action=sig.action,
        target_key=sig.target_key, page_key=sig.page_key))
    env.kb.confirm(entry_id, "active")
    env.drivers["uia"].fail_locate = True
    env.drivers["vision"].fail_locate = True
    case = make_case("TC-1", steps=[make_step(id="s2")])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)

    assert result.status == "skip"
    row = env.db.execute("SELECT status FROM case_exec WHERE id=?", (ce,)).fetchone()
    assert row["status"] == "skip"
    trace = repos.case_steps(env.db, ce)
    assert trace[0]["step_kb_hit"] == entry_id


def test_assert_step_rejects_screenshot_diff(env):
    """assert 步骤没有 before 截图：screenshot_diff 断言必须显式失败，
    不能静默"跳过=通过"造成假绿。"""
    case = make_case("TC-1", steps=[
        make_step(id="s1", action="assert.ui",
                  verify={"type": "screenshot_diff"})])
    run_id, ce, ctx = _start(env, case)
    result = env.executor.run_case(case, ctx, None)
    assert result.status == "fail"
    assert result.steps[0].error["type"] == "dsl_error"

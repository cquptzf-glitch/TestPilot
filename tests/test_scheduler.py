"""定时回归调度单测（M4-4 的调度内核）。"""
import pytest

from testpilot.app.scheduler import Scheduler


class StubOrch:
    def __init__(self, stats=None, raises=False):
        self.calls = []
        self.stats = stats if stats is not None else {"status": "completed",
                                                      "run_id": 7, "pass": 1}
        self.raises = raises

    def run_task(self, task_id, run_cfg=None, writeback=True):
        self.calls.append(task_id)
        if self.raises:
            raise RuntimeError("boom")
        return dict(self.stats)


def test_add_validates_interval():
    sched = Scheduler(StubOrch())
    assert sched.add(1, 30).interval_min == 30
    with pytest.raises(ValueError):
        sched.add(2, 0)


def test_run_once_executes_all_entries():
    orch = StubOrch()
    sched = Scheduler(orch)
    sched.add(1, 60)
    sched.add(2, 60)
    results = sched.run_once()
    assert orch.calls == [1, 2]
    assert results == [
        {"task_id": 1, "status": "completed", "duration_s": results[0]["duration_s"],
         "run": 7, "runs": 1},
        {"task_id": 2, "status": "completed", "duration_s": results[1]["duration_s"],
         "run": 7, "runs": 1},
    ]
    assert sched.status()["entries"][0]["runs"] == 1


def test_run_due_respects_interval():
    orch = StubOrch()
    sched = Scheduler(orch)
    entry = sched.add(1, 60)
    entry.last_run_at = 0                      # 视为"很久没跑" -> 到期
    assert len(sched.run_due()) == 1
    # 刚跑过 -> 未到期
    import time
    entry.last_run_at = time.time()
    assert sched.run_due() == []
    assert orch.calls == [1]


def test_failure_in_one_entry_does_not_stop_others():
    orch = StubOrch(raises=True)
    sched = Scheduler(orch)
    sched.add(1, 10)
    sched.add(2, 10)
    results = sched.run_once()
    assert [r["status"] for r in results] == ["error", "error"]
    assert orch.calls == [1, 2]


def test_on_cycle_callback_receives_result_and_stats():
    orch = StubOrch()
    seen = []
    sched = Scheduler(orch, on_cycle=lambda result, stats: seen.append((result, stats)))
    sched.add(3, 10)
    sched.run_once()
    assert seen[0][0]["task_id"] == 3
    assert seen[0][1]["status"] == "completed"


def test_on_cycle_exception_is_swallowed():
    def boom(result, stats):
        raise RuntimeError("callback failed")

    sched = Scheduler(StubOrch(), on_cycle=boom)
    sched.add(1, 10)
    assert sched.run_once()[0]["status"] == "completed"


def test_load_entries_from_specs():
    sched = Scheduler(StubOrch())
    sched.load([{"task_id": 1, "interval_min": 15}, {"task_id": 2}])
    assert [(e.task_id, e.interval_min) for e in sched.entries] == [(1, 15), (2, 60)]


def test_run_forever_requires_entries():
    with pytest.raises(ValueError):
        Scheduler(StubOrch()).run_forever()


def test_run_forever_returns_immediately_after_stop():
    sched = Scheduler(StubOrch())
    sched.add(1, 60)
    sched.stop()
    sched.run_forever(poll_seconds=0.01)       # 已停止 -> 立即返回
    assert sched.status()["running"] is False


def test_status_snapshot_shape():
    sched = Scheduler(StubOrch())
    sched.add(1, 45)
    status = sched.status()
    assert status["running"] is True
    assert status["entries"][0]["interval_min"] == 45
    assert status["entries"][0]["last_status"] is None

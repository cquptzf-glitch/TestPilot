"""定时回归调度 — 可独立于桌面壳运行（设计 2.3 / M4-4）。

形态：
- ``run_once()``      按到期项跑一轮后返回（系统计划任务 / cron 直接调 CLI 即可）；
- ``run_forever()``   常驻轮询（夜间无人值守回归），Ctrl-C / ``stop()`` 安全退出。

调度语义：每个任务配置 ``interval_min`` 间隔；到期即执行，执行期间不并发（同一
编排器串行跑任务，避免抢占被测软件与模拟器资源）。一次 run 结束后把结果追加到
``history``，并支持 ``on_cycle`` 回调（壳可据此推送/落盘）。
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("testpilot.scheduler")

POLL_SECONDS = 10
#: 常驻模式保留的历史条数上限：调度器可能跑数周，无界追加会慢慢吃光内存
HISTORY_KEEP = 50


@dataclass
class ScheduleEntry:
    task_id: int
    interval_min: int
    last_run_at: float = 0.0          # time.time() 基准
    last_status: str | None = None
    runs: int = 0
    stats: list = field(default_factory=lambda: deque(maxlen=HISTORY_KEEP))

    def due(self, now: float) -> bool:
        return now - self.last_run_at >= self.interval_min * 60


class Scheduler:
    def __init__(self, orchestrator, entries=None, on_cycle=None):
        self.orchestrator = orchestrator
        self.entries: list[ScheduleEntry] = list(entries or [])
        self.on_cycle = on_cycle
        self._stop = threading.Event()

    # ---------- 配置 ----------

    def add(self, task_id: int, interval_min: int) -> ScheduleEntry:
        if interval_min <= 0:
            raise ValueError("interval_min 必须为正数")
        existing = next((e for e in self.entries if e.task_id == int(task_id)),
                        None)
        if existing is not None:
            # 重复登记同一任务会真的重复执行（占住被测软件/模拟器并互相踩轨迹）
            log.warning(f"任务 #{task_id} 已登记，忽略重复登记")
            return existing
        entry = ScheduleEntry(task_id=int(task_id), interval_min=int(interval_min))
        self.entries.append(entry)
        log.info(f"已登记定时回归: task#{entry.task_id} 每 {interval_min} 分钟")
        return entry

    def load(self, specs: list[dict]) -> None:
        """``[{"task_id": 1, "interval_min": 120}, ...]``"""
        for spec in specs:
            self.add(spec["task_id"], spec.get("interval_min", 60))

    # ---------- 执行 ----------

    def run_due(self) -> list[dict]:
        """跑一轮到期的任务，返回本轮结果。"""
        now = time.time()
        results = []
        for entry in self.entries:
            if self._stop.is_set():
                break
            if not entry.due(now):
                continue
            results.append(self._run_entry(entry))
        return results

    def run_once(self) -> list[dict]:
        """立即执行全部登记任务一次（忽略间隔，供 cron/CI 使用）。"""
        return [self._run_entry(e) for e in self.entries if not self._stop.is_set()]

    def _run_entry(self, entry: ScheduleEntry) -> dict:
        started = time.time()
        log.info(f"定时回归开始: task#{entry.task_id}")
        try:
            stats = self.orchestrator.run_task(entry.task_id)
            status = stats.get("status", "unknown")
        except Exception as e:      # 单个任务失败不影响后续调度
            log.exception(f"定时回归失败: task#{entry.task_id}")
            stats, status = {"error": str(e)}, "error"
        entry.last_run_at = time.time()
        entry.last_status = status
        entry.runs += 1
        entry.stats.append(stats)
        result = {"task_id": entry.task_id, "status": status,
                  "duration_s": round(entry.last_run_at - started, 1),
                  "run": stats.get("run_id"), "runs": entry.runs}
        log.info(f"定时回归结束: task#{entry.task_id} -> {status}")
        if self.on_cycle:
            try:
                self.on_cycle(result, stats)
            except Exception:
                log.exception("on_cycle 回调异常")
        return result

    # ---------- 常驻 ----------

    def run_forever(self, poll_seconds: int = POLL_SECONDS) -> None:
        """常驻轮询直到 ``stop()``（或 KeyboardInterrupt）。"""
        if not self.entries:
            raise ValueError("未登记任何定时任务")
        poll = max(1, int(poll_seconds))    # <=0 会让 Event.wait 退化成忙等
        log.info(f"调度器启动，共 {len(self.entries)} 个任务")
        try:
            while not self._stop.is_set():
                self.run_due()
                self._stop.wait(poll)
        except KeyboardInterrupt:
            log.info("调度器收到中断信号")
        finally:
            log.info("调度器已停止")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "running": not self._stop.is_set(),
            "entries": [{"task_id": e.task_id, "interval_min": e.interval_min,
                         "runs": e.runs, "last_status": e.last_status,
                         "last_run_at": e.last_run_at} for e in self.entries],
        }

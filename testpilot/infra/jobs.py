"""Bounded background case operations; health/read calls never wait on model IO."""
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from testpilot.core.errors import AbortRequested, TestPilotError
from testpilot.infra.abort import AbortSignal

CASE_METHODS = frozenset({"case.pull", "case.sync", "case.parse", "change.svn.generate"})


class CaseJobs:
    def __init__(self, runner):
        self._runner = runner
        self._lock = threading.RLock()
        self._entries = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="case-job")

    def start(self, method, params):
        if method not in CASE_METHODS:
            raise TestPilotError("后台任务仅支持用例拉取/同步/解析与 SVN 变更生成")
        if not isinstance(params, dict):
            raise TestPilotError("后台任务参数必须为对象")
        with self._lock:
            if any(e["status"] in ("queued", "running") for e in self._entries.values()):
                raise TestPilotError("已有用例后台任务，请等待完成或取消")
            while len(self._entries) >= 100:
                self._entries.pop(next(iter(self._entries)))
            job_id = uuid.uuid4().hex
            entry = {"id": job_id, "method": method, "params": copy.deepcopy(params),
                     "status": "queued", "progress": {}, "created_at": time.time(),
                     "abort": AbortSignal(), "result": None, "error": None}
            self._entries[job_id] = entry
            self._pool.submit(self._work, entry)
            return self.status(job_id)

    def _work(self, entry):
        with self._lock:
            entry["status"] = "running"
        try:
            entry["abort"].check()
            def progress(value):
                with self._lock:
                    entry["progress"].update(copy.deepcopy(value))
            result = self._runner(entry["method"], entry["params"], entry["abort"], progress)
            with self._lock:
                entry["result"] = result
                entry["status"] = "completed"
        except AbortRequested:
            with self._lock:
                entry["result"] = copy.deepcopy(entry["progress"].get("result"))
                entry["status"] = "cancelled"
                entry["error"] = "任务已取消；已完成的同步和草稿保留"
        except Exception as exc:
            with self._lock:
                entry["status"] = "failed"
                entry["error"] = str(exc)
        finally:
            with self._lock:
                entry["finished_at"] = time.time()

    def status(self, job_id=None):
        with self._lock:
            if job_id is None:
                return [self._view(e) for e in reversed(list(self._entries.values()))]
            if job_id not in self._entries:
                raise TestPilotError("后台任务不存在或引擎已重启，请重新加载任务列表")
            return self._view(self._entries[job_id])

    @staticmethod
    def _view(entry):
        return copy.deepcopy({key: value for key, value in entry.items()
                              if key not in ("abort", "params")})

    def cancel(self, job_id):
        with self._lock:
            self.status(job_id)
            self._entries[job_id]["abort"].request()
            return self.status(job_id)

    def cancel_all(self):
        with self._lock:
            for entry in self._entries.values():
                entry["abort"].request()

    def close(self):
        self.cancel_all()
        self._pool.shutdown(wait=False)

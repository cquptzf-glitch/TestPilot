"""协作取消 — 全链路 AbortSignal（设计文档 8.12）。
执行循环/驱动操作/模拟器指令在检查点周期检查；中止后 run 标记 aborted，
已完成轨迹全部保留（不丢数据）。
"""
import threading

from testpilot.core.errors import AbortRequested


class AbortSignal:
    """传给 ExecContext 的检查器；底层共享一个 threading.Event。"""

    def __init__(self, flag: threading.Event | None = None):
        self._flag = flag or threading.Event()

    def request(self) -> None:
        self._flag.set()

    @property
    def requested(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        """在检查点调用；已请求取消时抛 AbortRequested 中断当前操作。"""
        if self._flag.is_set():
            raise AbortRequested("收到中止请求")

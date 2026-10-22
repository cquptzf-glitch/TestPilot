"""领域事件总线 — 引擎内部发事件，RPC 层订阅后推给壳（设计 2.5 事件通道）。

CLI 形态下无订阅者，emit 为廉价空转；壳形态下 `RpcService` 订阅并把
`core/events.py` 的领域事件序列化后广播到 WebSocket。
订阅方回调抛异常不得影响执行主干（隔离 + 记日志）。
"""
import logging
from typing import Callable

from testpilot.core.events import DomainEvent

log = logging.getLogger("testpilot.events")


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[DomainEvent], None]] = []

    def subscribe(self, handler: Callable[[DomainEvent], None]) -> Callable[[], None]:
        """注册订阅者，返回退订函数。"""
        self._subs.append(handler)

        def _unsubscribe() -> None:
            if handler in self._subs:
                self._subs.remove(handler)

        return _unsubscribe

    def emit(self, evt: DomainEvent) -> None:
        for handler in list(self._subs):
            try:
                handler(evt)
            except Exception:  # 订阅方故障不得打断执行
                log.exception(f"事件订阅者异常: {evt.event}")

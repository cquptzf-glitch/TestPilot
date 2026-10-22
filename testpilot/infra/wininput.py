"""Windows 物理输入小封装 — 仅承载鼠标滚轮/拖拽的确定性原语。

模块级不导入 pywin32，保证非 Windows / 无 GUI 环境仍可 import 与跑单测。
坐标统一使用物理像素；调用方负责窗口/目标边界校验。
"""
from __future__ import annotations

import time
from typing import Any


def scroll_at(x: int, y: int, direction: str, amount: int,
              abort: Any = None) -> None:
    """在指定屏幕坐标发送滚轮。

    amount 是滚轮“格数”，不是像素。方向语义：
    up/down 为垂直滚轮，left/right 为水平滚轮。
    """
    direction = str(direction).lower()
    amount = int(amount)
    if direction not in ("up", "down", "left", "right"):
        raise ValueError(f"未知滚动方向: {direction}")
    if amount <= 0:
        raise ValueError("滚动 amount 必须大于 0")
    if abort is not None:
        abort.check()

    import win32api
    import win32con

    win32api.SetCursorPos((int(x), int(y)))
    wheel_delta = 120 * amount
    if direction in ("up", "down"):
        data = wheel_delta if direction == "up" else -wheel_delta
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, int(x), int(y), data, 0)
    else:
        flag = getattr(win32con, "MOUSEEVENTF_HWHEEL", 0x01000)
        data = wheel_delta if direction == "right" else -wheel_delta
        win32api.mouse_event(flag, int(x), int(y), data, 0)


def drag_from(x: int, y: int, dx: int, dy: int, duration_ms: int = 300,
              abort: Any = None) -> tuple[int, int]:
    """从 (x,y) 按住左键拖到相对偏移 (dx,dy)，返回终点坐标。

    移动分片执行，以便协作取消；无论中途发生什么都保证释放左键。
    """
    x, y, dx, dy = int(x), int(y), int(dx), int(dy)
    duration_ms = int(duration_ms)
    if dx == 0 and dy == 0:
        raise ValueError("拖拽 dx/dy 不能同时为 0")
    if duration_ms < 0:
        raise ValueError("拖拽 duration_ms 不能小于 0")
    if abort is not None:
        abort.check()

    import win32api
    import win32con

    end_x, end_y = x + dx, y + dy
    steps = max(2, min(60, duration_ms // 16 if duration_ms else 2))
    pause = duration_ms / 1000 / steps if duration_ms else 0

    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    try:
        for index in range(1, steps + 1):
            if abort is not None:
                abort.check()
            px = round(x + dx * index / steps)
            py = round(y + dy * index / steps)
            win32api.SetCursorPos((px, py))
            if pause:
                time.sleep(pause)
    finally:
        # 即使 AbortRequested/异常，也不能把系统鼠标留在“按住”状态。
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, end_x, end_y, 0, 0)
    return end_x, end_y


def right_click_at(x: int, y: int, abort: Any = None) -> None:
    """在指定物理像素坐标发送一次右键点击。"""
    if abort is not None:
        abort.check()
    import win32api
    import win32con

    x, y = int(x), int(y)
    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, x, y, 0, 0)
    try:
        if abort is not None:
            abort.check()
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, x, y, 0, 0)

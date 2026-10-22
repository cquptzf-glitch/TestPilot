"""双驱动路由 — 决策与反馈分离（设计 8.2 / 4.5）。

策略三级覆盖：**global（cfg.driver.mode） < case（方案/任务 driver_config） < step（DSL step.driver）**。
决策顺序：
1. 解析模式 -> 基础顺序（auto/uia_first -> UIA→视觉；vision_first -> 视觉→UIA；*_only -> 单驱动）
2. `target.vision_only_hint` 或知识库 stale 标记 -> 剔除 UIA
3. `router_stats` 中 UIA 连续失败 >= N -> 视觉提前（UIA 仍留队尾兜底）
"""
import logging
from dataclasses import dataclass

from testpilot.core.dsl import DriverMode, Step

log = logging.getLogger("testpilot.router")


@dataclass
class DriverPlan:
    attempts: list[str]      # 有序驱动名列表


class Router:
    def __init__(self, cfg: dict, stats_loader, fail_streak_n: int = 3):
        self.cfg = cfg or {}
        self._load_stats = stats_loader   # callable(target_key) -> dict[driver, StatsRow]
        self.fail_streak_n = fail_streak_n

    def decide(self, step: Step, hints) -> DriverPlan:
        mode = self._resolve_mode(step, hints)
        attempts = self._base_order(mode)
        target = step.target

        # vision-only 提示（DSL 注解 或 知识库 stale 标记）-> 剔除 UIA
        if target and (target.vision_only_hint
                       or self._kb_marks_vision_only(hints, target.key)):
            attempts = [a for a in attempts if a != "uia"] or ["vision"]

        # 学习闭环：UIA 连续失败 >= N -> 视觉提前（UIA 移至队尾，仍可兜底）
        if target and "uia" in attempts and self._load_stats is not None:
            stats = self._load_stats(target.key) or {}
            uia = stats.get("uia")
            if uia is not None and getattr(uia, "fail_streak", 0) >= self.fail_streak_n:
                attempts = [a for a in attempts if a != "uia"] + ["uia"]
                log.info("路由学习: UIA 连续失败，视觉提前",
                         extra={"target_key": target.key,
                                "count": uia.fail_streak})

        return DriverPlan(attempts=_dedup(attempts))

    # ---------- 内部 ----------

    def _base_order(self, mode: DriverMode) -> list[str]:
        if mode == DriverMode.UIA_ONLY:
            return ["uia"]
        if mode == DriverMode.VISION_ONLY:
            return ["vision"]
        if mode == DriverMode.VISION_FIRST:
            return ["vision", "uia"]
        return ["uia", "vision"]   # auto / uia_first

    def _resolve_mode(self, step: Step, hints) -> DriverMode:
        """三级覆盖：step.driver > hints.driver_mode（方案级） > cfg.driver.mode。"""
        if step.driver is not None:
            return step.driver
        case_mode = getattr(hints, "driver_mode", None) if hints else None
        raw = case_mode or self.cfg.get("mode", "auto")
        try:
            return DriverMode(raw)
        except ValueError:
            log.warning(f"未知驱动模式 {raw!r}，回退 auto")
            return DriverMode.AUTO

    def _kb_marks_vision_only(self, hints, target_key: str) -> bool:
        if not hints:
            return False
        for ek in getattr(hints, "elements", []) or []:
            if getattr(ek, "key", None) == target_key and \
                    getattr(ek, "prefer_vision", False):
                return True
        return False


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out

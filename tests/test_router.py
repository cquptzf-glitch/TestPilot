"""路由决策单测 — 三级覆盖 + 知识库提示 + 学习重排（设计 8.2 / 4.5）。"""
from testpilot.engine.router import Router
from testpilot.knowledge.service import CaseHints, ElementKnowledge, StatsRow


def _step(mode=None, vision_only=False, key="btn.start"):
    step = {
        "id": "s1", "action": "ui.click",
        "target": {"key": key,
                   "primary": {"type": "uia", "name": "开始"},
                   "fallback": {"type": "visual", "desc": "开始按钮"},
                   "vision_only_hint": vision_only},
    }
    if mode:
        step["driver"] = mode
    from testpilot.core.dsl import Step
    return Step.model_validate(step)


def _router(mode="auto", stats=None, streak=3):
    """stats 传 dict 表示"任意 key 都返回该统计"；传 callable 表示按 key 取（更真实）。"""
    loader = stats if callable(stats) else (lambda key: stats or {})
    return Router({"mode": mode}, loader, fail_streak_n=streak)


def test_auto_and_uia_first_prefer_uia():
    assert _router("auto").decide(_step(), None).attempts == ["uia", "vision"]
    assert _router("uia_first").decide(_step(), None).attempts == ["uia", "vision"]


def test_vision_first_and_only_modes():
    assert _router("vision_first").decide(_step(), None).attempts == ["vision", "uia"]
    assert _router("uia_only").decide(_step(), None).attempts == ["uia"]
    assert _router("vision_only").decide(_step(), None).attempts == ["vision"]


def test_step_level_driver_overrides_global():
    plan = _router("auto").decide(_step(mode="vision_only"), None)
    assert plan.attempts == ["vision"]


def test_case_level_driver_mode_overrides_global_but_not_step():
    hints = CaseHints(driver_mode="vision_first")
    assert _router("auto").decide(_step(), hints).attempts == ["vision", "uia"]
    # step 优先于 case
    assert _router("auto").decide(_step(mode="uia_only"), hints).attempts == ["uia"]


def test_unknown_mode_falls_back_to_auto():
    assert _router("nonsense").decide(_step(), None).attempts == ["uia", "vision"]


def test_vision_only_hint_drops_uia():
    assert _router("auto").decide(_step(vision_only=True), None).attempts == ["vision"]


def test_kb_prefer_vision_drops_uia():
    hints = CaseHints(elements=[ElementKnowledge(key="btn.start", prefer_vision=True)])
    assert _router("auto").decide(_step(), hints).attempts == ["vision"]
    # 其它 key 不受影响
    other = CaseHints(elements=[ElementKnowledge(key="btn.other", prefer_vision=True)])
    assert _router("auto").decide(_step(), other).attempts == ["uia", "vision"]


def test_vision_only_hint_respects_only_modes():
    # uia_only 模式 + vision_only_hint：hint 只剔除 uia，兜底回 vision
    assert _router("uia_only").decide(_step(vision_only=True), None).attempts == ["vision"]


def test_fail_streak_reorders_to_vision_first():
    stats = {"uia": StatsRow("uia", 0, 3, 3, 100)}
    assert _router("auto", stats).decide(_step(), None).attempts == ["vision", "uia"]
    # 未达阈值则不动
    weak = {"uia": StatsRow("uia", 0, 2, 2, 100)}
    assert _router("auto", weak).decide(_step(), None).attempts == ["uia", "vision"]


def test_fail_streak_only_affects_matching_target_key():
    """学习统计按 target_key 隔离：别的元素失败再多也不影响本步骤。"""
    stats = {"uia": StatsRow("uia", 0, 5, 5, 1)}
    router = _router("auto", lambda key: stats if key == "btn.start" else {})
    assert router.decide(_step(key="btn.other"), None).attempts == ["uia", "vision"]
    assert router.decide(_step(key="btn.start"), None).attempts == ["vision", "uia"]


def test_no_target_step_returns_base_order():
    from testpilot.core.dsl import Step
    assert _router("auto").decide(Step(id="s1", action="sleep"), None).attempts == [
        "uia", "vision"]


def test_stats_loader_none_is_tolerated():
    router = Router({"mode": "auto"}, None)
    assert router.decide(_step(), None).attempts == ["uia", "vision"]


def test_dedup_keeps_order():
    stats = {"uia": StatsRow("uia", 0, 3, 3, 1)}
    plan = _router("vision_first", stats).decide(_step(), None)
    assert plan.attempts == ["vision", "uia"]

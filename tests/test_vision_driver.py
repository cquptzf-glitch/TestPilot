"""视觉驱动单测 — L1 模板匹配 / L2 VLM 定位 / L2→L1 模板沉淀 / 截图通道（设计 8.4、M3-3）。

真实截图与桌面会话无法进 CI，但**匹配算法、坐标换算、置信度门禁、通道序**都可以
用合成图像完整覆盖：这里用 numpy 造图 + 假 mss/PIL/llm 替身。
"""
import sys
import types
from pathlib import Path

import pytest

from testpilot.core.contracts import ExecContext, LocatedElement
from testpilot.core.dsl import Locator
from testpilot.core.errors import ActionError, LocateError
from testpilot.infra.abort import AbortSignal

pytest.importorskip("cv2")
pytest.importorskip("numpy")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from testpilot.drivers import vision_driver  # noqa: E402
from testpilot.drivers.vision_driver import (VisionDriver, grab_screen,  # noqa: E402
                                            sanitize_key)


class FakeLlm:
    def __init__(self, ground=None):
        self.ground_result = ground or {"bbox": [5, 6, 20, 10], "confidence": 0.9}
        self.ground_calls = []

    def ground(self, image_b64, desc, region):
        self.ground_calls.append({"desc": desc, "region": region,
                                  "b64_len": len(image_b64)})
        return dict(self.ground_result)


def _window_image(w=120, h=80):
    """白底 + 带纹理的方块（可被模板匹配唯一命中）。

    **不能只画纯色块**：``TM_CCOEFF_NORMED`` 是去均值归一化相关，模板方差为 0
    时结果无定义（OpenCV 会给出全 0/NaN，命中位置退化为 (0,0)）。因此方块里
    再画两个角标，保证模板有方差、匹配位置唯一。
    """
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    img[30:40, 40:70] = (0, 0, 200)          # BGR 红块
    img[30:34, 40:44] = (220, 220, 0)        # 左上角标
    img[36:40, 66:70] = (0, 200, 0)          # 右下角标
    return img


@pytest.fixture()
def driver(tmp_path):
    return VisionDriver({"template_dir": str(tmp_path / "templates"),
                         "locate_poll_ms": 1}, llm=FakeLlm())


def _ctx(window_rect=None):
    return ExecContext(run_id=1, case_exec_id=1, window_rect=window_rect)


def _pin_screen(driver, img):
    """把驱动的取屏替换为固定图像（避免真截图）。"""
    driver._grab_bgr = lambda rect: img


# ---------------------------------------------------------------- 工具

def test_sanitize_key_keeps_unicode_and_strips_separators():
    assert sanitize_key("主界面/底部:开始检测按钮") == "主界面_底部_开始检测按钮"
    assert sanitize_key("btn.start-detect") == "btn.start-detect"
    assert sanitize_key("///") == "unnamed"
    assert len(sanitize_key("x" * 200)) == 80


def test_health_ok_with_cv2(driver):
    rep = driver.health()
    assert rep.ok is True and "opencv" in rep.detail


def test_health_warns_when_vlm_missing(tmp_path):
    rep = VisionDriver({"template_dir": str(tmp_path)}, llm=None).health()
    assert rep.ok is True and "VLM 未装配" in rep.detail


# ---------------------------------------------------------------- 模板库

def test_learn_template_writes_file_and_returns_relative_name(driver):
    name = driver.learn_template("btn start", _window_image()[30:40, 40:70])
    assert name == "btn_start.png"
    assert (driver.template_dir / name).exists()


def test_learned_template_none_when_absent(driver):
    assert driver.learned_template("没学过") is None
    assert driver.learned_template(None) is None


def test_learned_template_found_after_learning(driver):
    driver.learn_template("开始检测", _window_image()[30:40, 40:70])
    assert driver.learned_template("开始检测") == "开始检测.png"


# ---------------------------------------------------------------- L1 模板匹配

def test_locate_template_hits_and_maps_to_screen_coords(driver):
    tpl = _window_image()[30:40, 40:70]
    driver.learn_template("blk", tpl)
    _pin_screen(driver, _window_image())

    el = driver.locate(Locator.model_validate(
        {"type": "template", "template": "blk.png"}), _ctx(), timeout_ms=200)
    assert el.driver.value == "template"
    assert el.rect == (40, 30, 30, 10)          # 命中位置 + 原点(0,0)
    assert el.confidence > 0.99

    # 带窗口原点 -> 结果整体平移
    el2 = driver.locate(Locator.model_validate(
        {"type": "template", "template": "blk.png"}), _ctx((100, 50, 120, 80)),
        timeout_ms=200)
    assert el2.rect == (140, 80, 30, 10)


def test_locate_template_missing_file_raises(driver):
    with pytest.raises(LocateError) as e:
        driver.locate(Locator.model_validate(
            {"type": "template", "template": "nope.png"}), _ctx(), timeout_ms=10)
    assert "模板不存在" in str(e.value)


def test_locate_template_requires_path(driver):
    with pytest.raises(LocateError):
        driver.locate(Locator.model_validate({"type": "template"}), _ctx(),
                      timeout_ms=10)


def _noise(w=10, h=10, seed=0):
    """确定性噪声模板：保证在简单合成图上匹配分低于阈值。"""
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


def test_locate_template_below_threshold_times_out(driver):
    driver.learn_template("noise", _noise())
    _pin_screen(driver, _window_image())
    with pytest.raises(LocateError) as e:
        driver.locate(Locator.model_validate(
            {"type": "template", "template": "noise.png"}), _ctx(), timeout_ms=30)
    assert "匹配分不足" in str(e.value) or "超时" in str(e.value)


def test_locate_template_region_smaller_than_template(driver):
    driver.learn_template("big", _noise(50, 50))
    _pin_screen(driver, _window_image())
    with pytest.raises(LocateError):
        driver.locate(Locator.model_validate(
            {"type": "template", "template": "big.png", "region": [0, 0, 10, 10]}),
            _ctx(), timeout_ms=10)


def test_locate_template_with_region_offsets_result(driver):
    driver.learn_template("blk", _window_image()[30:40, 40:70])
    _pin_screen(driver, _window_image())
    el = driver.locate(Locator.model_validate(
        {"type": "template", "template": "blk.png", "region": [30, 20, 90, 60]}),
        _ctx(), timeout_ms=200)
    assert el.rect == (40, 30, 30, 10)          # region 偏移 + 命中位置


def test_locate_rejects_unknown_locator_type(driver):
    loc = Locator.model_validate({"type": "uia", "name": "x"})
    with pytest.raises(LocateError):
        driver.locate(loc, _ctx())


# ---------------------------------------------------------------- L2 VLM

def test_locate_vlm_maps_bbox_with_origin_and_region(driver):
    _pin_screen(driver, _window_image())
    loc = Locator.model_validate({"type": "visual", "desc": "开始检测按钮"})
    el = driver.locate(loc, _ctx((100, 50, 120, 80)), timeout_ms=200)
    assert el.driver.value == "vision"
    assert el.rect == (105, 56, 20, 10)         # origin + bbox
    assert el.confidence == 0.9
    assert driver.llm.ground_calls[0]["desc"] == "开始检测按钮"


def test_locate_vlm_region_offsets_bbox(driver):
    _pin_screen(driver, _window_image())
    loc = Locator.model_validate({"type": "visual", "desc": "x", "region": [20, 10, 60, 40]})
    el = driver.locate(loc, _ctx(), timeout_ms=200)
    assert el.rect == (25, 16, 20, 10)          # region 偏移 + bbox


def test_locate_vlm_low_confidence_raises(driver):
    driver.llm.ground_result = {"bbox": [1, 2, 3, 4], "confidence": 0.3}
    _pin_screen(driver, _window_image())
    with pytest.raises(LocateError) as e:
        driver.locate(Locator.model_validate({"type": "visual", "desc": "x"}), _ctx(),
                      timeout_ms=200)
    assert "置信度不足" in str(e.value)


def test_locate_vlm_rejects_bad_bbox(driver):
    driver.llm.ground_result = {"bbox": [1, 2], "confidence": 0.99}
    _pin_screen(driver, _window_image())
    with pytest.raises(LocateError):
        driver.locate(Locator.model_validate({"type": "visual", "desc": "x"}), _ctx(),
                      timeout_ms=200)


def test_locate_vlm_requires_llm(tmp_path):
    d = VisionDriver({"template_dir": str(tmp_path)}, llm=None)
    d._grab_bgr = lambda rect: _window_image()
    with pytest.raises(LocateError) as e:
        d.locate(Locator.model_validate({"type": "visual", "desc": "x"}), _ctx(),
                 timeout_ms=200)
    assert "VLM 未装配" in str(e.value)


def test_locate_visual_prefers_learned_template(driver):
    """L2->L1 毕业：同一 desc 已学模板时不再调用 VLM（零 token）。"""
    driver.learn_template("开始检测按钮", _window_image()[30:40, 40:70])
    _pin_screen(driver, _window_image())
    el = driver.locate(Locator.model_validate({"type": "visual",
                                              "desc": "开始检测按钮"}),
                       _ctx(), timeout_ms=200)
    assert el.driver.value == "template"
    assert driver.llm.ground_calls == []        # 未花 token


def test_locate_visual_falls_back_to_vlm_when_template_misses(driver):
    driver.learn_template("开始检测按钮", _noise())
    _pin_screen(driver, _window_image())
    el = driver.locate(Locator.model_validate({"type": "visual",
                                              "desc": "开始检测按钮"}),
                       _ctx(), timeout_ms=30)
    assert el.driver.value == "vision"
    assert len(driver.llm.ground_calls) == 1


# ---------------------------------------------------------------- L2 -> L1 沉淀

def test_learn_visual_template_crops_by_window_basis(driver):
    _pin_screen(driver, _window_image())
    name = driver.learn_visual_template("开始检测按钮", (40, 30, 30, 10),
                                        _ctx((0, 0, 120, 80)))
    assert name == "开始检测按钮.png"
    # 读回也要用 Unicode 安全通道（cv2.imread 在中文路径下会返回 None）
    saved = vision_driver._imread_unicode(cv2, np, driver.template_dir / name)
    assert saved.shape[:2] == (10, 30)          # 裁剪区域尺寸正确

    # 学完之后可直接被 L1 命中
    el = driver.locate(Locator.model_validate({"type": "visual",
                                              "desc": "开始检测按钮"}),
                       _ctx(), timeout_ms=200)
    assert el.driver.value == "template"


def test_learn_visual_template_without_window_basis(driver):
    _pin_screen(driver, _window_image())
    name = driver.learn_visual_template("块", (40, 30, 30, 10), _ctx())
    assert name == "块.png"


def test_learn_visual_template_outside_window_falls_back(driver):
    _pin_screen(driver, _window_image())
    name = driver.learn_visual_template("块", (500, 500, 10, 10),
                                        _ctx((0, 0, 120, 80)))
    assert name == "块.png"                     # 退化为直接区域截图，不抛错


def test_learn_visual_template_disabled_and_noop_cases(driver):
    assert driver.learn_visual_template(None, (0, 0, 1, 1), _ctx()) is None
    assert driver.learn_visual_template("x", None, _ctx()) is None
    driver.cfg["learn_from_vlm"] = False
    assert driver.learn_visual_template("x", (0, 0, 1, 1), _ctx()) is None


def test_learn_visual_template_swallows_grab_failure(driver):
    def boom(rect):
        raise RuntimeError("screen locked")

    driver._grab_bgr = boom
    assert driver.learn_visual_template("x", (0, 0, 4, 4), _ctx()) is None


# ---------------------------------------------------------------- 不支持的能力

def test_unsupported_operations_raise_action_error(driver):
    el = LocatedElement(driver=None, handle=(0, 0, 1, 1), rect=(0, 0, 1, 1))
    for call in (lambda: driver.select(el, "A", _ctx()),
                 lambda: driver.wait_gone(el, _ctx(), 1),
                 lambda: driver.read_text(el)):
        with pytest.raises(ActionError):
            call()
    assert driver.wait_ready(_ctx(), 1) is True


# ---------------------------------------------------------------- 截图（通道序）

class _FakeMSS:
    monitors = [{}, {"left": 0, "top": 0, "width": 4, "height": 2}]
    last_region = None

    def __init__(self, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, region):
        _FakeMSS.last_region = region
        # BGRA：B=10 G=20 R=30 A=255（2 行 × 4 列 × 4 通道）
        return np.tile(np.array([[[10, 20, 30, 255]]], dtype=np.uint8), (2, 4, 1))


def test_grab_screen_mss_keeps_bgr_channel_order(monkeypatch):
    mod = types.ModuleType("mss")
    mod.mss = _FakeMSS
    monkeypatch.setitem(sys.modules, "mss", mod)

    shot = grab_screen((5, 6, 4, 2))
    assert _FakeMSS.last_region == {"left": 5, "top": 6, "width": 4, "height": 2}
    bgr = cv2.imdecode(np.frombuffer(shot.png, np.uint8), cv2.IMREAD_COLOR)
    assert tuple(int(v) for v in bgr[0, 0]) == (10, 20, 30)   # 未被反转


def test_grab_screen_full_desktop_uses_virtual_screen(monkeypatch):
    """无矩形抓 monitors[0] = 整个虚拟桌面：monitors[1] 只有主屏，
    被测程序在副屏时会静默截错屏（launch 之前/关闭后的 assert 场景）。"""
    mod = types.ModuleType("mss")
    mod.mss = _FakeMSS
    monkeypatch.setitem(sys.modules, "mss", mod)
    grab_screen(None)
    assert _FakeMSS.last_region == _FakeMSS.monitors[0]


def test_grab_screen_pil_backend(monkeypatch):
    real_pil = sys.modules.get("PIL")

    class _Img:
        def convert(self, mode):
            assert mode == "RGB"
            return self

        def save(self, buf, format=None):
            assert format == "PNG"
            buf.write(b"PILPNG")

    class _ImageGrab:
        bbox = None

        @staticmethod
        def grab(bbox=None):
            _ImageGrab.bbox = bbox
            return _Img()

    pil = types.ModuleType("PIL")
    pil.ImageGrab = _ImageGrab
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", _ImageGrab)

    shot = grab_screen((1, 2, 3, 4), backend="pil")
    assert shot.png == b"PILPNG"
    # PIL 的 bbox 是 (left, top, right, bottom) —— 右/下为开区间
    assert _ImageGrab.bbox == (1, 2, 1 + 3, 2 + 4)
    if real_pil is None:
        sys.modules.pop("PIL", None)


def test_driver_grab_uses_configured_backend(driver, monkeypatch):
    seen = {}

    def fake_grab(rect, backend="mss"):
        from testpilot.core.contracts import Screenshot
        seen["backend"] = backend
        return Screenshot(png=b"X", rect=rect)

    monkeypatch.setattr(vision_driver, "grab_screen", fake_grab)
    driver.grab(None, _ctx())
    assert seen["backend"] == "mss"


# ---------------------------------------------------------------- DPI

def test_dpi_entry_delegates_to_infra_module():
    """DPI 感知/坐标归一收敛在 infra/dpi.py；驱动只保留同名入口。

    具体算法（125/150/200% 换算、感知回退链）由 tests/test_dpi.py 覆盖。
    """
    from testpilot.infra import dpi
    assert vision_driver._ensure_dpi_aware is dpi.ensure_dpi_aware


def test_window_rect_normalized_into_capture_space(monkeypatch):
    """窗口基准必须经 dpi 归一：非感知进程（截图=逻辑空间）下按缩放比折算。"""
    from testpilot.infra import dpi

    called = {}

    def fake_normalize(rect, source=None):
        called["rect"], called["source"] = rect, source
        return (10, 20, 100, 50)

    monkeypatch.setattr(dpi, "normalize_rect", fake_normalize)
    ctx = _ctx(window_rect=(15, 30, 150, 75))
    assert vision_driver.VisionDriver.window_rect(ctx) == (10, 20, 100, 50)
    assert called == {"rect": (15, 30, 150, 75), "source": "physical"}


def test_window_rect_none_is_passthrough(monkeypatch):
    from testpilot.infra import dpi
    monkeypatch.setattr(dpi, "normalize_rect", lambda r, s=None: r)
    assert vision_driver.VisionDriver.window_rect(_ctx(None)) is None


# ---------------------------------------------------------------- 工厂

def test_create_factory_wires_default_llm(monkeypatch, tmp_path):
    from testpilot.adapters import llm_gateway
    from testpilot.adapters.llm_gateway import LlmGateway

    fake = LlmGateway({}, transport=lambda p, i: ("{}", {}))
    monkeypatch.setattr(llm_gateway, "get_default_llm", lambda: fake)
    d = vision_driver.create({"template_dir": str(tmp_path)})
    assert d.llm is fake
    assert Path(d.template_dir).exists()


def test_manifest_declares_learning_config():
    from testpilot.drivers.vision_driver import MANIFEST
    props = MANIFEST.config_schema["properties"]
    assert "learn_from_vlm" in props
    assert "locate_poll_ms" in props
    assert props["grab_backend"]["enum"] == ["mss", "pil"]


def test_abort_propagates_in_locate(driver):
    import threading

    from testpilot.core.errors import AbortRequested

    event = threading.Event()
    event.set()
    ctx = ExecContext(run_id=1, case_exec_id=1, abort=AbortSignal(event))
    driver.learn_template("blk", _window_image()[30:40, 40:70])
    _pin_screen(driver, _window_image())
    with pytest.raises(AbortRequested):
        driver.locate(Locator.model_validate({"type": "template",
                                             "template": "blk.png"}),
                      ctx, timeout_ms=50)

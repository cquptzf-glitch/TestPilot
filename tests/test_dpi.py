"""DPI 感知与坐标空间归一单测（M3-4，设计 13 章「高 DPI 坐标错位」对策）。

覆盖要点：
- **125% / 150% / 200%** 三档缩放下的物理<->逻辑换算（这是实机错位的算错就全错的部分）；
- 感知开关的完整回退链（shcore 失败 -> user32 成功；两者都失败 -> 只告警）与幂等；
- ``capture_space()`` 的三种组合（感知/非感知+缩放/非感知+100%）；
- ``normalize_rect`` 的同空间零成本直通与跨空间折算；
- 非 Windows 平台、API 缺失、脏缩放值等降级路径。

全部通过注入假 ``windll`` 完成，不依赖真实显示器与 DPI。
"""
import ctypes
import sys
from types import SimpleNamespace

import pytest

from testpilot.infra import dpi

# EnumDisplayMonitors 的回调必须收到**真正的 ctypes RECT 指针**（模块内会 cast），
# 因此这里复用 dpi 模块自己构造的 RECT 类型，保证内存布局完全一致。
_Rect = dpi._MONITOR_RECT


class FakeWindll:
    """可编程的 windll 替身：仅实现 dpi 模块用到的少数 API。

    与真实 WinAPI 的对齐点（旧版本替身在这里编错了语义，导致缺陷漏检）：
    - ``SetProcessDpiAwareness`` / ``SetProcessDPIAware`` 失败以**返回值**表达
      （非零 HRESULT / BOOL FALSE），并不抛异常——``ctypes.windll`` 不检查 HRESULT；
    - 进程真实感知级别以 ``GetProcessDpiAwareness`` 读回为准（清单声明与先前
      调用都可能改写过）；
    - **非感知进程的 DPI 查询被 Windows 虚拟化成 96**：真实缩放比读不到。
    """

    def __init__(self, dpi_value=96, shcore_ok=True, user32_aware_ok=True,
                 have_get_dpi_for_system=True, have_gdi=True,
                 screen=(1920, 1080), monitors=None,
                 declared_awareness=None, virtualize_dpi=True):
        self.calls = []
        self.dpi_value = dpi_value
        self.screen = screen
        self.gdi_calls = []
        self.virtualize_dpi = virtualize_dpi
        # 进程实际感知级别：None = 清单未声明且尚无设置调用成功
        self._awareness = declared_awareness

        def _set_awareness(level):
            self.calls.append(("SetProcessDpiAwareness", level))
            if shcore_ok:
                if self._awareness is None:
                    self._awareness = level
                return 0                    # S_OK：windll 下成功只体现为返回 0
            return 0x80070005               # E_ACCESSDENIED：返回值表达失败

        def _set_aware():
            self.calls.append(("SetProcessDPIAware",))
            if user32_aware_ok:
                if self._awareness is None:
                    self._awareness = 1
                return 1                    # BOOL TRUE
            return 0

        def _get_awareness(_hprocess, pval):
            if self._awareness is None:
                return 0x80004005           # E_FAIL：无法判定（未声明未设置成功）
            pval.contents.value = self._awareness
            return 0

        def _dpi_for_system():
            # 真实语义：非感知进程的 DPI 查询被系统虚拟化成 96
            if self.virtualize_dpi and self._awareness == 0:
                return 96
            return self.dpi_value

        user32_ns = {
            "SetProcessDPIAware": _set_aware,
            "GetSystemMetrics": lambda i: self._metric(i),
            "GetDC": lambda _hwnd: 42,
            "ReleaseDC": lambda _hwnd, _dc: 1,
            "EnumDisplayMonitors": self._enum,
        }
        if have_get_dpi_for_system:
            user32_ns["GetDpiForSystem"] = _dpi_for_system

        gdi_ns = {"GetDeviceCaps": self._device_caps}

        self.shcore = SimpleNamespace(SetProcessDpiAwareness=_set_awareness,
                                      GetProcessDpiAwareness=_get_awareness)
        self.user32 = SimpleNamespace(**user32_ns)
        self.gdi32 = SimpleNamespace(**gdi_ns) if have_gdi else SimpleNamespace()
        self._monitors = monitors

    # -- 内部 API ---
    def _metric(self, index):
        return {
            0: self.screen[0], 1: self.screen[1],
            76: -1920, 77: 0, 78: 3840, 79: 1080,
        }.get(index, 0)

    def _device_caps(self, _dc, index):
        self.gdi_calls.append(index)
        if self.virtualize_dpi and self._awareness == 0:
            return 96                       # 同 GetDpiForSystem 的虚拟化语义
        return self.dpi_value

    def _enum(self, _hdc, _clip, callback, _data):
        # box 语义 = (left, top, right, bottom)：与 Win32 RECT 一致
        rects = self._monitors if self._monitors is not None else [
            (0, 0, 2560, 1440), (2560, 0, 4480, 1080)]
        # 持有 RECT 实例引用：ctypes 指针不延长对象生命周期
        keep = []
        for box in rects:
            if _Rect is None:
                return False            # 非 Windows：让 dpi 走虚拟桌面兜底
            obj = _Rect(*box)
            keep.append(obj)
            if not callback(0, 0, ctypes.pointer(obj), 0):
                break
        return True


@pytest.fixture(autouse=True)
def _clean_cache():
    dpi.reset_cache()
    yield
    dpi.reset_cache()


def _install(monkeypatch, fake):
    monkeypatch.setattr(dpi, "_windll", lambda: fake, raising=False)
    return fake


def _real_monitors_supported():
    return sys.platform == "win32"


# ---------------------------------------------------------------- 缩放比

@pytest.mark.parametrize("dpi_value,expected", [
    (96, 1.0),        # 100%
    (120, 1.25),      # 125%
    (144, 1.5),       # 150%
    (192, 2.0),       # 200%
    (240, 2.5),
])
def test_dpi_scale_matches_windows_presets(monkeypatch, dpi_value, expected):
    _install(monkeypatch, FakeWindll(dpi_value=dpi_value))
    assert dpi.dpi_scale() == expected


def test_dpi_scale_falls_back_to_gdi_device_caps(monkeypatch):
    fake = _install(monkeypatch, FakeWindll(
        dpi_value=144, have_get_dpi_for_system=False))
    assert dpi.dpi_scale() == 1.5
    assert fake.gdi_calls == [dpi._DEFAULT_LOGPIXELSX]


def test_dpi_scale_degrades_to_one_when_every_api_fails(monkeypatch):
    fake = _install(monkeypatch, FakeWindll(have_get_dpi_for_system=False,
                                            have_gdi=False))
    assert dpi.dpi_scale() == 1.0
    assert fake.gdi_calls == []


def test_dpi_scale_is_cached_until_forced(monkeypatch):
    fake = _install(monkeypatch, FakeWindll(dpi_value=144))
    assert dpi.dpi_scale() == 1.5
    fake.dpi_value = 192          # 缓存生效：仍返回旧值
    assert dpi.dpi_scale() == 1.5
    assert dpi.dpi_scale(force=True) == 2.0


@pytest.mark.parametrize("raw,expected", [
    (1.5, 1.5), ("1.25", 1.25), (1, 1.0), (None, 1.0), ("abc", 1.0),
    (0, 1.0), (-2, 1.0), (float("nan"), 1.0), (float("inf"), 1.0),
    (9.0, 1.0),      # 超上限按脏数据丢弃
])
def test_normalize_scale_rejects_garbage(raw, expected):
    assert dpi.normalize_scale(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (0.8, 0.8), (1 / 1.25, 0.8), (2.5, 2.5), ("0.5", 0.5),
    (0, 1.0), (-1, 1.0), ("x", 1.0), (float("nan"), 1.0), (9.0, 1.0),
])
def test_normalize_factor_allows_downscaling(raw, expected):
    """换算因子必须允许 <1（物理->逻辑要除以缩放比），这是 125%+ 正确性的关键。"""
    assert dpi.normalize_factor(raw) == expected


def test_normalize_scale_rejects_sub_one_but_factor_accepts():
    """两个归一化函数语义不同：屏幕缩放比 >=1，换算因子可 <1。"""
    assert dpi.normalize_scale(0.8) == 1.0        # 缩放比不可能小于 100%
    assert dpi.normalize_factor(0.8) == 0.8       # 但除数是合法的
    assert dpi.scale_rect((100, 200, 400, 80), 0.8) == (80, 160, 320, 64)


# ---------------------------------------------------------------- 感知开关

def test_ensure_dpi_aware_falls_back_and_is_idempotent(monkeypatch):
    fake = _install(monkeypatch, FakeWindll(shcore_ok=False))
    assert dpi.ensure_dpi_aware() is True
    assert ("SetProcessDpiAwareness", 2) in fake.calls
    assert ("SetProcessDPIAware",) in fake.calls
    assert dpi.ensure_dpi_aware() is True         # 幂等
    assert fake.calls.count(("SetProcessDPIAware",)) == 1


def test_ensure_dpi_aware_survives_total_failure(monkeypatch):
    _install(monkeypatch, FakeWindll(shcore_ok=False, user32_aware_ok=False))
    assert dpi.ensure_dpi_aware() is False        # 只告警，不抛
    assert dpi.is_aware() is False


def test_ensure_dpi_aware_readback_overrides_failed_setters(monkeypatch):
    """真实场景：重复调用/清单已声明感知时，设置调用返回 E_ACCESSDENIED（返回值
    而非异常），但进程其实已感知——以读回为准，不能因设置失败就误判未感知。"""
    _install(monkeypatch, FakeWindll(shcore_ok=False, user32_aware_ok=False,
                                     declared_awareness=2))
    assert dpi.ensure_dpi_aware() is True
    assert dpi.is_aware() is True


def test_ensure_dpi_aware_false_when_manifest_declares_unaware(monkeypatch):
    """旧实现的缺陷场景：清单声明非感知 + 两个设置调用都静默失败，
    旧代码只 try/except 会误报已感知；现在以读回的实际级别（0）为准。"""
    _install(monkeypatch, FakeWindll(dpi_value=192, shcore_ok=False,
                                     user32_aware_ok=False, declared_awareness=0))
    assert dpi.ensure_dpi_aware() is False
    assert dpi.is_aware() is False


def test_ensure_dpi_aware_is_false_without_windll(monkeypatch):
    monkeypatch.setattr(dpi, "_windll", lambda: None)
    assert dpi.ensure_dpi_aware() is False
    assert dpi.dpi_scale() == 1.0


# ---------------------------------------------------------------- 坐标空间

def test_capture_space_physical_when_aware(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=144))
    assert dpi.ensure_dpi_aware() is True
    assert dpi.capture_space() == "physical"


def test_capture_space_unaware_cannot_read_real_scale(monkeypatch):
    """非感知进程的 DPI 查询被系统虚拟化成 96：capture_space 只能保守判 physical，
    错位风险由 warnings() 显式告警（真实机器上"logical+缩放"不可测得）。"""
    _install(monkeypatch, FakeWindll(dpi_value=144, shcore_ok=False,
                                     user32_aware_ok=False, declared_awareness=0))
    assert dpi.ensure_dpi_aware() is False
    assert dpi.dpi_scale() == 1.0              # 虚拟化：读不到真实 1.5
    assert dpi.capture_space() == "physical"


def test_capture_space_physical_when_unaware_but_100_percent(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=96, shcore_ok=False,
                                     user32_aware_ok=False))
    dpi.ensure_dpi_aware()
    assert dpi.capture_space() == "physical"      # 1:1，无错位风险


# ---------------------------------------------------------------- 矩形换算

@pytest.mark.parametrize("scale,box,expected", [
    (1.25, (100, 200, 400, 80), (125, 250, 500, 100)),
    (1.5, (100, 200, 401, 81), (150, 300, 602, 122)),      # 四舍五入
    (2.0, (33, 66, 17, 19), (66, 132, 34, 38)),
    (1.0, (10, 20, 30, 40), (10, 20, 30, 40)),             # 原样
])
def test_scale_rect(scale, box, expected):
    assert dpi.scale_rect(box, scale) == expected


def test_scale_rect_clamps_min_size_and_keeps_none():
    assert dpi.scale_rect((0, 0, 0, 0), 2.0) == (0, 0, 1, 1)   # 宽高不归零
    assert dpi.scale_rect(None, 2.0) is None


@pytest.mark.parametrize("scale", [1.25, 1.5, 2.0])
def test_physical_logical_round_trip_is_stable(monkeypatch, scale):
    """物理->逻辑->物理 往返回到原点（取整误差不累积）。"""
    _install(monkeypatch, FakeWindll(dpi_value=int(96 * scale)))
    physical = (640, 480, 1200, 800)
    logical = dpi.to_logical(physical)
    assert dpi.to_physical(logical) == physical
    for got, want in zip(logical, physical):
        assert abs(got - want / scale) <= 0.5       # 每项误差不超过半像素


def test_to_logical_uses_cached_scale_by_default(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=192))
    assert dpi.to_logical((400, 200, 100, 50)) == (200, 100, 50, 25)
    assert dpi.to_physical((200, 100, 50, 25)) == (400, 200, 100, 50)


def test_normalize_rect_passthrough_when_spaces_match(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=144))
    dpi.ensure_dpi_aware()                        # 感知：截图=物理
    assert dpi.normalize_rect((10, 20, 30, 40), "physical") == (10, 20, 30, 40)


def test_normalize_rect_scales_uia_rect_for_unaware_process(monkeypatch):
    """补偿数学（防御分支）：非感知进程只要测得到缩放比，UIA 物理矩形就按比折算。"""
    _install(monkeypatch, FakeWindll(dpi_value=192, shcore_ok=False,
                                     user32_aware_ok=False,
                                     declared_awareness=0, virtualize_dpi=False))
    assert dpi.ensure_dpi_aware() is False
    assert dpi.normalize_rect((400, 200, 100, 50), "physical") == (200, 100, 50, 25)


def test_normalize_rect_upconverts_logical_source(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=192))
    dpi.ensure_dpi_aware()
    assert dpi.normalize_rect((200, 100, 50, 25), "logical") == (400, 200, 100, 50)


def test_normalize_rect_keeps_none():
    assert dpi.normalize_rect(None) is None


def test_normalize_rect_unknown_source_falls_through(monkeypatch):
    """未声明的来源空间不做猜测：原样返回，避免错上加错。"""
    _install(monkeypatch, FakeWindll(dpi_value=144))
    dpi.ensure_dpi_aware()
    assert dpi.normalize_rect((7, 8, 9, 10), "screen") == (7, 8, 9, 10)


# ---------------------------------------------------------------- 屏幕 / 显示器

def test_screen_size_and_virtual_screen(monkeypatch):
    _install(monkeypatch, FakeWindll(screen=(2560, 1440)))
    assert dpi.screen_size() == (2560, 1440)
    assert dpi.virtual_screen() == (-1920, 0, 3840, 1080)


def test_screen_size_none_without_windll(monkeypatch):
    monkeypatch.setattr(dpi, "_windll", lambda: None)
    assert dpi.screen_size() is None
    assert dpi.virtual_screen() is None
    assert dpi.monitors() == []


@pytest.mark.skipif(not _real_monitors_supported(),
                    reason="EnumDisplayMonitors 需要 Windows 的 ctypes.wintypes")
def test_monitors_enumerates_and_marks_first_as_primary(monkeypatch):
    _install(monkeypatch, FakeWindll())
    got = dpi.monitors()
    assert [m["rect"] for m in got] == [[0, 0, 2560, 1440],
                                       [2560, 0, 1920, 1080]]
    assert [m["primary"] for m in got] == [True, False]
    assert [m["index"] for m in got] == [1, 2]


def test_monitors_falls_back_to_virtual_screen_when_enum_fails(monkeypatch):
    fake = FakeWindll()

    def boom(*_a, **_kw):
        raise OSError("enum unsupported")

    fake.user32.EnumDisplayMonitors = boom
    _install(monkeypatch, fake)
    assert dpi.monitors() == [{"index": 1, "rect": [-1920, 0, 3840, 1080],
                               "primary": True}]


# ---------------------------------------------------------------- 快照 / 告警

def test_describe_shape(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=144))
    desc = dpi.describe()
    assert desc["scale"] == 1.5
    assert desc["aware"] is True
    assert desc["space"] == "physical"
    assert desc["resolution"] == "1920x1080"
    assert desc["virtual_screen"] == (-1920, 0, 3840, 1080)
    assert isinstance(desc["monitors"], list)


def test_describe_reports_unknown_resolution_without_windll(monkeypatch):
    monkeypatch.setattr(dpi, "_windll", lambda: None)
    assert dpi.describe()["resolution"] == "unknown"


def test_warnings_are_empty_on_healthy_aware_environment(monkeypatch):
    _install(monkeypatch, FakeWindll(dpi_value=144))     # 150% 且感知成功
    assert dpi.warnings() == []


def test_warnings_flag_unaware_even_when_scale_reads_virtualized(monkeypatch):
    """非感知必须告警：此时系统把缩放比虚拟化成 96，scale==1 不能作为豁免前提
    （旧实现的告警条件在真实缩放屏上永不触发）。"""
    _install(monkeypatch, FakeWindll(dpi_value=192, shcore_ok=False,
                                     user32_aware_ok=False, declared_awareness=0))
    out = dpi.warnings()
    assert len(out) == 1
    assert "未获 DPI 感知" in out[0]


def test_warnings_flag_logical_space_when_scale_measurable(monkeypatch):
    """若测得到缩放比（未虚拟化），未感知 + 缩放屏要给足两条告警。"""
    _install(monkeypatch, FakeWindll(dpi_value=192, shcore_ok=False,
                                     user32_aware_ok=False,
                                     declared_awareness=0, virtualize_dpi=False))
    out = dpi.warnings()
    assert len(out) == 2
    assert "未获 DPI 感知" in out[0]
    assert "逻辑坐标空间" in out[1]


def test_warnings_are_empty_on_non_windows(monkeypatch):
    monkeypatch.setattr(dpi.sys, "platform", "linux")
    monkeypatch.setattr(dpi, "_windll", lambda: None)
    assert dpi.warnings() == []


def test_non_windows_platform_is_inert(monkeypatch):
    """非 Windows：无 DPI 概念，一切按 1:1 处理而不是抛异常。"""
    monkeypatch.setattr(dpi.sys, "platform", "linux")
    assert dpi.ensure_dpi_aware() is False
    assert dpi.dpi_scale() == 1.0
    assert dpi.capture_space() == "physical"
    assert dpi.monitors() == []
    assert dpi.scale_rect((1, 2, 3, 4), 1.0) == (1, 2, 3, 4)

"""DPI 感知与坐标空间归一（设计 8.4 / 第 13 章「高 DPI 坐标错位」风险对策）。

**为什么需要这个模块**

Windows 上坐标有两个空间：

1. **物理像素空间**（physical）：UIA 的 ``BoundingRectangle``、DPI 感知进程的
   ``GetCursorPos`` / ``mss`` 截图都在这套坐标里；
2. **逻辑像素空间**（logical，= 物理 / 缩放比）：**非** DPI 感知进程看到的
   ``GetSystemMetrics``、GDI 桌面截图、``SetCursorPos`` 都在这套坐标里。

只要定位源和操作/截图源落在不同空间，坐标就会按缩放比错位：
100% 缩放看不出来，125% / 150% / 200% 上表现为"模板匹配永远命中不了、
点击点偏掉半个控件"——正是设计 13 章列出的风险。

**本模块的职责**

- ``ensure_dpi_aware()``：进程级 DPI 感知（失败只告警，绝不抛）；
- ``dpi_scale()``：系统缩放比（96 DPI = 1.0，125% -> 1.25……）；
- ``capture_space()``：**当前进程实际落在哪个坐标空间**，供驱动把 UIA 的物理矩形
  换算成"与截图同一套坐标"，这是消除错位的关键一步；
- ``describe()``：环境快照用的 DPI/屏幅描述（设计 M2-5 归因可比性字段）。

工程约定：
- ctypes 在**模块级**导入（stdlib，全平台可用），但 ``windll`` 只在 Windows 存在，
  统一经 ``_windll()`` 取用；非 Windows 一律返回保守值（scale=1.0）而非报错。
- 结果**带缓存**（DPI 在一次进程生命周期内基本不变），``reset_cache()`` 供测试/热切换。
"""
import ctypes
import logging
import sys

log = logging.getLogger("testpilot.dpi")

DEFAULT_DPI = 96.0
_MIN_SCALE = 1.0
_MAX_SCALE = 4.0            # 500% 以上按异常值处理，避免脏数据把坐标算飞

_DEFAULT_LOGPIXELSX = 88    # GetDeviceCaps 的 LOGPIXELSX 常量（避免依赖 win32con）

# 缓存：None 表示未探测
_aware: bool | None = None
_scale_cache: float | None = None


def _build_monitor_types():
    """构造显示器枚举所需的 ctypes 类型（非 Windows 返回 (None, None)）。

    ``EnumDisplayMonitors`` 的回调必须是真正的 C 回调类型（``WINFUNCTYPE``）：
    传 Python 函数会直接失败，而失败又被 ``except`` 吞成"回落虚拟桌面"，
    表现为高分屏多显示器信息永远缺失。这里的类型在模块级构造一次并复用。
    """
    if sys.platform != "win32":
        return None, None
    try:
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE,
                                      wintypes.HDC, ctypes.POINTER(RECT),
                                      wintypes.LPARAM)
        return RECT, proc_type
    except Exception as e:      # pragma: no cover - 极端环境
        log.debug(f"显示器枚举类型构造失败: {e}")
        return None, None


_MONITOR_RECT, _MONITOR_ENUMPROC = _build_monitor_types()


def _windll():
    """取 Windows DLL 集合；非 Windows / 异常时返回 None。"""
    if sys.platform != "win32":
        return None
    return getattr(ctypes, "windll", None)


def reset_cache() -> None:
    """清空探测缓存（单测与运行中切换显示器场景）。"""
    global _aware, _scale_cache
    _aware = None
    _scale_cache = None


# ---------------------------------------------------------------- 感知开关

def _query_awareness(windll) -> bool | None:
    """读回进程**实际**的 DPI 感知级别（shcore.GetProcessDpiAwareness）。

    清单声明与先前调用都会影响实际级别，所以设置完成后必须读回验证，
    不能只信本次设置调用的返回值。API 缺失（Win8.1-）或调用失败返回 None。
    """
    try:
        val = ctypes.c_int(0)
        # pointer()（而非 byref()）：真实 API 语义相同，且测试替身在 Python 层
        # 能直接经 contents 写回值
        hr = windll.shcore.GetProcessDpiAwareness(None, ctypes.pointer(val))
        if hr == 0:
            return int(val.value) >= 1    # 0=UNAWARE 1=SYSTEM_AWARE 2=PER_MONITOR
    except Exception:
        pass
    return None


def ensure_dpi_aware() -> bool:
    """进程级 DPI 感知（幂等）。返回进程**实际**是否处于 DPI 感知模式。

    优先 ``SetProcessDpiAwareness(2)``，失败回落 ``SetProcessDPIAware()``。
    注意 ``ctypes.windll`` **不检查 HRESULT**——失败（含进程已设置时的
    E_ACCESSDENIED）以非零返回值表达而不是抛异常，必须看返回值；
    设置后再经 ``GetProcessDpiAwareness`` 读回，避免"清单声明非感知、
    两个设置调用都静默失败却被当成已感知"的错判。
    """
    global _aware
    if _aware is not None:
        return _aware

    windll = _windll()
    if windll is None:
        _aware = False            # 非 Windows：无 DPI 感知概念，视为已一致
        return _aware

    try:
        set_ok = False
        try:
            hr = windll.shcore.SetProcessDpiAwareness(2)
            set_ok = hr == 0
        except (AttributeError, OSError):
            hr = 1                # 旧系统无 shcore：走 user32 回退
        if hr != 0:
            try:
                set_ok = bool(windll.user32.SetProcessDPIAware())
            except Exception:
                set_ok = False
            if not set_ok:
                log.warning("DPI 感知设置失败：缩放屏下坐标可能错位")
        actual = _query_awareness(windll)
        # 读回优先（清单/先前调用都可能改写过实际级别）；读不到才信本次设置结果
        _aware = actual if actual is not None else set_ok
        log.debug(f"DPI 感知探测完成: shcore_hr={hr} actual_aware={_aware}")
    except Exception as e:        # 探测路径本身异常：按未感知处理并告警
        _aware = False
        log.warning(f"DPI 感知探测异常({e})：缩放屏下坐标可能错位")
    return _aware


def is_aware() -> bool:
    """进程是否已进入 DPI 感知模式（未探测则先探测）。"""
    if _aware is None:
        ensure_dpi_aware()
    return bool(_aware)


# ---------------------------------------------------------------- 缩放比

def normalize_scale(value) -> float:
    """把任意输入规整为合法**屏幕缩放比**；非法/越界回落到 1.0。

    注意：缩放比语义上 >= 1（96 DPI 及以上）；**不要**用它归一化
    ``1/scale`` 这类换算因子，那种场景用 :func:`normalize_factor`。
    """
    scale = normalize_factor(value)
    if scale < _MIN_SCALE:
        return 1.0
    return scale


def normalize_factor(value) -> float:
    """把任意输入规整为合法的**换算因子**（允许小于 1，如 1/1.25=0.8）。"""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return 1.0
    if factor != factor or factor <= 0:        # NaN / <=0
        return 1.0
    if factor > _MAX_SCALE:
        log.warning(f"DPI 换算因子 {factor} 超出合理区间，按 1.0 处理")
        return 1.0
    return round(factor, 6)


def dpi_scale(force: bool = False) -> float:
    """系统 DPI 缩放比（96 DPI = 1.0）。带缓存，探测失败返回 1.0。"""
    global _scale_cache
    if _scale_cache is not None and not force:
        return _scale_cache

    windll = _windll()
    if windll is None:
        _scale_cache = 1.0
        return _scale_cache

    dpi = 0.0
    try:
        dpi = float(windll.user32.GetDpiForSystem())      # Win10 1607+
    except Exception:
        try:
            gdi = windll.gdi32
            dc = windll.user32.GetDC(0)
            dpi = float(gdi.GetDeviceCaps(dc, _DEFAULT_LOGPIXELSX))
            windll.user32.ReleaseDC(0, dc)
        except Exception as e:
            log.debug(f"DPI 探测失败，按 100% 处理: {e}")
            dpi = DEFAULT_DPI

    if dpi <= 0:
        dpi = DEFAULT_DPI
    _scale_cache = normalize_scale(dpi / DEFAULT_DPI)
    return _scale_cache


# ---------------------------------------------------------------- 屏幕 / 显示器

def screen_size() -> tuple[int, int] | None:
    """主屏逻辑尺寸（DPI 非感知进程拿到的是逻辑值，这正是我们要的）。"""
    windll = _windll()
    if windll is None:
        return None
    try:
        return (int(windll.user32.GetSystemMetrics(0)),
                int(windll.user32.GetSystemMetrics(1)))
    except Exception:
        return None


def virtual_screen() -> tuple[int, int, int, int] | None:
    """虚拟桌面（多屏合并）矩形 (x, y, w, h)。"""
    windll = _windll()
    if windll is None:
        return None
    try:
        u = windll.user32
        x = int(u.GetSystemMetrics(76))     # SM_XVIRTUALSCREEN
        y = int(u.GetSystemMetrics(77))     # SM_YVIRTUALSCREEN
        w = int(u.GetSystemMetrics(78))     # SM_CXVIRTUALSCREEN
        h = int(u.GetSystemMetrics(79))     # SM_CYVIRTUALSCREEN
        return (x, y, w, h)
    except Exception:
        return None


def monitors() -> list[dict]:
    """枚举显示器：``[{index, rect, primary}]``；枚举不可用时回落到虚拟桌面一条。

    说明：逐屏 DPI 需要 ``GetDpiForMonitor``（shcore，需 HMONITOR 句柄），
    这里只做只读枚举，不猜每屏缩放比——避免给出错误值。
    """
    windll = _windll()
    if windll is None or _MONITOR_RECT is None:
        return _virtual_fallback()

    try:
        found: list[dict] = []

        def _cb(_hmon, _hdc, lprc, _data):
            r = ctypes.cast(lprc, ctypes.POINTER(_MONITOR_RECT)).contents
            found.append({"index": len(found) + 1,
                          "rect": [int(r.left), int(r.top),
                                   int(r.right - r.left), int(r.bottom - r.top)],
                          "primary": False})
            return True

        # 必须包成真正的 WINFUNCTYPE，否则 user32 收到的是 Python 对象 -> 调用直接失败
        # （表现形式是"枚举永远悄悄回落到虚拟桌面"，单屏机器上看不出来）
        proc = _MONITOR_ENUMPROC(_cb)
        windll.user32.EnumDisplayMonitors(None, None, proc, 0)
        if found:
            found[0]["primary"] = True          # 枚举首个为主屏（Windows 约定）
            return found
    except Exception as e:
        log.debug(f"显示器枚举失败，回落虚拟桌面: {e}")

    return _virtual_fallback()


def _virtual_fallback() -> list[dict]:
    vs = virtual_screen()
    if vs:
        return [{"index": 1, "rect": list(vs), "primary": True}]
    return []


# ---------------------------------------------------------------- 坐标空间归一

def capture_space() -> str:
    """当前进程的截图坐标空间：``"physical"`` 或 ``"logical"``。

    DPI 感知进程的 mss/GDI 截图是物理像素；非感知进程被 DWM 缩放，拿到逻辑像素。
    驱动据此把 UIA 的物理矩形换算到"与截图同一套坐标"。
    """
    if is_aware():
        return "physical"
    return "logical" if dpi_scale() != 1.0 else "physical"


def scale_rect(rect, factor: float):
    """按比例缩放矩形 ``(x, y, w, h)``；factor 非法时原样返回。"""
    if not rect:
        return rect
    f = normalize_factor(factor)
    if f == 1.0:
        return tuple(int(v) for v in rect)
    x, y, w, h = (float(v) for v in rect)
    return (int(round(x * f)), int(round(y * f)),
            max(1, int(round(w * f))), max(1, int(round(h * f))))


def to_logical(rect, scale: float | None = None):
    """物理像素 -> 逻辑像素（除以缩放比）。"""
    s = normalize_scale(dpi_scale() if scale is None else scale)
    return scale_rect(rect, 1.0 / s) if s != 1.0 else scale_rect(rect, 1.0)


def to_physical(rect, scale: float | None = None):
    """逻辑像素 -> 物理像素（乘以缩放比）。"""
    s = normalize_scale(dpi_scale() if scale is None else scale)
    return scale_rect(rect, s)


def normalize_rect(rect, source: str | None = None):
    """把 ``source`` 空间的矩形换算到当前**截图空间**。

    - 源空间与截图空间一致 -> 原样返回（零成本，也避免无谓取整误差）；
    - UIA 物理矩形 + 非感知进程（截图是逻辑空间）-> 除以缩放比。
    """
    if not rect:
        return rect
    source = source or "physical"
    target = capture_space()
    if source == target:
        return tuple(int(v) for v in rect)
    if source == "physical" and target == "logical":
        return to_logical(rect)
    if source == "logical" and target == "physical":
        return to_physical(rect)
    return tuple(int(v) for v in rect)


# ---------------------------------------------------------------- 快照描述

def describe() -> dict:
    """环境快照用描述（设计 M2-5 归因可比性）。"""
    size = screen_size()
    return {
        "aware": is_aware(),
        "scale": dpi_scale(),
        "space": capture_space(),
        "resolution": f"{size[0]}x{size[1]}" if size else "unknown",
        "monitors": monitors(),
        "virtual_screen": virtual_screen(),
    }


def warnings() -> list[str]:
    """给 doctor / 壳状态徽章用的告警文案（无告警返回空列表）。"""
    out: list[str] = []
    scale = dpi_scale()
    if sys.platform != "win32":
        return out
    if not is_aware():
        # 未感知进程的系统 DPI 查询会被 Windows 虚拟化成 96（读不到真实缩放比），
        # 所以这里**不能**以 scale != 1 为前提——只要未感知就要提示错位风险
        out.append(f"进程未获 DPI 感知（系统报告缩放 {scale:.2f}x，"
                   f"真实缩放可能更高且无法测得）：UIA 物理坐标与截图可能按缩放比"
                   f"错位，建议检查进程清单/启动方式")
    if scale != 1.0 and capture_space() == "logical":
        out.append(f"截图落在逻辑坐标空间（{scale:.2f}x）：模板素材需按同一缩放采集")
    return out

"""视觉驱动 — L1 模板匹配(零成本) + L2 VLM 定位(兜底)（设计 8.4）。

分层与学习闭环（设计 5.2 / M3-3）：
1. ``template`` 定位 —— 命中模板库，毫秒级、零 token；
2. ``visual`` 定位 —— **先查已学模板**（desc -> 模板文件），未命中才送 VLM；
   VLM 命中后由引擎回调 ``learn_visual_template`` 把该区域裁图存库，
   下次同一语义元素直接走 L1（成本趋零）；
3. VLM 置信度低于阈值不硬点，抛 ``LocateError`` 交引擎按策略处理。

关键工程点：
- **DPI 感知**：不做会导致截图坐标与物理像素错位（高分屏必踩，见设计 13 章）；
  感知开关、缩放比、坐标空间归一统一在 ``infra/dpi.py``（M3-4）；
- **窗口坐标换算**：``ctx.window_rect``（UIA 物理像素）先归一到截图空间，
  再以它为基准裁剪/定位，结果同样落在截图空间；
- 模板匹配便宜 → 在超时预算内轮询；VLM 有成本 → 单次尝试。

工程约定：cv2 / numpy / mss / PIL 全部**方法内惰性导入**，``MANIFEST`` 保持模块级，
未装视觉依赖的环境仍可 import 本模块（注册表与一致性门禁不依赖运行时）。

cv2 统一经 ``infra.opencv.load_cv2()`` 取得：引导文件（``cv2/__init__.py``）不可读时
会自动降级为直接装载原生扩展，不因一个文本文件坏了就丢掉整条视觉能力线。
"""
import base64
import logging
import re
import time
import math
from contextlib import nullcontext

from testpilot.core.contracts import (DriverKind, DriverManifest, ExecContext,
                                      HealthReport, LocatedElement, Rect,
                                      Screenshot)
from testpilot.core.dsl import Locator, LocatorType
from testpilot.core.errors import ActionError, LocateError
from testpilot.infra import dpi
from testpilot.infra.opencv import load_cv2

log = logging.getLogger("testpilot.vision")

MANIFEST = DriverManifest(
    kind="interaction",
    name="vision",
    version="1.2.0",
    capabilities=frozenset({"click", "right_click", "input", "scroll", "drag", "grab",
                            "locate_template", "locate_visual"}),
    config_schema={"type": "object", "properties": {
        "grab_backend": {"type": "string", "enum": ["mss", "pil"],
                         "default": "mss"},
        "template_dir": {"type": "string", "default": "./data/templates"},
        "template_threshold": {"type": "number", "default": 0.86},
        "vision_min_confidence": {"type": "number", "default": 0.70},
        "locate_poll_ms": {"type": "integer", "default": 300},
        "learn_from_vlm": {"type": "boolean", "default": True,
                           "description": "VLM 命中后是否自动沉淀模板（L2->L1）"},
    }},
    entry="testpilot.drivers.vision_driver:create",
    priority=20,
)

# 语义描述 -> 模板文件名时需要的字符清洗（跨平台安全）
_UNSAFE_RE = re.compile(r"[^\w\u4e00-\u9fff.-]+")

# DPI 感知与坐标空间归一统一收敛在 infra/dpi.py（M3-4）；
# 这里保留同名入口，模块内调用点不必关心实现位置。
_ensure_dpi_aware = dpi.ensure_dpi_aware


def sanitize_key(key: str) -> str:
    """把元素语义键规整为安全文件名（保留中文/字母数字/点/横线）。"""
    cleaned = _UNSAFE_RE.sub("_", str(key)).strip("._")
    return cleaned[:80] or "unnamed"


def _imread_unicode(cv2, np, path):
    """读图（**支持中文等非 ASCII 路径**）。

    保留本函数签名是为了不破坏既有调用/测试；实现已上提到 ``infra/imaging``
    （报告侧算截图差异也要用同一套语义，不能各写一份）。
    """
    from testpilot.infra.imaging import imread
    return imread(path)


def _imwrite_unicode(cv2, path, img) -> bool:
    """写图（**支持中文等非 ASCII 路径**）。成功返回 True。"""
    from testpilot.infra.imaging import imwrite
    return imwrite(path, img)


def grab_screen(rect: Rect | None = None, backend: str = "mss") -> Screenshot:
    """全屏或区域截图 -> PNG bytes。mss 为主，PIL 为备。"""
    _ensure_dpi_aware()
    if backend == "pil":
        from io import BytesIO

        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(rect[0], rect[1], rect[0] + rect[2],
                                   rect[1] + rect[3]) if rect else None)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return Screenshot(png=buf.getvalue(), rect=rect)

    cv2 = load_cv2()
    import mss
    import numpy as np
    with mss.mss() as sct:
        region = None
        if rect is not None:
            x, y, w, h = rect
            region = {"left": x, "top": y, "width": w, "height": h}
        # 无矩形（app.launch 之前/关闭后的 assert）取 monitors[0] = 整个虚拟桌面：
        # monitors[1] 只有主屏，被测程序在副屏时会静默截到错误屏幕
        shot = sct.grab(region if region else sct.monitors[0])
        # mss 返回 BGRA；cv2 期望 BGR -> 丢掉 alpha 通道（不能反转通道序）
        bgr = np.asarray(shot, dtype=np.uint8)[:, :, :3]
        ok, buf = cv2.imencode(".png", bgr)
        if not ok:
            raise ActionError("截图编码失败")
        return Screenshot(png=buf.tobytes(), rect=rect)


class VisionDriver:
    def __init__(self, cfg: dict, llm):
        from pathlib import Path

        self.cfg = cfg or {}
        self.llm = llm
        self.template_dir = Path(self.cfg.get("template_dir", "./data/templates"))
        self.template_dir.mkdir(parents=True, exist_ok=True)

    manifest = MANIFEST

    def health(self) -> HealthReport:
        try:
            cv2 = load_cv2()
        except Exception as e:
            return HealthReport(ok=False, detail=f"opencv 不可用: {e}")
        detail = f"opencv {cv2.__version__}"
        if self.llm is None:
            detail += "；VLM 未装配（仅 L1 模板可用）"
        return HealthReport(ok=True, detail=detail)

    # ---------- 定位 ----------

    def locate(self, loc: Locator, ctx: ExecContext,
               timeout_ms: int | None = None) -> LocatedElement:
        """模板匹配在超时预算内轮询；VLM 语义定位单次尝试（成本约束）。"""
        _ensure_dpi_aware()
        win = self.window_rect(ctx)          # 已归一化到截图坐标空间（M3-4）
        origin = (win[0], win[1]) if win else (0, 0)

        if loc.type == LocatorType.TEMPLATE:
            if not loc.template:
                raise LocateError("template 定位缺少 template 路径")
            # 模板缺失属配置错误：立刻失败，不浪费整个超时预算去轮询
            self._template_path(loc.template)
            return self._poll_template(loc.template, loc.region, ctx, win, origin,
                                       timeout_ms)

        if loc.type == LocatorType.VISUAL:
            learned = self.learned_template(loc.desc)
            if learned:
                # L2->L1 毕业：同一语义元素已沉淀模板，先用零成本 L1 试
                try:
                    return self._poll_template(learned, loc.region, ctx, win,
                                               origin, timeout_ms)
                except LocateError as e:
                    log.info(f"已学模板未命中，回落 VLM: desc={loc.desc!r} {e}")
            img = self._grab_bgr(win)
            return self._locate_vlm(loc, img, origin, ctx, timeout_ms)

        raise LocateError(f"vision 驱动不接受定位类型: {loc.type}")

    @staticmethod
    def window_rect(ctx: ExecContext) -> Rect | None:
        """窗口基准统一换算到**截图坐标空间**。

        ``ctx.window_rect`` 来自 UIA（物理像素）；若进程未获 DPI 感知，mss 截图
        落在逻辑像素空间，两套坐标不归一就会按缩放比错位（设计 13 章风险项）。
        """
        return dpi.normalize_rect(ctx.window_rect, "physical")

    def _poll_template(self, template: str, region, ctx: ExecContext,
                       win: Rect | None, origin,
                       timeout_ms: int | None) -> LocatedElement:
        deadline = time.monotonic() + (timeout_ms if timeout_ms is not None
                                       else 3000) / 1000
        poll = float(self.cfg.get("locate_poll_ms", 300)) / 1000
        # 模板整轮只读一次盘：长超时预算下每圈重读是纯浪费
        tpl = self._load_template(template)
        last: LocateError | None = None
        while True:
            ctx.abort.check()
            img = None
            try:
                img = self._grab_bgr(win)
            except Exception as e:
                # 瞬时截图失败（编码/屏幕暂不可用）不该击穿驱动降级链：
                # 按 LocateError 处理，轮询到超时后由引擎换下一驱动
                last = LocateError(f"轮询截图失败: {e}")
            if img is not None:
                try:
                    return self._match_template(tpl, region, img, origin)
                except LocateError as e:
                    last = e
            if time.monotonic() >= deadline:
                raise last or LocateError("模板匹配超时")
            time.sleep(poll)

    def _load_template(self, template: str):
        """读模板图；缺失/不可读立刻失败（配置错误不浪费超时预算轮询）。"""
        import numpy as np

        cv2 = load_cv2()
        tpl_path = self._template_path(template)
        tpl = _imread_unicode(cv2, np, tpl_path)
        if tpl is None:
            raise LocateError(f"模板无法读取: {tpl_path}")
        return tpl

    def _template_path(self, template: str):
        """模板文件路径校验（存在性 + 可读性前置检查）。"""
        path = self.template_dir / template
        if not path.exists():
            raise LocateError(f"模板不存在: {path}")
        return path

    def _locate_template(self, template: str, region, img_bgr,
                         origin) -> LocatedElement:
        """L1: OpenCV 模板匹配 — 毫秒级、零 token。"""
        return self._match_template(self._load_template(template), region,
                                    img_bgr, origin, target_key=template)

    def _match_template(self, tpl, region, img_bgr, origin,
                        target_key: str = "") -> LocatedElement:
        cv2 = load_cv2()

        search, off_x, off_y = img_bgr, 0, 0
        if region:
            rx, ry, rw, rh = region
            search = img_bgr[ry:ry + rh, rx:rx + rw]
            off_x, off_y = rx, ry
        if search.size == 0 or search.shape[0] < tpl.shape[0] \
                or search.shape[1] < tpl.shape[1]:
            raise LocateError("搜索区域小于模板")

        th = float(self.cfg.get("template_threshold", 0.86))
        res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        # `not (max_val >= th)` 同时拦住低分与 NaN：纯色/零方差模板会让
        # TM_CCOEFF_NORMED 产出 NaN，`NaN < th` 恒 False 会绕过阈值直接"命中"
        if not (max_val >= th):
            raise LocateError(f"模板匹配分不足: {max_val:.3f} < {th}",
                              confidence=float(max_val)
                              if max_val == max_val else 0.0)
        h, w = tpl.shape[:2]
        bbox = (origin[0] + off_x + max_loc[0], origin[1] + off_y + max_loc[1], w, h)
        log.info("模板匹配命中", extra={"confidence": round(float(max_val), 3),
                                        "target_key": target_key})
        return LocatedElement(driver=DriverKind.TEMPLATE, handle=bbox,
                              rect=bbox, confidence=float(max_val))

    def _locate_vlm(self, loc, img_bgr, origin, ctx=None, timeout_ms=None) -> LocatedElement:
        """L2: VLM 语义定位 — 截图（可裁剪 region）送 grounding 模型取 bbox。"""
        cv2 = load_cv2()
        import numpy as np

        if self.llm is None:
            raise LocateError("VLM 未装配（llm 网关缺失），无法做语义定位")

        crop, off_x, off_y = img_bgr, 0, 0
        if loc.region:
            rx, ry, rw, rh = loc.region
            crop = img_bgr[ry:ry + rh, rx:rx + rw]
            off_x, off_y = rx, ry
        if crop.size == 0:
            raise LocateError("VLM 定位区域为空")
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            raise LocateError("VLM 输入截图编码失败")
        b64 = base64.b64encode(np.asarray(buf).tobytes()).decode()
        t0 = time.monotonic()
        scope = getattr(self.llm, "request_context", None)
        with scope(abort=ctx.abort if ctx else None,
                   timeout_s=timeout_ms / 1000 if timeout_ms is not None else None) if scope else nullcontext():
            out = self.llm.ground(image_b64=b64, desc=loc.desc or "", region=loc.region)
        ms = int((time.monotonic() - t0) * 1000)
        conf = float(out.get("confidence", 0) or 0)
        min_conf = float(self.cfg.get("vision_min_confidence", 0.70))
        bbox = out.get("bbox")
        if not bbox or len(bbox) != 4:
            # bbox 缺失与置信度低是两种故障：混成一条"置信度不足"会误导归因与人工复核
            raise LocateError(f"VLM 未返回有效 bbox: {bbox!r}", confidence=conf)
        if not math.isfinite(conf) or not 0 <= conf <= 1 or conf < min_conf:
            # 低置信不硬点 -> 抛 LocateError，引擎按策略转人工复核
            raise LocateError(f"VLM 置信度不足: {conf:.2f} < {min_conf}",
                              confidence=conf)
        bx, by, bw, bh = (int(v) for v in bbox)
        ch, cw = crop.shape[:2]
        if bw <= 0 or bh <= 0 or bx < 0 or by < 0 \
                or bx + bw > cw or by + bh > ch:
            # 越界/零尺寸 bbox 直接拒绝：负坐标或 w=0 的框点击会落空甚至点到左上角
            raise LocateError(
                f"VLM 返回 bbox 越界: {bbox!r}（裁剪图 {cw}x{ch}）",
                confidence=conf)
        screen_bbox = (origin[0] + off_x + bx, origin[1] + off_y + by, bw, bh)
        log.info("VLM 定位命中", extra={"confidence": conf,
                                        "target_key": loc.desc or "",
                                        "locate_ms": ms})
        return LocatedElement(driver=DriverKind.VISION, handle=screen_bbox,
                              rect=screen_bbox, confidence=conf)

    # ---------- 操作 ----------

    def click(self, el: LocatedElement, ctx: ExecContext) -> None:
        """bbox -> 屏幕中心点击（真实鼠标事件，保证被测软件收到完整消息）。"""
        ctx.abort.check()
        x, y, w, h = el.rect
        cx, cy = x + w // 2, y + h // 2
        _ensure_dpi_aware()
        try:
            import win32api
            import win32con
            win32api.SetCursorPos((cx, cy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, cx, cy, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, cx, cy, 0, 0)
        except Exception as e:
            raise ActionError(f"视觉点击失败 @({cx},{cy}): {e}") from e

    def right_click(self, el: LocatedElement, ctx: ExecContext) -> None:
        """在视觉目标中心发送右键。"""
        ctx.abort.check()
        x, y, w, h = el.rect
        if w <= 0 or h <= 0:
            raise ActionError("视觉目标没有可操作几何区域")
        try:
            from testpilot.infra.wininput import right_click_at
            right_click_at(x + w // 2, y + h // 2, ctx.abort)
        except Exception as e:
            from testpilot.core.errors import AbortRequested
            if isinstance(e, AbortRequested):
                raise
            raise ActionError(f"视觉右键失败: {e}") from e

    def scroll(self, el: LocatedElement, direction: str, amount: int,
               ctx: ExecContext) -> None:
        """在视觉目标中心发送物理滚轮。"""
        ctx.abort.check()
        x, y, w, h = el.rect
        if w <= 0 or h <= 0:
            raise ActionError("视觉目标没有可操作几何区域")
        cx, cy = x + w // 2, y + h // 2
        try:
            from testpilot.infra.wininput import scroll_at
            scroll_at(cx, cy, direction, amount, ctx.abort)
        except Exception as e:
            from testpilot.core.errors import AbortRequested
            if isinstance(e, AbortRequested):
                raise
            raise ActionError(
                f"视觉滚动失败: direction={direction!r} amount={amount!r} ({e})") from e

    def drag(self, el: LocatedElement, dx: int, dy: int, duration_ms: int,
             ctx: ExecContext) -> None:
        """从视觉目标中心按相对位移拖拽，默认限制在当前被测窗口。"""
        ctx.abort.check()
        x, y, w, h = el.rect
        if w <= 0 or h <= 0:
            raise ActionError("视觉目标没有可操作几何区域")
        cx, cy = x + w // 2, y + h // 2
        end_x, end_y = cx + int(dx), cy + int(dy)
        if ctx.window_rect is not None:
            wx, wy, ww, wh = ctx.window_rect
            if not (wx <= end_x < wx + ww and wy <= end_y < wy + wh):
                raise ActionError(
                    f"视觉拖拽终点 ({end_x},{end_y}) 超出当前被测窗口")
        try:
            from testpilot.infra.wininput import drag_from
            drag_from(cx, cy, int(dx), int(dy), int(duration_ms), ctx.abort)
        except Exception as e:
            from testpilot.core.errors import AbortRequested
            if isinstance(e, AbortRequested):
                raise
            raise ActionError(
                f"视觉拖拽失败: dx={dx!r} dy={dy!r} duration_ms={duration_ms!r} ({e})") from e

    def input_text(self, el: LocatedElement, text: str, ctx: ExecContext) -> None:
        """视觉定位的输入：点击聚焦后剪贴板粘贴（支持中文/特殊字符）。"""
        self.click(el, ctx)
        try:
            import pyperclip
            import win32api
            import win32con
        except Exception as e:      # pragma: no cover - 依赖缺失
            raise ActionError(f"视觉输入需要 pyperclip/pywin32: {e}") from e
        pyperclip.copy(text)
        time.sleep(0.05)            # 等剪贴板落定
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(ord("V"), 0, 0, 0)
        win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    def select(self, el: LocatedElement, value: str, ctx: ExecContext) -> None:
        raise ActionError("视觉驱动不支持 select，请在 DSL 中指定 uia 驱动")

    def wait_ready(self, ctx: ExecContext, timeout_ms: int) -> bool:
        """视觉驱动无就绪信号概念：视为立即就绪（真就绪判据应由 UIA/测试端口给）。"""
        return True

    def wait_gone(self, el: LocatedElement, ctx: ExecContext,
                  timeout_ms: int) -> bool:
        raise ActionError("视觉驱动不支持 wait_gone，请在 DSL 中指定 uia 驱动")

    def read_text(self, el: LocatedElement) -> str:
        raise ActionError("视觉驱动不支持 read_text，请在 DSL 中指定 uia 驱动")

    def grab(self, rect: Rect | None, ctx: ExecContext) -> Screenshot:
        """截图；``rect`` 约定为**物理像素**（与 UIA/``ctx.window_rect`` 一致），
        内部归一化到截图坐标空间后再抓取。"""
        return grab_screen(dpi.normalize_rect(rect, "physical"),
                           self.cfg.get("grab_backend", "mss"))

    # ---------- 学习接口（L2 -> L1） ----------

    def learned_template(self, desc: str | None) -> str | None:
        """语义描述对应的已学模板文件名；没有则 None。"""
        if not desc:
            return None
        name = f"{sanitize_key(desc)}.png"
        return name if (self.template_dir / name).exists() else None

    def learn_template(self, key: str, img_bgr) -> str:
        """把裁剪图写入模板库，返回相对模板路径（可直接写进 DSL ``Locator.template``）。"""
        cv2 = load_cv2()

        safe = sanitize_key(key)
        path = self.template_dir / f"{safe}.png"
        if not _imwrite_unicode(cv2, path, img_bgr):
            raise ActionError(f"模板写入失败: {path}")
        log.info(f"模板已学习: {path}")
        return f"{safe}.png"

    def learn_visual_template(self, desc: str | None, screen_rect: Rect | None,
                             ctx: ExecContext) -> str | None:
        """VLM 命中后沉淀模板：按窗口基准裁剪该区域存库。

        ``screen_rect`` 来自本驱动刚算出的定位结果，**已在截图坐标空间**；
        ``ctx.window_rect`` 是 UIA 物理矩形，需先归一化到同一空间再相减，
        否则高分屏下裁剪窗口会整体偏移（M3-4）。

        下次同一语义元素（同 ``desc``）定位时先走 L1 模板匹配，命中即不再花 token。
        """
        if not desc or not screen_rect or not self.cfg.get("learn_from_vlm", True):
            return None
        import numpy as np

        x, y, w, h = screen_rect
        base = self.window_rect(ctx)
        try:
            if base:
                bx, by, bw, bh = base
                img = self._grab_bgr(base)
                rx, ry = x - bx, y - by
                if rx < 0 or ry < 0 or rx + w > bw or ry + h > bh:
                    log.info("定位结果不在窗口基准内，退化为直接区域截图")
                    img = self._grab_bgr(screen_rect)
                    rx = ry = 0
            else:
                img = self._grab_bgr(screen_rect)
                rx = ry = 0
            crop = img[ry:ry + h, rx:rx + w]
            if crop is None or crop.size == 0:
                log.warning("学习模板时裁剪为空，跳过")
                return None
            return self.learn_template(desc, np.ascontiguousarray(crop))
        except Exception as e:      # 学习失败绝不影响执行主干
            log.warning(f"学习视觉模板失败(不阻断): {e}")
            return None

    # ---------- 内部 ----------

    def _grab_bgr(self, rect: Rect | None):
        from testpilot.infra.imaging import decode_png

        shot = grab_screen(rect, self.cfg.get("grab_backend", "mss"))
        return decode_png(shot.png)


def create(cfg: dict) -> VisionDriver:
    """注册表工厂。llm 网关缺省取全局配置实例（装配层可显式覆盖）。"""
    from testpilot.adapters.llm_gateway import get_default_llm
    return VisionDriver(cfg, get_default_llm())

"""图像编解码工具 —— 非 ASCII 路径安全的读写（**单一来源**）。

**为什么单独成模块**：``cv2.imread`` / ``cv2.imwrite`` 走 ANSI 文件 API，在中文路径下
（本项目的元素语义键、用例标题大量是中文）会**静默失败**——写不进、读不回，
视觉学习闭环因此失效（历史缺陷 F3）。正确做法是 ``np.fromfile + cv2.imdecode`` /
``cv2.imencode + write_bytes``。

这段逻辑原先只在 ``drivers/vision_driver.py`` 里，报告侧需要算截图差异时又要用一份，
所以上提为共用模块（避免"同类根因只修一半"）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from testpilot.infra.opencv import load_cv2

log = logging.getLogger("testpilot.imaging")


def imread(path: str | Path, flags: int | None = None):
    """读图；失败返回 ``None``（不抛）。

    默认按彩色读（``IMREAD_COLOR``）。中文路径安全。
    """
    import numpy as np

    cv2 = load_cv2()
    flags = cv2.IMREAD_COLOR if flags is None else flags
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError as e:
        log.warning(f"图片读取失败: {path} {e}")
        return None
    if data.size == 0:
        return None
    img = cv2.imdecode(data, flags)
    if img is None:
        log.warning(f"图片解码失败（格式不支持或文件损坏）: {path}")
    return img


def imwrite(path: str | Path, img) -> bool:
    """写图；成功返回 True。中文路径安全。"""
    cv2 = load_cv2()
    path = Path(path)
    try:
        ok, buf = cv2.imencode(path.suffix or ".png", img)
    except Exception as e:          # 图像非法（dtype/shape 不对）不该抛给调用方
        log.warning(f"图片编码失败: {path} {e}")
        return False
    if not ok:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf.tobytes())
    except OSError as e:
        log.warning(f"图片写盘失败: {path} {e}")
        return False
    return True


def decode_png(png: bytes, flags: int | None = None):
    """从 PNG 字节解出图像（截图存的是 bytes，不落盘也能算差异）。"""
    import numpy as np

    cv2 = load_cv2()
    flags = cv2.IMREAD_COLOR if flags is None else flags
    if not png:
        return None
    return cv2.imdecode(np.frombuffer(png, np.uint8), flags)


def diff_overlay(before_png: bytes, after_png: bytes, *, pixel_delta: int = 12,
                 min_area: int = 12, max_boxes: int = 60):
    """算 before/after 差异并返回 ``(overlay_png, stats)``。

    ``overlay_png``：以 after 为底图、差异区描红框的 PNG 字节（算不出来返回 None）；
    ``stats``：``{ratio, boxes, changed, same_size, size_before, size_after}``。

    为什么要**连通域描框**而不是整图叠加：整图 50% 透明的红蓝叠加在高分屏上几乎看不清，
    而"哪里变了"才是排查线索。连通域用形态学膨胀近似（不引入 scipy 依赖），
    对小面积噪点做面积过滤，避免把 JPEG 噪声全框出来。
    """
    import numpy as np

    stats = {"ratio": 0.0, "boxes": 0, "changed": 0, "same_size": False,
             "size_before": None, "size_after": None}
    before = decode_png(before_png)
    after = decode_png(after_png)
    if before is None or after is None:
        return None, stats

    cv2 = load_cv2()
    stats["size_before"] = f"{before.shape[1]}x{before.shape[0]}"
    stats["size_after"] = f"{after.shape[1]}x{after.shape[0]}"

    if before.shape != after.shape:
        # 尺寸变了就没法逐像素对齐：以 after 为底图，整体提示"尺寸变化"
        return _annotate_size_change(cv2, np, after, stats), stats

    stats["same_size"] = True
    ga = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(ga, gb)
    mask = (diff > pixel_delta).astype(np.uint8) * 255
    changed = int(np.count_nonzero(mask))
    stats["changed"] = changed
    stats["ratio"] = round(changed / diff.size, 4) if diff.size else 0.0
    if changed == 0:
        # 契约是"返回 PNG 字节"：直接回 ndarray 会让消费方 write_bytes 抛 TypeError
        ok, buf = cv2.imencode(".png", after)
        return (buf.tobytes() if ok else None), stats

    # 形态学闭运算把邻近像素连成块，减少碎框数量
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    merged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in contours]
    boxes = [b for b in boxes if b[2] * b[3] >= min_area]
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    boxes = boxes[:max_boxes]
    stats["boxes"] = len(boxes)

    overlay = after.copy()
    for (x, y, w, h) in boxes:
        cv2.rectangle(overlay, (x - 2, y - 2), (x + w + 2, y + h + 2),
                      (0, 0, 255), 2)
    ok, buf = cv2.imencode(".png", overlay)
    return (buf.tobytes() if ok else None), stats


def _annotate_size_change(cv2, np, after, stats: dict):
    """尺寸变化时给底图加一条提示条，避免用户以为是"没变化"。"""
    overlay = after.copy()
    h, w = overlay.shape[:2]
    band = min(28, max(16, h // 20))
    cv2.rectangle(overlay, (0, 0), (w, band), (0, 140, 255), -1)
    cv2.putText(overlay, "size changed", (6, int(band * 0.72)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    stats["changed"] = -1                     # 约定：-1 表示尺寸不一致
    stats["ratio"] = 1.0
    ok, buf = cv2.imencode(".png", overlay)
    return buf.tobytes() if ok else None

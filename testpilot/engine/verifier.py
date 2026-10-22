"""动作验证 — 规则断言优先，VLM 断言带置信度门禁（设计 8.6 / 6.3）。

判定矩阵：
- ``text``            控件文本 contains/equals/regex（UIA 读取，规则）
- ``state``           内部状态路径值（模拟器/测试端口，规则）
- ``vision``          截图语义（VLM + 置信度；低于阈值返回 unknown，不硬判）
- ``screenshot_diff`` 区域前后变化 >= 阈值（OpenCV，只判"有没有变"）
- ``none``            不验证

规则断言优先；``unknown`` 一律不判 fail（防幻觉漏判/误判，交人工复核）。
"""
import base64
import logging
import re
import time
import math
from contextlib import nullcontext

from testpilot.core.errors import AbortRequested

from testpilot.core.contracts import VerifyResult

log = logging.getLogger("testpilot.verifier")

_DEFAULT_DIFF_RATIO = 0.005   # 变化像素占比阈值
_DIFF_PIXEL_DELTA = 25        # 单像素灰度差阈值（抑制压缩噪声）


class Verifier:
    def __init__(self, drivers: dict, emulator, llm, cfg: dict):
        self.drivers = drivers
        self.emulator = emulator
        self.llm = llm
        self.cfg = cfg or {}

    def verify(self, step, ctx, before, after) -> VerifyResult:
        v = step.verify
        if v is None or v.type == "none":
            return VerifyResult(status="pass", detail="无需验证")

        if v.type == "text":
            return self._verify_text(v, step, ctx)
        if v.type == "state":
            return self._verify_state(v, step, ctx)
        if v.type == "vision":
            return self._verify_vision(v, after, ctx)
        if v.type == "screenshot_diff":
            return self._verify_diff(before, after, v)
        return VerifyResult(status="fail", detail=f"未知验证类型: {v.type}")

    # ---------- 规则断言 ----------

    def _poll(self, check, timeout_ms, ctx=None) -> VerifyResult:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            if ctx is not None:
                ctx.abort.check()
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            result = check(remaining_ms)
            if ctx is not None:
                ctx.abort.check()
            if time.monotonic() > deadline and result.status == "pass":
                return VerifyResult(status="fail", detail="断言超时: " + result.detail)
            if result.status != "fail" or time.monotonic() >= deadline:
                return result
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return result

    def _verify_text(self, v, step, ctx) -> VerifyResult:
        return self._poll(lambda remaining: self._verify_text_once(v, step, ctx, remaining),
                          v.timeout_ms, ctx)

    def _verify_text_once(self, v, step, ctx, remaining_ms) -> VerifyResult:
        """UIA 读取控件文本 -> contains/equals/regex。"""
        uia = self.drivers.get("uia")
        if uia is None:
            return VerifyResult(status="fail", detail="text 断言需要 UIA 驱动")
        loc = v.target or (step.target.primary if step.target else None)
        if loc is None:
            return VerifyResult(status="fail",
                                detail="text 断言缺少 target（verify.target 或 step.target）")
        try:
            el = uia.locate(loc, ctx, timeout_ms=remaining_ms)
            text = uia.read_text(el)
        except AbortRequested:
            raise
        except Exception as e:
            return VerifyResult(status="fail", detail=f"断言定位/读取失败: {e}")

        expect = v.expect or ""
        if v.op == "equals":
            ok = text == expect
        elif v.op == "regex":
            try:
                ok = bool(re.search(expect, text))
            except re.error as e:
                return VerifyResult(status="fail", detail=f"非法正则 {expect!r}: {e}")
        else:  # contains
            ok = expect in text
        return VerifyResult(status="pass" if ok else "fail",
                            detail=f"text={text!r} expect={expect!r} op={v.op}")

    def _verify_state(self, v, step, ctx=None) -> VerifyResult:
        return self._poll(lambda remaining: self._verify_state_once(v, step, ctx, remaining),
                          v.timeout_ms, ctx)

    def _verify_state_once(self, v, step, ctx, remaining_ms) -> VerifyResult:
        """内部状态断言：模拟器/测试端口 query_state(path)。"""
        if self.emulator is None:
            return VerifyResult(status="fail", detail="state 断言需要模拟器端口")
        path = step.args.get("path", "/")
        expect = step.args.get("expect", v.expect)
        if expect is None:
            # 缺期望值不能拿 str(None)="None" 去比：断言恒 fail 且 detail 误导排查
            return VerifyResult(
                status="fail",
                detail="state 断言缺少期望值（args.expect / verify.expect）")
        try:
            bounded = getattr(self.emulator, "query_state_bounded", None)
            if callable(bounded):
                actual = bounded(path, timeout_s=remaining_ms / 1000,
                                 abort=ctx.abort if ctx is not None else None)
            else:
                actual = self.emulator.query_state(path)
        except AbortRequested:
            raise
        except Exception as e:
            return VerifyResult(status="fail", detail=f"状态查询失败: {e}")
        if v.op == "regex" and isinstance(expect, str):
            try:
                ok = bool(re.search(expect, str(actual)))
            except re.error as e:
                return VerifyResult(status="fail", detail=f"非法正则: {e}")
        else:
            ok = str(actual) == str(expect)
        return VerifyResult(status="pass" if ok else "fail",
                            detail=f"path={path} actual={actual!r} expect={expect!r}")

    # ---------- 视觉断言 ----------

    def _verify_vision(self, v, after, ctx=None) -> VerifyResult:
        if after is None:
            return VerifyResult(status="unknown", detail="无截图，无法视觉断言")
        if self.llm is None:
            return VerifyResult(status="unknown", detail="LLM 网关未装配")
        b64 = base64.b64encode(after.png).decode()
        try:
            scope = getattr(self.llm, "request_context", None)
            with scope(abort=ctx.abort if ctx else None,
                       timeout_s=max(0.001, v.timeout_ms / 1000)) if scope else nullcontext():
                out = self.llm.verify_vision(b64, v.expect or "")
        except AbortRequested:
            raise
        except Exception as e:
            return VerifyResult(status="unknown", detail=f"VLM 调用失败: {e}")
        conf = float(out.get("confidence", 0) or 0)
        min_conf = float(self.cfg.get("vision_min_confidence", 0.70))
        if not math.isfinite(conf) or not 0 <= conf <= 1 or conf < min_conf:
            return VerifyResult(status="unknown",
                                detail=f"低置信 {conf:.2f} < {min_conf}；"
                                       f"{out.get('reason', '')}",
                                confidence=conf)
        return VerifyResult(status="pass" if out.get("pass") else "fail",
                            detail=out.get("reason", ""), confidence=conf)

    # ---------- 截图差异 ----------

    def _verify_diff(self, before, after, v) -> VerifyResult:
        """只判"有没有变化"，不判变化对错（对错交后续 assert）。

        阈值与像素容差来自本模块常量，与 ``infra/imaging.diff_overlay`` 的
        预览图共用同一套语义（预览与判定不能各写一份阈值，否则"看着变了却判没变"）。
        """
        if before is None:
            return VerifyResult(status="unknown", detail="无 before 截图，无法验证 diff")
        if after is None:
            return VerifyResult(status="unknown", detail="无 after 截图")
        try:
            import numpy as np

            from testpilot.infra.imaging import decode_png
            from testpilot.infra.opencv import load_cv2
            cv2 = load_cv2()
        except Exception as e:  # pragma: no cover - 依赖缺失
            return VerifyResult(status="unknown", detail=f"OpenCV 不可用: {e}")

        a = decode_png(before.png, cv2.IMREAD_GRAYSCALE)
        b = decode_png(after.png, cv2.IMREAD_GRAYSCALE)
        if a is None or b is None:
            return VerifyResult(status="unknown", detail="截图解码失败")
        if a.shape != b.shape:
            return VerifyResult(status="pass", detail="画面尺寸变化（视为已变化）")
        diff = cv2.absdiff(a, b)
        ratio = float(np.count_nonzero(diff > _DIFF_PIXEL_DELTA)) / diff.size
        threshold = float(v.expect) if _is_float(v.expect) else _DEFAULT_DIFF_RATIO
        return VerifyResult(status="pass" if ratio > threshold else "fail",
                            detail=f"变化像素比 {ratio:.4f}（阈值 {threshold}）")


def _is_float(text) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False

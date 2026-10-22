"""失败归因 — 规则先行（零成本、可解释），LLM 兜底（新失败）（设计 8.9）。

流程（对齐设计 3.2/3.3）：
1. 知识库已命中（精确/模糊）-> 直接产出参考结论，**不重复入库**；
2. 规则层（零 token）-> 崩溃/环境/时序/控件变更等可解释结论；
3. 新失败送 LLM 归因 -> 必须人审（``pending``）后才进知识库；
   规则结论若 needs_review=False（如模拟器环境异常）则直接 ``active``。

类别集合见 ``CATEGORIES``；``disposition`` 映射决定命中后的既定处置
（``block_known`` 会让执行器把用例标记为 blocked 而非 fail）。
"""
import logging
import base64
import math
from pathlib import Path
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field

from testpilot.core.errors import AbortRequested
from testpilot.knowledge.service import FailureKnowledge

log = logging.getLogger("testpilot.judge")

CATEGORIES = (
    "app_crash",      # 被测程序崩溃/退出
    "uia_change",     # 界面/控件变更（元素库过期佐证）
    "timing",         # 时序/等待不足
    "env",            # 环境/模拟器异常
    "dsl_error",      # 用例本身问题
    "product_bug",    # 疑似真实缺陷
    "unknown",
)

# 类别 -> 命中后的既定处置
DISPOSITION = {
    "timing": "retry_ready",
    "product_bug": "block_known",
    "unknown": "none",
    "app_crash": "none",
    "uia_change": "none",
    "env": "none",
    "dsl_error": "none",
}


@dataclass
class RootCauseReport:
    category: str
    confidence: float
    evidence: str
    suggestion: str
    source: str          # rule | kb_exact | kb_fuzzy | llm
    needs_review: bool
    similar: list[dict] = field(default_factory=list)   # 相似历史对照（M4-5）

    def to_dict(self) -> dict:
        return asdict(self)


class Judge:
    def __init__(self, llm, kb, cfg: dict):
        self.llm = llm
        self.kb = kb
        self.cfg = cfg or {}
        self.similar_top_n = int((cfg or {}).get("knowledge", {}).get(
            "similar_top_n", 3))

    def analyze(self, res, sig, kb_hit, ctx) -> RootCauseReport:
        if kb_hit is not None:
            # 已知/相似失败：直接给参考，不入库（避免污染）
            report = RootCauseReport(
                category=kb_hit.category if kb_hit.category in CATEGORIES
                else "unknown",
                confidence=kb_hit.confidence,
                evidence=f"相似历史失败 {kb_hit.signature}（entry#{kb_hit.entry_id}）",
                suggestion=kb_hit.suggestion,
                source="kb_fuzzy",
                needs_review=True)
            log.info("知识库命中，跳过新归因",
                     extra={"sig": sig.digest(), "kb_hit": kb_hit.entry_id})
            self._attach_similar(report, sig, exclude=kb_hit.entry_id)
            return report

        # 1) 规则层：零 token，立即可解释
        report = self._rule_pass(res, ctx)
        if report is None:
            report = self._llm_pass(res, sig, ctx)
        elif report.confidence < 0.6:
            candidate = self._llm_pass(res, sig, ctx)
            if candidate.confidence > report.confidence and candidate.category != "unknown":
                candidate.evidence += f"\n规则线索：{report.evidence}"
                report = candidate
            report.needs_review = True

        log.info("归因完成",
                 extra={"sig": sig.digest(), "status": report.category,
                        "kb_hit": 0 if report.needs_review else -1})
        self._attach_similar(report, sig)
        self._persist(sig, report, res, ctx)
        return report

    def _attach_similar(self, report: RootCauseReport, sig,
                        exclude: int | None = None) -> None:
        """挂上相似历史（报告与 RunDetail 的"相似历史对照"，不参与自动判定）。"""
        try:
            refs = self.kb.similar_failures(sig, limit=self.similar_top_n + 1)
        except Exception:       # 历史对照是增强信息，失败不影响归因
            log.warning("相似历史查询失败(不阻断)", exc_info=True)
            return
        report.similar = [f.as_ref() for f in refs if f.entry_id != exclude
                          ][:self.similar_top_n]

    # ---------- 规则层 ----------

    def _rule_pass(self, res, ctx):
        err = res.error or {}
        etype = err.get("type", "")
        msg = str(err.get("message", ""))

        if etype == "app_error" and ("崩溃" in msg or "crash" in msg.lower()):
            return RootCauseReport("app_crash", 1.0,
                                   evidence=msg or "被测程序异常退出",
                                   suggestion="检查被测程序 dump 与崩溃日志",
                                   source="rule", needs_review=False)
        if etype == "device_error":
            return RootCauseReport("env", 0.9, evidence=msg,
                                   suggestion="检查模拟器连接、场景加载与指令参数",
                                   source="rule", needs_review=False)
        if etype == "locate_timeout":
            return RootCauseReport(
                "uia_change", 0.6,
                evidence=f"全部驱动未定位 {res.target_json or ''}",
                suggestion="核对控件 accessibleName/automation_id 是否变更；"
                           "必要时补充视觉 fallback 或标记 vision_only_hint",
                source="rule", needs_review=True)
        if etype == "dsl_error":
            return RootCauseReport("dsl_error", 0.8, evidence=msg,
                                   suggestion="修正 DSL（缺 target / 动作名拼写）",
                                   source="rule", needs_review=True)
        if etype == "verify_failed":
            detail = (res.verify.detail if res.verify else "") or ""
            return RootCauseReport(
                "timing", 0.5,
                evidence=f"动作完成但断言未通过：{detail}",
                suggestion="确认是否需要 ui.wait 等待就绪；若界面确已变更则属产品缺陷",
                source="rule", needs_review=True)
        return None

    # ---------- LLM 层 ----------

    def _llm_pass(self, res, sig, ctx) -> RootCauseReport:
        shot_paths = [p for p in (res.screenshot_before, res.screenshot_after) if p]
        if self.llm is None:
            return RootCauseReport(
                "unknown", 0.3, evidence="LLM 网关未装配",
                suggestion="人工确认失败原因", source="rule", needs_review=True)
        try:
            images, missing = self._evidence_images(shot_paths)
            scope = getattr(self.llm, "request_context", None)
            with scope(abort=ctx.abort) if scope else nullcontext():
                draft = self.llm.judge_failure({
                "signature": str(sig),
                "case_id": ctx.case.case_id if ctx.case else None,
                "step": {"id": res.step_id, "action": res.action,
                         "driver": res.driver, "source_text": res.source_text,
                         "error": res.error,
                         "verify": res.verify.detail if res.verify else None},
                "run_id": ctx.run_id,
                "screenshots": shot_paths,
                "images": images,
                "analysis_mode": "with_images" if images else "text_only",
                "image_errors": missing,
                # 人审维护的用例上下文（已知坑/备注）：让归因带着"人已经知道的事"，
                # 而不是每次都从零猜（case_ctx 的消费点之一）
                "case_ctx": _case_ctx_brief(ctx),
            }) or {}
        except AbortRequested:
            raise
        except Exception as e:
            log.exception("LLM 归因失败，回落 unknown")
            return RootCauseReport("unknown", 0.2, evidence=f"LLM 调用失败: {e}",
                                   suggestion="人工确认失败原因", source="rule",
                                   needs_review=True)

        category = draft.get("category", "unknown")
        if category not in CATEGORIES:
            category = "unknown"
        return RootCauseReport(
            category=category,
            confidence=_as_float(draft.get("confidence"), 0.5),
            evidence=(("图像证据不可用，仅文本分析。 " if shot_paths and not images else "")
                      + str(draft.get("evidence", ""))),
            suggestion=str(draft.get("suggestion", "")),
            source="llm",
            needs_review=True)   # LLM 归因必须人审后才入知识库

    def _evidence_images(self, paths):
        root = Path(self.cfg.get("app", {}).get("screenshot_root", "./data/shots")).resolve()
        images, errors = [], []
        for rel in paths[:2]:
            try:
                path = (root / rel).resolve()
                if not path.is_relative_to(root) or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("截图越界或过大")
                data = path.read_bytes()
                from testpilot.infra.imaging import decode_png
                decode_png(data)
                images.append(base64.b64encode(data).decode("ascii"))
            except (OSError, ValueError) as exc:
                errors.append(f"{rel}: {exc}")
        return images, errors

    # ---------- 入库 ----------

    def _persist(self, sig, report: RootCauseReport, res, ctx) -> None:
        fk = FailureKnowledge(
            entry_id=0, signature=sig.digest(), category=report.category,
            disposition=DISPOSITION.get(report.category, "none"),
            suggestion=report.suggestion, confidence=report.confidence,
            error_type=sig.error_type, action=sig.action,
            target_key=sig.target_key, page_key=sig.page_key,
            source_run_id=getattr(ctx, "run_id", None))
        entry_id = self.kb.save_failure(fk)
        if not report.needs_review:
            # 规则高置信（如模拟器环境异常）直接生效，否则留 pending 待人审。
            # 人审明确拒绝过的条目不得被规则归因自动复活。
            if self.kb.status_of(entry_id) != "rejected":
                self.kb.confirm(entry_id, "active")
                auto = True
            else:
                auto = False
        else:
            auto = False
        log.info(f"失败知识入库: entry#{entry_id} "
                 f"{'active' if auto else 'pending'}")


def _case_ctx_brief(ctx) -> dict | None:
    """归因输入用的用例上下文摘要（只带 LLM 用得上的字段）。"""
    entry = (getattr(ctx, "extra", None) or {}).get("case_ctx")
    if not entry:
        return None
    brief = {"case_id": entry.get("case_id"),
             "scene": entry.get("scene") or None,
             "pitfalls": list(entry.get("pitfalls") or []),
             "notes": entry.get("notes") or ""}
    return brief if any(brief.get(k) for k in ("scene", "pitfalls", "notes")) else None


def _as_float(value, default: float) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1 else default
    except (TypeError, ValueError):
        return default

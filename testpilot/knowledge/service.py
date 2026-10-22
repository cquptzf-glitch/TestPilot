"""知识服务 — 元素库/失败库/用例上下文/环境矩阵的读写、匹配、路由反馈（设计 8.14 / 第 9 章）。

匹配三级：
1. **精确**：签名唯一索引 ``uk_kb_sig`` 直查（同 error_type+action+target+page）
2. **模糊**：同 ``error_type`` + ``action``，且 target 前缀相似 -> 置信度 x0.8 待复核
3. **语义（预留钩子）**：注入 ``EmbeddingProvider`` 时对规则未命中做向量召回，
   置信度再乘 ``EMBED_DECAY``；未注入时保持纯规则行为（设计 9.2）

四类条目（设计 9.1）：
- ``element``：定位成功自动积累，无需人审；
- ``failure``：LLM 归因后入 ``pending``，人审后生效；
- ``case_ctx``：**人审界面维护**的前置依赖（模拟器场景）/已知坑/备注，执行前预取注入；
- ``env_compat``：每次 run 汇总写入的「被测版本 × 用例集」历史通过率矩阵，无需人审。

置信衰减：元素知识超过 ``element_stale_days`` 未有成功命中的记录 -> 标记
``prefer_vision``，路由据此跳过 UIA 优先（M3 闭环，此处提供机制）。
命中失败库会累加 ``hits``（KB 命中率统计源）。
"""
import difflib
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("testpilot.kb")

# 命中已知失败后的处置策略（与 judge / executor 的消费口径一致）
DISPOSITIONS = ("block_known", "retry_ready", "skip", "none")

# case_ctx 允许的人审字段（设计 9.1：前置依赖（模拟器场景）、已知坑、人审备注）
CASECTX_FIELDS = ("scene", "pitfalls", "notes")

FUZZY_DECAY = 0.8          # 模糊命中置信度衰减
EMBED_DECAY = 0.8          # 语义召回置信度衰减（与模糊同口径）
STALE_NEVER = 999          # 无 last_success_at 时视为长期未命中


class EmbeddingProvider(Protocol):
    """失败库语义召回通道（设计 9.2 预留钩子）。

    实现方拿到签名后返回 ``[(失败签名, 相似度 0~1), ...]``（按相似度降序）。
    引擎只把它当"候选建议"：命中结果仍需人审，且置信度再乘 ``EMBED_DECAY``。
    未注入实现时，匹配保持纯规则（精确 + 前缀模糊）。
    """

    def similar(self, sig, limit: int) -> list[tuple[str, float]]: ...


@dataclass
class StatsRow:
    driver: str
    success_count: int
    fail_count: int
    fail_streak: int
    avg_locate_ms: int


@dataclass
class ElementKnowledge:
    key: str
    prefer_vision: bool = False
    payload: dict | None = None
    entry_id: int | None = None


@dataclass
class FailureKnowledge:
    entry_id: int
    signature: str
    category: str
    disposition: str          # block_known | retry_ready | skip | none
    suggestion: str
    confidence: float
    error_type: str = ""
    action: str = ""
    target_key: str = ""
    page_key: str = ""
    hits: int = 0
    source_run_id: int | None = None   # 条目来源 run（溯源列 source_run_id 的真实语义）
    match_kind: str = "exact"          # exact | fuzzy | embed（命中来源，决定是否自动处置）

    def to_payload(self) -> dict:
        return {"category": self.category, "disposition": self.disposition,
                "suggestion": self.suggestion, "confidence": self.confidence,
                "error_type": self.error_type, "action": self.action,
                "target_key": self.target_key, "page_key": self.page_key}

    def as_ref(self) -> dict:
        """历史对照用的轻量引用（报告/RunDetail 的"相似历史"区）。"""
        return {"entry_id": self.entry_id, "signature": self.signature,
                "category": self.category, "suggestion": self.suggestion,
                "confidence": self.confidence, "hits": self.hits}

    @classmethod
    def from_row(cls, row) -> "FailureKnowledge":
        payload = _loads(row[2], {}) if not isinstance(row[2], dict) else row[2]
        hits = int(row[3]) if len(row) > 3 and row[3] is not None else 0
        loaded = FailureKnowledge(
            entry_id=row[0], signature=row[1],
            category=payload.get("category", "unknown"),
            disposition=payload.get("disposition", "none"),
            suggestion=payload.get("suggestion", ""),
            confidence=float(payload.get("confidence", 0.5)),
            error_type=payload.get("error_type", ""),
            action=payload.get("action", ""),
            target_key=payload.get("target_key", ""),
            page_key=payload.get("page_key", ""),
            hits=hits)
        return loaded


class CaseHints:
    """执行前预取结果（元素库 + 失败库 + 用例上下文 + 方案级驱动模式）。"""

    def __init__(self, elements=None, failures=None, case_ctx=None,
                 driver_mode: str | None = None):
        self.elements = elements or []      # list[ElementKnowledge]
        self.failures = failures or []      # list[FailureKnowledge]
        self.case_ctx = case_ctx or []      # list[dict]
        self.driver_mode = driver_mode      # 方案/任务级驱动模式覆盖（路由三级覆盖）


class KnowledgeService:
    def __init__(self, conn: sqlite3.Connection, cfg: dict,
                 embedding: EmbeddingProvider | None = None):
        self.conn = conn
        self.cfg = cfg or {}
        self.fuzzy_top_n = int(self.cfg.get("fuzzy_top_n", 3))
        self.stale_days = int(self.cfg.get("element_stale_days", 30))
        self.embedding = embedding      # 可选语义召回（设计 9.2 钩子）

    # ---------- 预取（执行前注入） ----------

    def prefetch(self, case_ids: list[str],
                 driver_mode: str | None = None) -> CaseHints:
        elements = []
        for sig, payload_json, last_ok in self.conn.execute(
                "SELECT signature, payload_json, last_success_at FROM kb_entry "
                "WHERE type='element' AND status='active'"):
            payload = _loads(payload_json, {})
            stale = _days_since(last_ok) > self.stale_days
            elements.append(ElementKnowledge(
                key=sig, prefer_vision=stale or bool(payload.get("prefer_vision")),
                payload=payload))

        failures = [FailureKnowledge.from_row(r) for r in self.conn.execute(
            "SELECT id, signature, payload_json, hits FROM kb_entry "
            "WHERE type='failure' AND status='active'")]

        case_ctx = []
        if case_ids:
            placeholders = ",".join("?" * len(case_ids))
            case_ctx = [{**_loads(p, {}), "case_id": sig} for sig, p in self.conn.execute(
                f"SELECT signature, payload_json FROM kb_entry WHERE type='case_ctx'"
                f" AND status='active' AND signature IN ({placeholders})",
                list(case_ids))]
        return CaseHints(elements=elements, failures=failures, case_ctx=case_ctx,
                         driver_mode=driver_mode)

    # ---------- 元素库 ----------

    def query_element(self, key: str) -> ElementKnowledge | None:
        row = self.conn.execute(
            "SELECT id, payload_json, last_success_at FROM kb_entry "
            "WHERE type='element' AND status='active' AND signature=?",
            (key,)).fetchone()
        if row is None:
            return None
        payload = _loads(row[1], {})
        stale = _days_since(row[2]) > self.stale_days
        # 与 prefetch 同口径：超期降权之外也尊重 learn_element 写入的视觉偏好
        return ElementKnowledge(
            key=key, prefer_vision=stale or bool(payload.get("prefer_vision")),
            payload=payload, entry_id=row[0])

    def save_element(self, ek: ElementKnowledge) -> None:
        """写入/刷新元素知识（定位成功时调用，M3-1 自动积累）。"""
        now = _now()
        self.conn.execute(
            "INSERT INTO kb_entry(type, signature, payload_json, status,"
            " last_success_at, updated_at) VALUES ('element',?,?,'active',?,?)"
            " ON CONFLICT(type, signature) DO UPDATE SET"
            " payload_json=excluded.payload_json,"
            " last_success_at=excluded.last_success_at,"
            " status='active', updated_at=excluded.updated_at",
            (ek.key, json.dumps(ek.payload or {}, ensure_ascii=False), now, now))
        self.conn.commit()

    def learn_element(self, key: str, payload: dict) -> None:
        """定位成功后的元素知识更新（Executor 学习反馈入口）。"""
        self.save_element(ElementKnowledge(key=key, payload=payload))

    # ---------- 失败库 ----------

    def match_failure(self, sig) -> FailureKnowledge | None:
        digest = sig.digest()
        row = self.conn.execute(
            "SELECT id, signature, payload_json, hits FROM kb_entry "
            "WHERE type='failure' AND status='active' AND signature=?",
            (digest,)).fetchone()
        if row is not None:
            self._bump_hits(row[0])
            return FailureKnowledge.from_row(row)

        # 模糊：同 error_type + action，target 相似（顶级类别一致 + 尾段相近）
        candidates = []
        for r in self.conn.execute(
                "SELECT id, signature, payload_json, hits FROM kb_entry "
                "WHERE type='failure' AND status='active'"):
            fk = FailureKnowledge.from_row(r)
            if fk.error_type != sig.error_type or fk.action != sig.action:
                continue
            if not _target_similar(fk.target_key, sig.target_key,
                                   sig.page_key, fk.page_key):
                continue
            candidates.append(fk)
        if not candidates:
            return self._embedding_match(sig, digest)
        candidates.sort(key=lambda f: f.confidence, reverse=True)
        best = candidates[0]
        best.confidence *= FUZZY_DECAY
        best.match_kind = "fuzzy"    # 非精确命中：执行器不得自动 block/retry
        self._bump_hits(best.entry_id)
        log.info("失败库模糊命中", extra={"sig": digest, "kb_hit": best.entry_id})
        return best

    def _embedding_match(self, sig, digest: str) -> FailureKnowledge | None:
        """规则未命中时的语义召回（未注入 provider 时保持纯规则行为）。"""
        if self.embedding is None:
            return None
        try:
            hits = self.embedding.similar(sig, self.fuzzy_top_n)
        except Exception as e:          # 召回是增强通道：失败必须降级为纯规则
            log.warning(f"embedding 召回失败(降级纯规则): {e}")
            return None
        for item in hits or []:
            try:
                entry_sig, score = item
            except (TypeError, ValueError):
                continue
            entry_sig = str(entry_sig)
            if entry_sig == digest:
                continue                # 不回引自身
            row = self.conn.execute(
                "SELECT id, signature, payload_json FROM kb_entry"
                " WHERE type='failure' AND status='active' AND signature=?",
                (entry_sig,)).fetchone()
            if row is None:
                continue
            fk = FailureKnowledge.from_row(row)
            ratio = max(0.0, min(1.0, float(score)))
            fk.confidence = round(fk.confidence * ratio * EMBED_DECAY, 6)
            fk.match_kind = "embed"   # 语义召回是候选建议：不参与自动处置
            self._bump_hits(fk.entry_id)
            log.info("失败库语义召回", extra={"sig": digest, "kb_hit": fk.entry_id})
            return fk
        return None

    def save_failure(self, fk: FailureKnowledge) -> int:
        """写入失败知识（默认 pending：LLM 归因必须人审后才生效）。

        返回该签名的 entry id（ON CONFLICT 走 UPDATE 时 lastrowid 不可靠，
        统一回查保证调用方可直接用于 ``confirm``）。
        """
        self.conn.execute(
            "INSERT INTO kb_entry(type, signature, payload_json, status,"
            " hits, confidence, source_run_id, updated_at)"
            " VALUES ('failure',?,?,'pending',0,?,?,?)"
            " ON CONFLICT(type, signature) DO UPDATE SET"
            " payload_json=excluded.payload_json,"
            " confidence=excluded.confidence, updated_at=excluded.updated_at",
            (fk.signature, json.dumps(fk.to_payload(), ensure_ascii=False),
             fk.confidence, fk.source_run_id, _now()))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM kb_entry WHERE type='failure' AND signature=?",
            (fk.signature,)).fetchone()
        return int(row["id"]) if row is not None else 0

    def confirm(self, entry_id: int, verdict: str) -> None:
        """人审入口：``active`` 生效 / ``rejected`` 留档不删。"""
        if verdict not in ("active", "rejected", "stale", "pending"):
            raise ValueError(f"非法 verdict: {verdict}")
        self.conn.execute(
            "UPDATE kb_entry SET status=?, updated_at=? WHERE id=?",
            (verdict, _now(), entry_id))
        self.conn.commit()

    def status_of(self, entry_id: int) -> str | None:
        """条目当前状态（自动激活前检查"人审是否明确拒绝过"用）。"""
        row = self.conn.execute("SELECT status FROM kb_entry WHERE id=?",
                                (entry_id,)).fetchone()
        return row["status"] if row is not None else None

    def pending(self) -> list[dict]:
        from testpilot.storage.repos import kb_pending
        return kb_pending(self.conn)

    def set_disposition(self, entry_id: int, disposition: str, *,
                        category: str | None = None, suggestion: str | None = None,
                        evidence: str | None = None) -> dict:
        """人审阶段修正处置策略（设计 3.3：处置由"归因草稿 + 人审"共同决定）。

        处置策略直接影响命中后的引擎行为（block_known -> blocked / retry_ready -> 重试），
        因此不让人审改就等于把人审降级成"只能全盘接受 LLM 结论"。
        """
        if disposition not in DISPOSITIONS:
            raise ValueError(f"非法处置策略: {disposition}"
                             f"（可选 {', '.join(DISPOSITIONS)}）")
        row = self.conn.execute(
            "SELECT payload_json FROM kb_entry WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError(f"kb_entry 不存在: {entry_id}")
        payload = _loads(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        payload["disposition"] = disposition
        if category is not None:
            from testpilot.engine.judge import CATEGORIES
            if category not in CATEGORIES:
                raise ValueError("未知根因类别")
        changes = {key: value for key, value in
                   (("category", category), ("suggestion", suggestion), ("evidence", evidence))
                   if value is not None}
        if changes:
            payload.setdefault("review_history", []).append({
                "at": _now(), "previous": {key: payload.get(key) for key in changes}})
            payload.update(changes)
        self.conn.execute(
            "UPDATE kb_entry SET payload_json=?, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), _now(), entry_id))
        self.conn.commit()
        log.info(f"知识条目处置已更新: entry#{entry_id} -> {disposition}")
        return {"entry_id": entry_id, "disposition": disposition}

    def similar_failures(self, sig, limit: int = 3) -> list[FailureKnowledge]:
        """相似历史失败（供报告/RunDetail 的"相似历史对照"，不参与自动处置）。

        相似判据：同 ``action``，且（同 ``error_type`` 或 target 前缀一致）。
        排序：置信度降序 -> entry id 降序（新的优先）。
        """
        digest = sig.digest()
        pre_sig = _target_prefix(sig.target_key)
        out: list[FailureKnowledge] = []
        for r in self.conn.execute(
                "SELECT id, signature, payload_json, hits FROM kb_entry"
                " WHERE type='failure' AND status IN ('active','rejected')"):
            fk = FailureKnowledge.from_row(r)
            if fk.signature == digest:
                continue
            if not fk.action or fk.action != sig.action:
                continue
            same_type = bool(fk.error_type) and fk.error_type == sig.error_type
            same_target = bool(pre_sig) and _target_prefix(fk.target_key) == pre_sig
            if same_type or same_target:
                out.append(fk)
        out.sort(key=lambda f: (f.confidence, f.entry_id), reverse=True)
        return out[:max(0, limit)]

    def _bump_hits(self, entry_id: int) -> None:
        self.conn.execute("UPDATE kb_entry SET hits=hits+1, updated_at=?"
                          " WHERE id=?", (_now(), entry_id))
        self.conn.commit()

    # ---------- 用例上下文（case_ctx，人审维护） ----------

    def list_case_ctx(self, case_id: str | None = None) -> list[dict]:
        """列出人审维护的用例上下文；``case_id`` 支持本地 ID（TC-1024）或禅道数字 ID。"""
        sql = ("SELECT e.id, e.signature, e.payload_json, e.status, e.updated_at,"
               " c.title AS case_title, c.zentao_case_id AS zentao_case_id"
               " FROM kb_entry e LEFT JOIN dsl_case c ON c.case_id=e.signature"
               " WHERE e.type='case_ctx'")
        args: list = []
        if case_id:
            text = str(case_id).strip()
            sql += " AND (e.signature=? OR c.zentao_case_id=?)"
            args = [text, _as_int(text)]
        sql += " ORDER BY e.signature"
        out = []
        for row in self.conn.execute(sql, args):
            d = dict(row)
            d["case_id"] = d["signature"]
            d["payload"] = _loads(d.pop("payload_json", None), {})
            out.append(d)
        return out

    def get_case_ctx(self, case_id: str) -> dict | None:
        entries = self.list_case_ctx(case_id)
        return entries[0] if entries else None

    def save_case_ctx(self, case_id: str, payload: dict) -> dict:
        """人审维护入口：写入/更新用例上下文（设计 9.1 case_ctx）。

        语义：**按字段增量合并**；显式传空值（``""``/``[]``）即清除该字段。
        全空时不写空条目（知识库不应留下"没有内容"的上下文）。
        """
        local_id, zentao_id, _ = self._resolve_case_id(case_id)
        incoming = _clean_case_ctx(payload)
        row = self.conn.execute(
            "SELECT payload_json FROM kb_entry"
            " WHERE type='case_ctx' AND signature=?", (local_id,)).fetchone()
        merged = dict(_loads(row["payload_json"], {})) if row is not None else {}
        for key, value in incoming.items():
            if value in (None, "", []):
                merged.pop(key, None)
            else:
                merged[key] = value
        if not any(merged.get(key) for key in CASECTX_FIELDS):
            raise ValueError("用例上下文内容为空：至少提供 scene/pitfalls/notes 之一")
        merged["zentao_case_id"] = zentao_id
        merged["source"] = "human_review"
        now = _now()
        self.conn.execute(
            "INSERT INTO kb_entry(type, signature, payload_json, status,"
            " updated_at) VALUES ('case_ctx',?,?,'active',?)"
            " ON CONFLICT(type, signature) DO UPDATE SET"
            " payload_json=excluded.payload_json, status='active',"
            " updated_at=excluded.updated_at",
            (local_id, json.dumps(merged, ensure_ascii=False), now))
        self.conn.commit()
        log.info(f"用例上下文已保存: {local_id}")
        return {"case_id": local_id, "zentao_case_id": zentao_id,
                "payload": merged}

    def delete_case_ctx(self, case_id: str) -> dict:
        local_id, _, _ = self._resolve_case_id(case_id)
        cur = self.conn.execute(
            "DELETE FROM kb_entry WHERE type='case_ctx' AND signature=?",
            (local_id,))
        self.conn.commit()
        log.info(f"用例上下文已删除: {local_id} rows={cur.rowcount}")
        return {"case_id": local_id, "deleted": int(cur.rowcount)}

    def _resolve_case_id(self, case_id) -> tuple[str, int | None, str]:
        """把本地 ID / 禅道数字 ID 归一为本地 case_id（用例必须已入库）。"""
        text = str(case_id or "").strip()
        if not text:
            raise ValueError("case_id 不能为空")
        row = self.conn.execute(
            "SELECT case_id, zentao_case_id, title FROM dsl_case"
            " WHERE case_id=? OR zentao_case_id=?",
            (text, _as_int(text))).fetchone()
        if row is None:
            raise ValueError(f"用例不存在（先 pull 入库）: {text}")
        return row["case_id"], row["zentao_case_id"], row["title"]

    # ---------- 环境通过率矩阵（env_compat，每次 run 汇总） ----------

    def record_env_compat(self, *, run_id: int, test_app_version: str | None,
                          suite_id: int | None, case_ids: list[str],
                          statuses: dict[str, str],
                          driver_mode: str | None = None,
                          emulator_version: str | None = None,
                          condition: dict | None = None) -> dict:
        """把一次 run 的用例结果并入「被测版本 × 用例集」矩阵（设计 9.1）。

        签名 = ``{被测版本}|suite:{id}``（无方案时用用例集摘要）。
        ``runs`` 只保留最近 ``_ENV_RUNS_KEEP`` 次，避免 payload 无界增长。
        """
        version = str(test_app_version or "unknown")
        suite_key = (f"suite:{suite_id}" if suite_id
                     else f"cases:{_cases_digest(case_ids)}")
        signature = f"{version}|{suite_key}"
        if condition:
            import hashlib
            fingerprint = hashlib.sha256(json.dumps(condition, sort_keys=True).encode()).hexdigest()[:16]
            signature += f"|condition:{fingerprint}"
        row = self.conn.execute(
            "SELECT payload_json FROM kb_entry"
            " WHERE type='env_compat' AND signature=?", (signature,)).fetchone()
        payload = dict(_loads(row["payload_json"], {})) if row is not None else {}
        payload.update({
            "test_app_version": version,
            "suite_id": suite_id,
            "case_ids": list(case_ids),
            "condition": condition,
        })
        # 元信息按"有值才覆盖"合并：None 表示本次未采集到，不该抹掉历史值
        for key, value in (("driver_mode", driver_mode),
                           ("emulator_version", emulator_version)):
            if value is not None:
                payload[key] = value
        matrix = payload.setdefault("matrix", {})
        for cid in case_ids:
            status = statuses.get(cid)
            if status is None:
                continue
            cell = matrix.setdefault(cid, {"pass": 0, "fail": 0, "other": 0,
                                           "total": 0})
            cell["total"] = int(cell.get("total", 0)) + 1
            if status == "pass":
                cell["pass"] = int(cell.get("pass", 0)) + 1
            elif status == "fail":
                cell["fail"] = int(cell.get("fail", 0)) + 1
            else:
                cell["other"] = int(cell.get("other", 0)) + 1
            cell["last_status"] = status
            cell["last_run_id"] = run_id
        runs = [r for r in payload.get("runs", []) if r != run_id]
        runs.append(run_id)
        payload["runs"] = runs[-_ENV_RUNS_KEEP:]
        payload["updated_at"] = _now()
        now = _now()
        self.conn.execute(
            "INSERT INTO kb_entry(type, signature, payload_json, status,"
            " source_run_id, updated_at) VALUES ('env_compat',?,?,'active',?,?)"
            " ON CONFLICT(type, signature) DO UPDATE SET"
            " payload_json=excluded.payload_json, status='active',"
            " source_run_id=excluded.source_run_id,"
            " updated_at=excluded.updated_at",
            (signature, json.dumps(payload, ensure_ascii=False), run_id, now))
        self.conn.commit()
        return {"signature": signature, "runs": len(payload["runs"]),
                "cases": len(case_ids)}

    def env_compat_matrix(self, *, test_app_version: str | None = None,
                          suite_id: int | None = None,
                          case_ids: list[str] | None = None,
                          limit: int = 12) -> list[dict]:
        """查询环境矩阵：优先按方案取全部版本；无方案时按用例集精确匹配。

        ``test_app_version`` 是**硬过滤**（给了就只返回该版本）；矩阵视图不传它，
        以便拿到同一方案下各版本的历史对照。
        """
        out: list[dict] = []
        rows = self.conn.execute(
            "SELECT id, signature, payload_json, updated_at FROM kb_entry"
            " WHERE type='env_compat' ORDER BY updated_at DESC, id DESC"
            " LIMIT ?", (max(1, int(limit)) * 5,))
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if test_app_version is not None and \
                    payload.get("test_app_version") != test_app_version:
                continue
            if suite_id is not None:
                if payload.get("suite_id") != suite_id:
                    continue
            elif case_ids:
                if set(payload.get("case_ids") or []) != set(case_ids):
                    continue
            out.append({"id": row["id"], "signature": row["signature"],
                        "updated_at": row["updated_at"], "payload": payload})
            if len(out) >= limit:
                break
        return out

    # ---------- 路由统计 ----------

    def feedback_router(self, key: str, driver: str, ok: bool, ms: int) -> None:
        """一次定位尝试的结果回写（成功清零 fail_streak，失败累加）。

        ``avg_locate_ms`` 用**增量均值**（旧均值 x 旧样本数 + 本次）/ 新样本数，
        而不是 (旧+新)/2 —— 后者是 EMA，样本多时会被单次抖动带偏。
        """
        if not key or not driver:
            return
        self.conn.execute(
            "INSERT INTO router_stats(target_key, driver, success_count,"
            " fail_count, fail_streak, avg_locate_ms, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(target_key, driver) DO UPDATE SET"
            " success_count=success_count+excluded.success_count,"
            " fail_count=fail_count+excluded.fail_count,"
            " fail_streak=CASE WHEN excluded.fail_count>0 THEN fail_streak+1"
            "                  ELSE 0 END,"
            " avg_locate_ms=CAST((avg_locate_ms * (success_count + fail_count)"
            "   + excluded.avg_locate_ms) / (success_count + fail_count + 1)"
            "   AS INTEGER),"
            " updated_at=excluded.updated_at",
            (key, driver, 1 if ok else 0, 0 if ok else 1, 0 if ok else 1,
             int(ms or 0), _now()))
        self.conn.commit()

    def load_router_stats(self, target_key: str) -> dict[str, StatsRow]:
        out: dict[str, StatsRow] = {}
        for driver, sc, fc, streak, avg_ms in self.conn.execute(
                "SELECT driver, success_count, fail_count, fail_streak,"
                " avg_locate_ms FROM router_stats WHERE target_key=?",
                (target_key,)):
            out[driver] = StatsRow(driver, sc, fc, streak, avg_ms)
        return out


# ---------- 内部 ----------

_ENV_RUNS_KEEP = 20        # env_compat payload 里保留的最近 run 数（防无界增长）

#: 模糊匹配的尾段相似度下限（改名/派生控件仍可命中，完全不同的控件不互相误伤）
_FUZZY_TARGET_FLOOR = 0.6


def _target_similar(a: str, b: str, page_a: str = "", page_b: str = "") -> bool:
    """模糊匹配的目标相似判据。

    旧实现只比顶级类别（``btn.*`` 与任何 ``btn.*`` 都互相命中），
    ``btn.submit`` 的失败知识会误命中 ``btn.cancel``。现在要求：
    顶级类别一致 + 尾段相似（``btn.start_detect`` ↔ ``btn.start_detect2``
    这类改名控件仍命中），且 page_key 都有值且不同时判不相似。
    """
    if page_a and page_b and page_a != page_b:
        return False
    head_a = a.split(".", 1)[0] if a else ""
    head_b = b.split(".", 1)[0] if b else ""
    if not head_a or head_a != head_b:
        return False
    tail_a = a.rsplit(".", 1)[-1]
    tail_b = b.rsplit(".", 1)[-1]
    if tail_a == tail_b:
        return True
    return difflib.SequenceMatcher(None, tail_a, tail_b).ratio() \
        >= _FUZZY_TARGET_FLOOR


def _target_prefix(target_key: str) -> str:
    """取 target_key 的父前缀（``btn.start_detect`` -> ``btn``），用于相似度粗判。"""
    if not target_key:
        return ""
    return target_key.rsplit(".", 1)[0] if "." in target_key else target_key


def _as_int(text) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _cases_digest(case_ids) -> str:
    """用例集摘要（无方案任务的环境矩阵签名后缀）。"""
    joined = "\n".join(sorted(str(c) for c in (case_ids or [])))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]


def _clean_case_ctx(payload: dict) -> dict:
    """校验并归一 case_ctx 人审字段；pitfalls 接受列表或 ``;``/换行分隔的字符串。"""
    if not isinstance(payload, dict):
        raise ValueError("case_ctx payload 需为对象")
    unknown = set(payload) - set(CASECTX_FIELDS)
    if unknown:
        raise ValueError(f"未知字段: {sorted(unknown)}"
                         f"（可选 {', '.join(CASECTX_FIELDS)}）")
    out: dict = {}
    for key, value in payload.items():
        if key == "pitfalls":
            if value is None:
                out[key] = []
            elif isinstance(value, str):
                out[key] = [p.strip() for p in re.split(r"[;\n]", value) if p.strip()]
            elif isinstance(value, (list, tuple)):
                out[key] = [str(p).strip() for p in value if str(p).strip()]
            else:
                raise ValueError("pitfalls 需为字符串列表")
        else:
            out[key] = "" if value is None else str(value).strip()
    return out


def _loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _days_since(ts: str | None) -> int:
    if not ts:
        return STALE_NEVER
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return STALE_NEVER
    return int((time.time() - t) / 86400)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

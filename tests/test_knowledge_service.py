"""知识服务单测 — 精确/模糊匹配、元素库、路由统计（设计 8.14 / 第 9 章）。"""
import time

from testpilot.knowledge.service import (CaseHints, FailureKnowledge,
                                         KnowledgeService)
from testpilot.knowledge.signature import FailureSignature


def _fk(sig, **kw):
    base = dict(entry_id=0, signature=sig.digest(), category="uia_change",
                disposition="block_known", suggestion="等待就绪", confidence=1.0,
                error_type=sig.error_type, action=sig.action,
                target_key=sig.target_key, page_key=sig.page_key)
    base.update(kw)
    return FailureKnowledge(**base)


def test_save_and_exact_match_increments_hits(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("verify_failed", "ui.click", "btn.start_detect", "main")
    entry_id = kb.save_failure(_fk(sig))
    kb.confirm(entry_id, "active")

    hit = kb.match_failure(sig)
    assert hit is not None
    assert hit.disposition == "block_known"
    assert hit.entry_id == entry_id
    assert hit.error_type == "verify_failed"
    assert hit.action == "ui.click"
    assert hit.target_key == "btn.start_detect"

    hits = mem_conn.execute("SELECT hits FROM kb_entry WHERE id=?",
                            (entry_id,)).fetchone()["hits"]
    assert hits == 1


def test_pending_entry_is_not_matched(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("verify_failed", "ui.click", "btn.a", "main")
    kb.save_failure(_fk(sig))            # 默认 pending，未人审
    assert kb.match_failure(sig) is None


def test_fuzzy_match_decays_confidence(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    base = FailureSignature("verify_failed", "ui.click", "btn.start_detect", "main")
    kb.confirm(kb.save_failure(_fk(base, disposition="none")), "active")

    near = FailureSignature("verify_failed", "ui.click", "btn.start_detect2", "main")
    hit = kb.match_failure(near)
    assert hit is not None
    assert abs(hit.confidence - 0.8) < 1e-6


def test_fuzzy_requires_same_error_type_and_action(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    base = FailureSignature("verify_failed", "ui.click", "btn.start", "main")
    kb.confirm(kb.save_failure(_fk(base)), "active")

    other_type = FailureSignature("locate_timeout", "ui.click", "btn.start2", "main")
    other_action = FailureSignature("verify_failed", "ui.input", "btn.start2", "main")
    assert kb.match_failure(other_type) is None
    assert kb.match_failure(other_action) is None


# ---------- 语义召回钩子（设计 9.2） ----------

class _Emb:
    """假 embedding provider：返回预置候选并记录调用参数。"""

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def similar(self, sig, limit):
        self.calls.append((sig.digest(), limit))
        return self.hits


def test_embedding_hook_recalls_when_rules_miss(mem_conn):
    from testpilot.knowledge.service import EMBED_DECAY

    base = FailureSignature("locate_timeout", "ui.select", "tbl.result", "main")
    svc = KnowledgeService(mem_conn, {})
    entry_id = svc.save_failure(_fk(base, disposition="none", confidence=1.0))
    svc.confirm(entry_id, "active")

    # 目标签名与库中条目不同 error_type/action/target -> 规则必然未命中，只能靠语义召回
    target = FailureSignature("verify_failed", "ui.click", "btn.other", "main")
    emb = _Emb([(base.digest(), 0.9)])
    hit = KnowledgeService(mem_conn, {}, embedding=emb).match_failure(target)

    assert hit is not None and hit.entry_id == entry_id
    assert abs(hit.confidence - 0.9 * EMBED_DECAY) < 1e-6
    assert emb.calls == [(target.digest(), 3)]
    hits = mem_conn.execute("SELECT hits FROM kb_entry WHERE id=?",
                            (entry_id,)).fetchone()["hits"]
    assert hits == 1


def test_embedding_hook_degrades_on_provider_error(mem_conn):
    class Boom:
        def similar(self, sig, limit):
            raise RuntimeError("模型不可用")

    sig = FailureSignature("verify_failed", "ui.click", "btn.x", "main")
    assert KnowledgeService(mem_conn, {}, embedding=Boom()).match_failure(sig) is None


def test_embedding_hook_ignores_unknown_candidates(mem_conn):
    class Unknown:
        def similar(self, sig, limit):
            return [("not-in-kb", 0.99)]

    sig = FailureSignature("verify_failed", "ui.click", "btn.x", "main")
    assert KnowledgeService(mem_conn, {}, embedding=Unknown()).match_failure(sig) is None


def test_exact_match_takes_priority_over_fuzzy(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    fuzzy = FailureSignature("verify_failed", "ui.click", "btn.start", "main")
    exact = FailureSignature("verify_failed", "ui.click", "btn.start2", "main")
    kb.confirm(kb.save_failure(_fk(fuzzy, disposition="none", confidence=0.5)),
               "active")
    exact_id = kb.save_failure(_fk(exact, disposition="block_known", confidence=1.0))
    kb.confirm(exact_id, "active")

    hit = kb.match_failure(exact)
    assert hit.entry_id == exact_id
    assert hit.disposition == "block_known"


def test_element_learn_and_stale_marks_prefer_vision(mem_conn):
    kb = KnowledgeService(mem_conn, {"element_stale_days": 30})
    kb.learn_element("btn.start", {"driver": "uia", "locate_ms": 12})
    ek = kb.query_element("btn.start")
    assert ek is not None and ek.prefer_vision is False
    assert ek.payload["driver"] == "uia"

    # 人为把 last_success_at 推到 40 天前 -> 视为 stale
    old = time.strftime("%Y-%m-%dT%H:%M:%S",
                        time.localtime(time.time() - 40 * 86400))
    mem_conn.execute("UPDATE kb_entry SET last_success_at=? WHERE signature=?",
                     (old, "btn.start"))
    mem_conn.commit()
    assert kb.query_element("btn.start").prefer_vision is True

    # 再次学习 -> 刷新时间，恢复正常
    kb.learn_element("btn.start", {"driver": "uia"})
    assert kb.query_element("btn.start").prefer_vision is False


def test_element_upsert_keeps_single_row(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    kb.learn_element("k", {"v": 1})
    kb.learn_element("k", {"v": 2})
    rows = mem_conn.execute("SELECT payload_json FROM kb_entry WHERE type='element'"
                            " AND signature='k'").fetchall()
    assert len(rows) == 1
    assert '"v": 2' in rows[0]["payload_json"]


def test_query_element_missing_returns_none(mem_conn):
    assert KnowledgeService(mem_conn, {}).query_element("nope") is None


def test_prefetch_collects_elements_failures_and_mode(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    kb.learn_element("btn.a", {"driver": "uia"})
    kb.confirm(kb.save_failure(_fk(FailureSignature("env", "device.call", "d", "main"))),
               "active")
    mem_conn.execute(
        "INSERT INTO kb_entry(type, signature, payload_json, status, updated_at)"
        " VALUES ('case_ctx','TC-1','{\"note\":\"注意\"}','active','2026-01-01T00:00:00')")
    mem_conn.commit()

    hints = kb.prefetch(["TC-1"], driver_mode="vision_first")
    assert isinstance(hints, CaseHints)
    assert [e.key for e in hints.elements] == ["btn.a"]
    assert len(hints.failures) == 1
    assert hints.case_ctx[0]["note"] == "注意"
    assert hints.driver_mode == "vision_first"


def test_prefetch_empty_case_ids_ok(mem_conn):
    hints = KnowledgeService(mem_conn, {}).prefetch([])
    assert hints.elements == [] and hints.case_ctx == []


def test_router_stats_upsert_streak_and_reset(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    kb.feedback_router("btn.start", "uia", ok=False, ms=100)
    kb.feedback_router("btn.start", "uia", ok=False, ms=120)
    stats = kb.load_router_stats("btn.start")
    assert stats["uia"].fail_streak == 2
    assert stats["uia"].fail_count == 2
    assert stats["uia"].success_count == 0

    kb.feedback_router("btn.start", "uia", ok=True, ms=50)
    stats = kb.load_router_stats("btn.start")
    assert stats["uia"].fail_streak == 0
    assert stats["uia"].success_count == 1
    # 增量均值：ms=100 -> 110（两次）-> 90（三次）
    assert stats["uia"].avg_locate_ms == 90


def test_router_stats_ignores_blank_key_or_driver(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    kb.feedback_router("", "uia", ok=True, ms=1)
    kb.feedback_router("k", "", ok=True, ms=1)
    assert mem_conn.execute("SELECT COUNT(*) AS n FROM router_stats").fetchone()["n"] == 0


def test_similar_failures_requires_same_action_and_type_or_target(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    base = FailureSignature("locate_timeout", "ui.click", "btn.start", "main")
    kb.confirm(kb.save_failure(_fk(base)), "active")

    similar = FailureSignature("locate_timeout", "ui.click", "btn.stop", "main")
    other_action = FailureSignature("locate_timeout", "ui.input", "btn.start", "main")
    other_type = FailureSignature("env", "ui.click", "edit.pressure", "main")

    assert [f.signature for f in kb.similar_failures(similar)] == [base.digest()]
    assert kb.similar_failures(other_action) == []      # 动作不同
    assert kb.similar_failures(other_type) == []        # 类型与 target 前缀都不同


def test_similar_failures_excludes_self_and_respects_limit(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    sigs = [FailureSignature("locate_timeout", "ui.click", f"btn.a{i}", "main")
            for i in range(4)]
    for s in sigs:
        kb.confirm(kb.save_failure(_fk(s)), "active")

    out = kb.similar_failures(sigs[0], limit=2)
    assert len(out) == 2
    assert sigs[0].digest() not in {f.signature for f in out}


def test_similar_failures_sorted_by_confidence_then_id(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    low = FailureSignature("locate_timeout", "ui.click", "btn.low", "main")
    high = FailureSignature("locate_timeout", "ui.click", "btn.high", "main")
    kb.confirm(kb.save_failure(_fk(low, confidence=0.4)), "active")
    kb.confirm(kb.save_failure(_fk(high, confidence=0.95)), "active")

    out = kb.similar_failures(FailureSignature("locate_timeout", "ui.click",
                                              "btn.x", "main"))
    assert [f.signature for f in out] == [high.digest(), low.digest()]


def test_similar_failures_includes_rejected_history(mem_conn):
    """被拒条目也是"历史"，供人审时参考（不参与自动处置）。"""
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("locate_timeout", "ui.click", "btn.a", "main")
    kb.confirm(kb.save_failure(_fk(sig)), "rejected")
    out = kb.similar_failures(FailureSignature("locate_timeout", "ui.click",
                                              "btn.b", "main"))
    assert len(out) == 1


def test_similar_failures_reports_hits(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("locate_timeout", "ui.click", "btn.a", "main")
    entry_id = kb.save_failure(_fk(sig))
    kb.confirm(entry_id, "active")
    kb.match_failure(sig)                       # 命中一次 -> hits=1
    kb.match_failure(sig)

    out = kb.similar_failures(FailureSignature("locate_timeout", "ui.click",
                                              "btn.b", "main"),
                              limit=1)
    assert out[0].entry_id == entry_id
    assert out[0].hits == 2
    assert out[0].as_ref()["hits"] == 2


def test_confirm_rejects_illegal_verdict(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    try:
        kb.confirm(1, "whatever")
    except ValueError:
        return
    raise AssertionError("非法 verdict 应当报错")


def test_save_failure_returns_stable_entry_id_on_conflict(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("env", "device.call", "d", "main")
    first = kb.save_failure(_fk(sig))
    again = kb.save_failure(_fk(sig, suggestion="新建议"))
    row = mem_conn.execute("SELECT id, payload_json FROM kb_entry").fetchone()
    assert row["id"] == first
    assert len(mem_conn.execute("SELECT 1 FROM kb_entry").fetchall()) == 1
    assert "新建议" in row["payload_json"]
    assert again == first          # ON CONFLICT 不新插入，返回原 entry


# ---------------------------------------------------------------- 处置策略人审

def test_set_disposition_updates_payload(mem_conn):
    """处置策略决定命中后的引擎行为，人审必须能改（否则人审只能全盘接受 LLM 结论）。"""
    kb = KnowledgeService(mem_conn, {})
    sig = FailureSignature("locate_timeout", "ui.click", "btn.a", "main")
    entry_id = kb.save_failure(_fk(sig))

    assert kb.set_disposition(entry_id, "retry_ready") == {
        "entry_id": entry_id, "disposition": "retry_ready"}
    row = mem_conn.execute("SELECT payload_json FROM kb_entry WHERE id=?",
                           (entry_id,)).fetchone()
    import json as _json
    assert _json.loads(row["payload_json"])["disposition"] == "retry_ready"

    # 改完生效：命中后按新处置走（block_known -> 直接判阻塞）
    kb.set_disposition(entry_id, "block_known")
    kb.confirm(entry_id, "active")
    hit = kb.match_failure(sig)
    assert hit is not None and hit.disposition == "block_known"


def test_set_disposition_rejects_illegal_value(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    entry_id = kb.save_failure(_fk(FailureSignature("env", "device.call", "d", "main")))
    try:
        kb.set_disposition(entry_id, "whatever")
    except ValueError as e:
        assert "非法处置策略" in str(e)
        return
    raise AssertionError("非法处置策略应当报错")


def test_set_disposition_unknown_entry_raises(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    try:
        kb.set_disposition(999, "none")
    except ValueError as e:
        assert "不存在" in str(e)
        return
    raise AssertionError("不存在的条目应当报错")


# ---------------------------------------------------------------- case_ctx 人审维护

def _seed_case(conn, case_id="TC-1024", zid=1024, title="样本检测"):
    from testpilot.storage import repos
    repos.create_case(conn, case_id=case_id, zentao_case_id=zid,
                      title=title, raw=None, zentao_updated_at="t")
    return case_id


def test_case_ctx_save_by_local_and_zentao_id(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    _seed_case(mem_conn)
    out = kb.save_case_ctx("TC-1024", {"scene": "normal_sample_a",
                                       "pitfalls": "坑1;坑2",
                                       "notes": "先预热"})
    assert out["case_id"] == "TC-1024"
    assert out["payload"]["pitfalls"] == ["坑1", "坑2"]
    assert out["payload"]["zentao_case_id"] == 1024

    # 禅道数字 ID 同样可寻址（人审界面既能从本地 ID 也能从禅道 ID 进入）
    assert kb.get_case_ctx("1024")["payload"]["scene"] == "normal_sample_a"
    rows = mem_conn.execute(
        "SELECT status FROM kb_entry WHERE type='case_ctx'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "active"


def test_case_ctx_merge_and_clear_fields(mem_conn):
    """按字段增量合并：缺席字段不改，显式空值清除（避免"编辑器覆盖丢内容"）。"""
    kb = KnowledgeService(mem_conn, {})
    _seed_case(mem_conn)
    kb.save_case_ctx("TC-1024", {"scene": "s1", "notes": "n1"})
    kb.save_case_ctx("TC-1024", {"pitfalls": ["p1"]})
    payload = kb.get_case_ctx("TC-1024")["payload"]
    assert payload["scene"] == "s1" and payload["notes"] == "n1"
    assert payload["pitfalls"] == ["p1"]

    kb.save_case_ctx("TC-1024", {"notes": ""})
    assert "notes" not in kb.get_case_ctx("TC-1024")["payload"]


def test_case_ctx_rejects_unknown_field_and_empty_payload(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    _seed_case(mem_conn)
    try:
        kb.save_case_ctx("TC-1024", {"whatever": 1})
    except ValueError as e:
        assert "未知字段" in str(e)
    else:
        raise AssertionError("未知字段应当报错")

    try:
        kb.save_case_ctx("TC-1024", {"scene": ""})
    except ValueError as e:
        assert "为空" in str(e)
    else:
        raise AssertionError("全空上下文不应写入空条目")


def test_case_ctx_requires_existing_case(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    try:
        kb.save_case_ctx("TC-9999", {"notes": "x"})
    except ValueError as e:
        assert "不存在" in str(e)
    else:
        raise AssertionError("未入库用例不应能写上下文")


def test_case_ctx_list_and_delete(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    _seed_case(mem_conn, "TC-1", 1, "用例一")
    _seed_case(mem_conn, "TC-2", 2, "用例二")
    kb.save_case_ctx("TC-1", {"notes": "a"})
    kb.save_case_ctx("TC-2", {"scene": "s"})

    all_rows = kb.list_case_ctx()
    assert [r["case_id"] for r in all_rows] == ["TC-1", "TC-2"]
    assert all_rows[0]["case_title"] == "用例一"
    assert [r["case_id"] for r in kb.list_case_ctx("2")] == ["TC-2"]

    assert kb.delete_case_ctx("TC-1")["deleted"] == 1
    assert [r["case_id"] for r in kb.list_case_ctx()] == ["TC-2"]


def test_prefetch_injects_case_ctx_with_case_id(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    _seed_case(mem_conn)
    kb.save_case_ctx("TC-1024", {"pitfalls": ["坑"]})
    hints = kb.prefetch(["TC-1024"])
    assert hints.case_ctx[0]["case_id"] == "TC-1024"
    assert hints.case_ctx[0]["pitfalls"] == ["坑"]


# ---------------------------------------------------------------- env_compat 矩阵

def test_record_env_compat_accumulates_matrix(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    out = kb.record_env_compat(
        run_id=1, test_app_version="1.2.0", suite_id=3,
        case_ids=["TC-1", "TC-2"],
        statuses={"TC-1": "pass", "TC-2": "fail"},
        driver_mode="auto", emulator_version="2.0")
    assert out["signature"] == "1.2.0|suite:3"

    kb.record_env_compat(
        run_id=2, test_app_version="1.2.0", suite_id=3,
        case_ids=["TC-1", "TC-2"],
        statuses={"TC-1": "pass", "TC-2": "blocked"})
    entries = kb.env_compat_matrix(suite_id=3)
    assert len(entries) == 1
    payload = entries[0]["payload"]
    assert payload["test_app_version"] == "1.2.0"
    assert payload["driver_mode"] == "auto"
    assert payload["matrix"]["TC-1"] == {
        "pass": 2, "fail": 0, "other": 0, "total": 2,
        "last_status": "pass", "last_run_id": 2}
    assert payload["matrix"]["TC-2"]["fail"] == 1
    assert payload["matrix"]["TC-2"]["other"] == 1     # blocked 计入 other
    assert payload["runs"] == [1, 2]


def test_env_compat_matrix_versions_and_version_filter(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    for version, run_id in (("1.0.0", 1), ("1.1.0", 2)):
        kb.record_env_compat(run_id=run_id, test_app_version=version,
                             suite_id=7, case_ids=["TC-1"],
                             statuses={"TC-1": "pass"})
    entries = kb.env_compat_matrix(suite_id=7)
    assert [e["payload"]["test_app_version"] for e in entries] == ["1.1.0", "1.0.0"]
    assert len(kb.env_compat_matrix(test_app_version="1.0.0", suite_id=7)) == 1
    assert kb.env_compat_matrix(suite_id=8) == []


def test_env_compat_without_suite_uses_case_set(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    kb.record_env_compat(run_id=1, test_app_version="1.0.0", suite_id=None,
                         case_ids=["TC-1", "TC-2"], statuses={"TC-1": "pass"})
    assert len(kb.env_compat_matrix(case_ids=["TC-2", "TC-1"])) == 1
    assert kb.env_compat_matrix(case_ids=["TC-1"]) == []


def test_env_compat_runs_history_is_capped(mem_conn):
    kb = KnowledgeService(mem_conn, {})
    for run_id in range(1, 26):
        kb.record_env_compat(run_id=run_id, test_app_version="1.0.0",
                             suite_id=1, case_ids=["TC-1"],
                             statuses={"TC-1": "pass"})
    payload = kb.env_compat_matrix(suite_id=1)[0]["payload"]
    assert len(payload["runs"]) == 20
    assert payload["runs"][-1] == 25
    assert payload["matrix"]["TC-1"]["total"] == 25

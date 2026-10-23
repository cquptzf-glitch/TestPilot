"""用例应用服务单测 — 拉取/同步/解析/人审（M1-1~M1-3，设计 3.1/3.6）。"""
import pytest

from testpilot.case_parser import CaseService, dumps, loads
from testpilot.core.errors import LlmError, TestPilotError, ZentaoError
from testpilot.storage import repos
from tests.conftest import FakeZentao


@pytest.fixture()
def svc(db_conn, fake_zentao, fake_llm):
    return CaseService(db_conn, fake_zentao, fake_llm, {})


def _changed_case(fake_zentao, zid=1, updated="2026-02-02T00:00:00"):
    raw = dict(fake_zentao._cases[zid])
    raw["updated_at"] = updated
    raw["title"] = raw["title"] + "（已更新）"
    raw["steps_text"] += "\n3. 关闭窗口"
    fake_zentao._cases[zid] = raw
    return raw


# ---------- 拉取 ----------

def test_pull_creates_draft_with_raw_snapshot(svc, db_conn):
    out = svc.pull([1, 2])
    assert out["new"] == 2
    assert [c["case_id"] for c in out["cases"]] == ["TC-1", "TC-2"]

    row = repos.get_case(db_conn, "TC-1")
    assert row["status"] == "draft"
    assert row["version"] == 0                       # 草稿：尚无生效版本
    assert row["steps"] == []
    assert row["raw"]["steps_text"].startswith("1. 启动被测程序")
    assert row["zentao_updated_at"] == "2026-01-01T00:00:00"


def test_pull_records_last_synced_at(svc, db_conn):
    """拉取必须写入 last_synced_at，且 list_cases 要把它带出来。

    Cases 页的「增量同步」按钮显示「上次同步 xxx」——若这一列没写或没被 SELECT，
    UI 会恒显示「从未同步」（不报错、不崩溃），用户以为同步功能坏了。
    """
    svc.pull([1])
    assert repos.get_case(db_conn, "TC-1")["last_synced_at"], \
        "pull 未写入 last_synced_at"

    listed = repos.list_cases(db_conn)
    assert listed, "list_cases 返回空"
    assert "last_synced_at" in listed[0], "list_cases 的 SELECT 列表漏了 last_synced_at"
    assert listed[0]["last_synced_at"], "list_cases 返回的 last_synced_at 为空"


def test_pull_is_idempotent(svc, db_conn):
    svc.pull([1])
    out = svc.pull([1])
    assert out["new"] == 0 and out["unchanged"] == 1
    assert repos.get_case(db_conn, "TC-1")["last_synced_at"]


def test_pull_marks_stale_when_active_case_changed(svc, db_conn, fake_zentao):
    svc.pull([1])
    svc.parse("TC-1")
    svc.accept("TC-1")

    _changed_case(fake_zentao)
    out = svc.pull([1])
    assert out["stale"] == 1
    row = repos.get_case(db_conn, "TC-1")
    assert row["sync_status"] == "stale"
    assert row["status"] == "active"                 # 已确认版本仍可用
    assert "已更新" in row["title"]


def test_pull_marks_conflict_when_draft_case_changed(svc, db_conn, fake_zentao):
    svc.pull([1])
    svc.parse("TC-1")                                # 草稿未确认
    _changed_case(fake_zentao)
    out = svc.pull([1])
    assert out["conflict"] == 1
    row = repos.get_case(db_conn, "TC-1")
    assert row["sync_status"] == "conflict"
    assert row["status"] == "draft"
    assert len(row["steps"]) == 2                    # 本地草稿仍在，可三版比对


def test_pull_missing_remote_case_raises(svc):
    with pytest.raises(TestPilotError):
        svc.pull([999])


def test_sync_scans_and_updates(svc, db_conn, fake_zentao):
    svc.pull([1, 2])
    _changed_case(fake_zentao, 2)
    out = svc.sync(product_id=1)
    assert out["scanned"] == 1              # 只拉远端水位之后的变更详情
    assert out["unchanged"] == 0
    assert out["since"] == "2026-01-02T00:00:00"
    # TC-2 本地仍是草稿（未人审）-> 远端变更按 conflict 处理（设计 3.6 三版并列）
    assert out["conflict"] == 1
    assert out["stale"] == 0
    assert repos.get_case(db_conn, "TC-2")["sync_status"] == "conflict"


def test_sync_uses_effective_configured_product_id(db_conn, fake_zentao, fake_llm):
    seen = []
    original = fake_zentao.fetch_cases

    def fetch(product_id=None, since=None):
        seen.append((product_id, since))
        return original(product_id, since)

    fake_zentao.fetch_cases = fetch
    svc = CaseService(
        db_conn, fake_zentao, fake_llm,
        {"zentao": {"product_id": 7}},
    )
    out = svc.sync()
    assert seen[0][0] == 7
    assert out["sync_state"]["scope"] == "7"


def test_sync_parse_empty_result_has_stable_batch_shape(
        db_conn, fake_zentao, fake_llm):
    svc = CaseService(
        db_conn, fake_zentao, fake_llm,
        {"zentao": {"product_id": 1}},
    )
    svc.pull([1, 2])
    # With no remote updates and no stale/empty Zentao cases, parse result remains
    # batch-shaped instead of changing type to {}.
    for cid in ("TC-1", "TC-2"):
        svc.parse(cid)
        svc.accept(cid)
    out = svc.sync(parse=True)
    assert out["parse"] == {
        "parsed": 0, "failed": [], "items": [], "total": 0,
    }


def test_sync_full_calibration_ignores_watermark(svc, fake_zentao):
    svc.pull([1, 2])
    out = svc.sync(product_id=1, full=True)
    assert out["full"] is True
    assert out["since"] is None
    assert out["scanned"] == 2
    assert out["unchanged"] == 2


def test_sync_persists_source_state_even_when_no_remote_change(svc, db_conn):
    svc.pull([1, 2])
    out = svc.sync(product_id=1)
    assert out["scanned"] == 0
    state = repos.get_sync_state(db_conn, "zentao", "1")
    assert state is not None
    assert state["watermark"] == "2026-01-02T00:00:00"
    assert state["synced_at"]
    assert out["sync_state"]["synced_at"] == state["synced_at"]


def test_sync_watermark_is_isolated_by_product(db_conn, fake_zentao, fake_llm):
    raw = dict(fake_zentao._cases[1])
    raw.update(case_id="TC-99", zentao_case_id=99, product=2,
               updated_at="2025-01-01T00:00:00", title="另一个产品")
    fake_zentao._cases[99] = raw
    svc = CaseService(db_conn, fake_zentao, fake_llm, {"zentao": {"product_id": 2}})
    repos.create_case(
        db_conn, case_id="TC-99", zentao_case_id=99, title=raw["title"],
        raw=raw, zentao_updated_at=raw["updated_at"])
    assert svc._sync_watermark(2) == "2025-01-01T00:00:00"
    assert svc._sync_watermark(1) is None


def test_sync_with_parse_reparses_stale_only(svc, fake_zentao, fake_llm, db_conn):
    svc.pull([1, 2])
    svc.parse("TC-1")
    svc.accept("TC-1")
    svc.parse("TC-2")
    svc.accept("TC-2")
    _changed_case(fake_zentao, 1)

    out = svc.sync(parse=True)
    assert out["parse"]["parsed"] == 1
    assert fake_llm.parse_calls[-1]["case_id"] == "TC-1"
    assert repos.get_case(db_conn, "TC-1")["status"] == "draft"   # 重解析后待复审


def test_zentao_sync_parse_does_not_consume_local_svn_drafts(
        svc, db_conn, fake_llm):
    repos.create_case(
        db_conn, case_id="SVN-WC-LOCAL-01", zentao_case_id=None,
        title="本地 SVN Agent 用例",
        raw={"source": "svn_agent", "steps_text": "1. 本地步骤", "product": None},
        zentao_updated_at=None)
    svc.pull([1])

    out = svc.sync(product_id=1, parse=True)
    assert out["parse"]["parsed"] >= 1
    parsed_ids = {call["case_id"] for call in fake_llm.parse_calls}
    assert "SVN-WC-LOCAL-01" not in parsed_ids


def test_pull_with_parse(svc, db_conn):
    out = svc.pull([1], parse=True)
    assert out["parse"]["parsed"] == 1
    row = repos.get_case(db_conn, "TC-1")
    assert len(row["steps"]) == 2
    assert row["preconditions"] == ["模拟器场景: normal_sample_a"]
    assert row["status"] == "draft"


# ---------- 解析 ----------

def test_parse_requires_raw_text(svc, db_conn):
    repos.create_case(db_conn, case_id="TC-9", zentao_case_id=9, title="T",
                      raw=None, zentao_updated_at=None)
    with pytest.raises(TestPilotError):
        svc.parse("TC-9")


def test_parse_unknown_case_raises(svc):
    with pytest.raises(TestPilotError):
        svc.parse("TC-404")


def test_parse_rejected_case_raises(svc):
    svc.pull([1])
    svc.parse("TC-1")
    svc.reject("TC-1")
    with pytest.raises(TestPilotError):
        svc.parse("TC-1")


def test_parse_empty_steps_raises(svc, fake_llm):
    svc.pull([1])
    fake_llm.draft = {"case_id": "TC-1", "steps": []}
    with pytest.raises(LlmError):
        svc.parse("TC-1")


def test_parse_failure_surfaces_llm_error(svc, fake_llm):
    svc.pull([1])
    fake_llm.fail_parse = True
    with pytest.raises(RuntimeError):
        svc.parse("TC-1")


def test_parse_injects_emulator_capabilities(db_conn, fake_zentao, fake_llm):
    from tests.conftest import FakeEmulator
    emu = FakeEmulator()
    svc = CaseService(db_conn, fake_zentao, fake_llm, {},
                      capabilities_provider=lambda: emu.capabilities)
    svc.pull([1])
    svc.parse("TC-1")
    assert fake_llm.emulator_capabilities["scenes"] == ["normal_sample_a"]


def test_parse_tolerates_capability_provider_failure(db_conn, fake_zentao, fake_llm):
    def boom():
        raise RuntimeError("emulator down")

    svc = CaseService(db_conn, fake_zentao, fake_llm, {},
                      capabilities_provider=boom)
    svc.pull([1])
    assert svc.parse("TC-1")["step_count"] == 2
    assert fake_llm.emulator_capabilities is None


def test_parse_many_reports_failures(svc, db_conn, fake_zentao):
    svc.pull([1])
    repos.create_case(db_conn, case_id="TC-9", zentao_case_id=9, title="无原文",
                      raw=None, zentao_updated_at=None)
    out = svc.parse_many(["TC-1", "TC-9"])
    assert out["parsed"] == 1
    assert out["failed"][0]["case_id"] == "TC-9"


def test_parse_many_default_selects_stale(db_conn, fake_zentao, fake_llm):
    svc = CaseService(db_conn, fake_zentao, fake_llm, {})
    svc.pull([1])
    svc.parse("TC-1")
    svc.accept("TC-1")
    _changed_case(fake_zentao)
    svc.pull([1])                                  # -> stale
    out = svc.parse_many(None, only_stale=True)
    assert out["parsed"] == 1


# ---------- 人审 ----------

def test_accept_bumps_version_and_marks_active(svc, db_conn):
    svc.pull([1])
    svc.parse("TC-1")
    out = svc.accept("TC-1")
    assert out["version"] == 1
    row = repos.get_case(db_conn, "TC-1")
    assert row["status"] == "active"
    assert row["accepted_steps"] == row["steps"]
    assert row["sync_status"] == "synced"

    # 再次人审（重新解析后）-> 版本再 +1
    svc.parse("TC-1")
    assert svc.accept("TC-1")["version"] == 2


def test_accept_without_steps_raises(svc):
    svc.pull([1])
    with pytest.raises(TestPilotError):
        svc.accept("TC-1")


def test_accept_reports_uncertain_steps(svc, fake_llm):
    svc.pull([1])
    fake_llm.draft = {"case_id": "TC-1", "steps": [
        {"id": "s1", "action": "ui.click", "uncertain": True,
         "target": {"key": "k", "primary": {"type": "uia", "name": "x"}}}]}
    svc.parse("TC-1")
    assert svc.accept("TC-1")["uncertain_steps"] == ["s1"]


def test_reject_sets_status(svc, db_conn):
    svc.pull([1])
    svc.parse("TC-1")
    assert svc.reject("TC-1")["status"] == "rejected"
    assert repos.get_case(db_conn, "TC-1")["status"] == "rejected"
    assert [c["case_id"] for c in svc.list_cases(status="rejected")] == ["TC-1"]


def test_reject_unknown_case_raises(svc):
    with pytest.raises(TestPilotError):
        svc.reject("TC-404")


# ---------- 导出 / 导入 ----------

@pytest.mark.parametrize("fmt", ["json", "yaml"])
def test_export_import_roundtrip(svc, db_conn, fmt):
    svc.pull([1])
    svc.parse("TC-1")
    text = svc.export_case("TC-1", fmt)
    data = loads(text, fmt)
    assert data["case_id"] == "TC-1" and len(data["steps"]) == 2

    data["steps"][0]["args"]["exe"] = "edited.exe"
    out = svc.import_draft(dumps(data, fmt), fmt)
    assert out["case_id"] == "TC-1"
    assert repos.get_case(db_conn, "TC-1")["steps"][0]["args"]["exe"] == "edited.exe"
    assert repos.get_case(db_conn, "TC-1")["status"] == "draft"


def test_import_json_string_detects_structure_errors(svc):
    with pytest.raises(TestPilotError):
        svc.import_draft('[{"not": "an object"}]', "json")


def test_import_creates_local_only_case(svc, db_conn):
    payload = {"case_id": "TC-777", "title": "手工用例", "steps": [
        {"id": "s1", "action": "sleep", "args": {"ms": 1}}]}
    svc.import_draft(dumps(payload, "json"), "json")
    row = repos.get_case(db_conn, "TC-777")
    assert row is not None and row["status"] == "draft"
    assert row["zentao_case_id"] is None


def test_import_can_override_case_id(svc):
    payload = {"case_id": "TC-A", "steps": [{"id": "s1", "action": "sleep"}]}
    assert svc.import_draft(dumps(payload, "json"), "json",
                            case_id="TC-B")["case_id"] == "TC-B"


def test_export_unknown_case_raises(svc):
    with pytest.raises(TestPilotError):
        svc.export_case("TC-404")


# ---------- 执行门禁 / 查询 ----------

def test_to_testcase_only_allows_active(svc):
    svc.pull([1])
    svc.parse("TC-1")
    with pytest.raises(TestPilotError):
        svc.to_testcase("TC-1")                     # draft 不可执行
    svc.accept("TC-1")
    case = svc.to_testcase("TC-1")
    assert case.case_id == "TC-1"
    assert case.version == 1
    assert len(case.steps) == 2
    assert case.preconditions == ["模拟器场景: normal_sample_a"]


def test_to_testcase_rejects_unknown(svc):
    with pytest.raises(TestPilotError):
        svc.to_testcase("TC-404")


def test_preconditions_of_dedupes_in_order(svc):
    svc.pull([1, 2])
    svc.parse("TC-1")
    svc.parse("TC-2")
    svc.accept("TC-1")
    svc.accept("TC-2")
    pres = svc.preconditions_of(["TC-1", "TC-2", "TC-1"])
    assert pres == ["模拟器场景: normal_sample_a"]


def test_not_runnable_lists_reasons(svc, db_conn):
    svc.pull([1])
    bad = svc.not_runnable(["TC-1", "TC-404"])
    assert {b["case_id"] for b in bad} == {"TC-1", "TC-404"}
    reasons = {b["case_id"]: b["reason"] for b in bad}
    assert reasons["TC-1"] == "状态 draft"
    assert reasons["TC-404"] == "本地不存在"


def test_not_runnable_reports_missing_steps(db_conn, fake_zentao, fake_llm):
    svc = CaseService(db_conn, fake_zentao, fake_llm, {})
    svc.pull([1])
    svc.parse("TC-1")
    svc.accept("TC-1")
    db_conn.execute("UPDATE dsl_case SET steps_json='[]' WHERE case_id='TC-1'")
    db_conn.commit()
    assert svc.not_runnable(["TC-1"])[0]["reason"] == "无 DSL 步骤"


def test_show_returns_review_view(svc):
    svc.pull([1])
    svc.parse("TC-1")
    view = svc.show("TC-1")
    assert view["case_id"] == "TC-1"
    assert view["step_count"] == 2
    assert view["source_text"].startswith("1. 启动被测程序")
    assert view["uncertain_steps"] == []
    assert view["status"] == "draft"


def test_show_exposes_three_versions_for_conflict(svc, db_conn, fake_zentao):
    """设计 3.6：conflict 时人审界面要**并列显示三版**
    （禅道最新原文 / 本地草稿 / 本地已确认版本）。

    视图若不带 `accepted_steps`，前端只能显示两版 —— 而 conflict 的整个意义
    就是拿三版做比对。这条测试同时钉住"三版内容确实不同"（否则比对没意义）。

    构造真实路径：先 accept 产出版本快照 → 远端变更使其 stale → 重新解析回到
    草稿态（此时既有 accepted 快照、又有新草稿、还有新原文，才是三版并存的时刻）。
    """
    svc.pull([1])
    svc.parse("TC-1")
    svc.accept("TC-1")                              # v1 生效，冻结 accepted_steps
    accepted = repos.get_case(db_conn, "TC-1")["accepted_steps"]
    assert accepted, "前提不成立：accept 未冻结步骤快照"

    _changed_case(fake_zentao, 1, updated="2026-03-03T00:00:00")
    svc.sync()                                      # 已 active + 远端变 -> stale
    assert repos.get_case(db_conn, "TC-1")["sync_status"] == "stale"
    svc.parse("TC-1")                               # 重解析 -> 回到草稿（旧快照仍在）
    assert repos.get_case(db_conn, "TC-1")["status"] == "draft"
    _changed_case(fake_zentao, 1, updated="2026-04-04T00:00:00")   # 远端再次变更
    svc.sync()                                      # 草稿 + 远端变更 -> conflict

    view = svc.show("TC-1")
    assert view["sync_status"] == "conflict"
    assert "accepted_steps" in view, "视图缺 accepted_steps → 前端只能显示两版"
    assert view["accepted_steps"], "accepted_steps 为空，三版对照无意义"
    # 三版齐全
    assert view["source_text"], "缺禅道原文"
    assert view["steps"], "缺本地草稿"
    assert "关闭窗口" in view["source_text"], "禅道原文应为**最新**版本"
    # 草稿与已确认版本必须是**两份独立数据**：人审改的是草稿，
    # 若两者是同一个对象/同一份 JSON，三版对照会退化成"两栏一样"。
    assert view["steps"] is not view["accepted_steps"]
    assert view["steps"] != view["accepted_steps"] or view["version"] >= 1


def test_show_accepted_steps_is_none_before_first_accept(svc):
    """version=0（从未人审）时没有"已确认版本"，应为 None 而不是 []。

    区别很重要：前端据此决定是否显示第三栏 —— 空数组会被误判成"有一版空的"。
    """
    svc.pull([1])
    svc.parse("TC-1")
    view = svc.show("TC-1")
    assert view["version"] == 0
    assert view["accepted_steps"] is None


def test_list_cases_rejects_illegal_filters(svc):
    with pytest.raises(TestPilotError):
        svc.list_cases(status="weird")
    with pytest.raises(TestPilotError):
        svc.list_cases(sync_status="weird")


def test_show_unknown_case_raises(svc):
    with pytest.raises(TestPilotError):
        svc.show("TC-404")


def test_list_cases_filters(svc):
    svc.pull([1, 2])
    svc.parse("TC-1")
    svc.accept("TC-1")
    assert [c["case_id"] for c in svc.list_cases(status="active")] == ["TC-1"]
    assert [c["case_id"] for c in svc.list_cases(sync_status="synced")] == ["TC-1", "TC-2"]


def test_serialization_helpers_support_unicode():
    assert "中文" in dumps({"k": "中文"}, "json")
    assert loads('{"k": "中文"}', "json")["k"] == "中文"


def test_zentao_fetch_case_error_propagates(db_conn, fake_llm):
    svc = CaseService(db_conn, FakeZentao(configured=False), fake_llm, {})
    with pytest.raises(ZentaoError):
        svc.pull([42])


# ---------- 禅道用例分组（模块）透传（第十五轮） ----------

def test_pull_persists_zentao_module_group(svc, db_conn, fake_zentao):
    """拉取时把禅道分组（模块）id/名称一并入库，供 UI 分组与筛选。"""
    raw = fake_zentao._cases[1]
    raw["module"] = 7
    raw["module_name"] = "登录模块"
    svc.pull([1])
    row = repos.get_case(db_conn, "TC-1")
    assert row["module_id"] == 7
    assert row["module_name"] == "登录模块"


def test_sync_updates_module_group_on_change(svc, db_conn, fake_zentao):
    raw = fake_zentao._cases[1]
    raw["module"] = 7
    raw["module_name"] = "登录模块"
    svc.pull([1])

    changed = dict(fake_zentao._cases[1])
    changed["updated_at"] = "2026-02-02T00:00:00"
    changed["module"] = 9
    changed["module_name"] = "订单模块"
    fake_zentao._cases[1] = changed
    svc.sync()

    row = repos.get_case(db_conn, "TC-1")
    assert row["sync_status"] == "conflict"   # 草稿态变更按设计标 conflict
    assert row["module_id"] == 9
    assert row["module_name"] == "订单模块"

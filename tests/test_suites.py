"""测试方案服务单测（M1-4，设计 3.6）。"""
import pytest

from testpilot.case_parser import CaseService
from testpilot.core.errors import TestPilotError
from testpilot.storage import repos
from testpilot.suites import SuiteService


@pytest.fixture()
def svc(db_conn, fake_zentao, fake_llm):
    cases = CaseService(db_conn, fake_zentao, fake_llm, {})
    cases.pull([1, 2])
    for cid in ("TC-1", "TC-2"):
        cases.parse(cid)
        cases.accept(cid)
    return SuiteService(db_conn, cases), cases


def test_create_derives_preconditions_from_cases(svc, db_conn):
    suites, _ = svc
    suite = suites.create("回归A", ["TC-1", "TC-2"], description="日常回归")
    assert suite["preconditions"] == ["模拟器场景: normal_sample_a"]
    assert suite["case_ids"] == ["TC-1", "TC-2"]
    assert suite["not_runnable"] == []
    assert repos.get_suite(db_conn, suite["id"])["description"] == "日常回归"


def test_create_rejects_empty_cases(svc):
    suites, _ = svc
    with pytest.raises(TestPilotError):
        suites.create("空方案", [])


def test_create_rejects_duplicate_name(svc):
    suites, _ = svc
    suites.create("重名", ["TC-1"])
    with pytest.raises(TestPilotError):
        suites.create("重名", ["TC-2"])


def test_create_rejects_invalid_driver_mode(svc):
    suites, _ = svc
    with pytest.raises(TestPilotError):
        suites.create("坏模式", ["TC-1"], driver_config={"mode": "turbo"})


def test_create_accepts_explicit_preconditions(svc):
    suites, _ = svc
    suite = suites.create("显式", ["TC-1"], preconditions=["模拟器场景: custom"])
    assert suite["preconditions"] == ["模拟器场景: custom"]


def test_create_surfaces_not_runnable_cases(svc):
    """未 active 的用例仍可入方案，但必须显式暴露不可执行原因。"""
    suites, cases = svc
    cases.reject("TC-2")
    suite = suites.create("含被拒用例", ["TC-1", "TC-2"])
    assert [b["case_id"] for b in suite["not_runnable"]] == ["TC-2"]


def test_update_recomputes_preconditions(svc):
    suites, _ = svc
    suite = suites.create("A", ["TC-1", "TC-2"])
    updated = suites.update(suite["id"], case_ids=["TC-2"])
    assert updated["case_ids"] == ["TC-2"]
    assert updated["preconditions"] == ["模拟器场景: normal_sample_a"]


def test_update_can_override_preconditions_and_driver_config(svc):
    suites, _ = svc
    suite = suites.create("A", ["TC-1"])
    updated = suites.update(suite["id"], preconditions=[], driver_config={"mode": "uia_only"},
                            name="A2", description="d2")
    assert updated["preconditions"] == []
    assert updated["driver_config"]["mode"] == "uia_only"
    assert updated["name"] == "A2" and updated["description"] == "d2"


def test_update_rejects_empty_case_list_and_bad_mode(svc):
    suites, _ = svc
    suite = suites.create("A", ["TC-1"])
    with pytest.raises(TestPilotError):
        suites.update(suite["id"], case_ids=[])
    with pytest.raises(TestPilotError):
        suites.update(suite["id"], driver_config={"mode": "nope"})


def test_update_unknown_suite_raises(svc):
    suites, _ = svc
    with pytest.raises(TestPilotError):
        suites.update(999, name="x")


def test_resolve_by_id_and_name(svc):
    suites, _ = svc
    suite = suites.create("可解析", ["TC-1"])
    assert suites.resolve(suite["id"])["id"] == suite["id"]
    assert suites.resolve(str(suite["id"]))["id"] == suite["id"]
    assert suites.resolve("可解析")["id"] == suite["id"]
    with pytest.raises(TestPilotError):
        suites.resolve("不存在")


def test_list_and_delete(svc):
    suites, _ = svc
    a = suites.create("A", ["TC-1"])
    suites.create("B", ["TC-2"])
    assert [s["name"] for s in suites.list()] == ["A", "B"]
    suites.delete(a["id"])
    assert [s["name"] for s in suites.list()] == ["B"]
    with pytest.raises(TestPilotError):
        suites.delete(a["id"])


def test_expand_returns_snapshot(svc):
    suites, _ = svc
    suite = suites.create("A", ["TC-1", "TC-2"],
                          driver_config={"mode": "vision_first"})
    snap = suites.expand(suite["id"])
    assert snap["case_ids"] == ["TC-1", "TC-2"]
    assert snap["preconditions"] == ["模拟器场景: normal_sample_a"]
    assert snap["driver_config"] == {"mode": "vision_first"}

    # 快照是副本：方案后续修改不影响已展开的结果
    suites.update(suite["id"], case_ids=["TC-1"])
    assert snap["case_ids"] == ["TC-1", "TC-2"]


def test_expand_unknown_suite_raises(svc):
    suites, _ = svc
    with pytest.raises(TestPilotError):
        suites.expand(999)

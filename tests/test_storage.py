"""存储层单测 — DDL/迁移/仓储/轨迹记录（设计 3.4、8.7）。"""
import json
import sqlite3

import pytest

from testpilot.core.contracts import StepResult, VerifyResult
from testpilot.storage import repos
from testpilot.storage.db import _columns, connect, migrate
from testpilot.storage.recorder import Recorder
from testpilot.storage.shotstore import ShotStore

ALL_TABLES = {"dsl_case", "test_suite", "test_task", "test_run", "case_exec",
              "step_trace", "kb_entry", "router_stats"}


# ---------- DDL / 迁移 ----------

def test_migrate_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "t.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert ALL_TABLES <= tables


def test_new_columns_present_after_migrate(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert "raw_json" in _columns(conn, "dsl_case")
    assert "accepted_steps_json" in _columns(conn, "dsl_case")
    assert "preconditions_json" in _columns(conn, "dsl_case")
    assert "notes" in _columns(conn, "dsl_case")
    assert "sync_status" in _columns(conn, "test_task")
    assert "preconditions_json" in _columns(conn, "test_task")
    assert "root_cause_json" in _columns(conn, "case_exec")
    assert "verify_detail" in _columns(conn, "step_trace")
    assert "source_text" in _columns(conn, "step_trace")
    assert "step_kb_hit" in _columns(conn, "step_trace")
    assert "screenshot_before_path" in _columns(conn, "step_trace")


# ---------- 截图路径（attempt 感知） ----------

def test_shotstore_attempt_suffix_keeps_retry_evidence(tmp_path):
    """复跑复用同一 case_exec 行：第 2 次尝试的截图必须换名落盘，
    否则会静默覆盖第 1 次的取证。第 1 次保持旧命名（兼容既有库内引用）。"""
    store = ShotStore(tmp_path)
    first = store.write(1, 2, 3, "after", b"a", attempt=1)
    second = store.write(1, 2, 3, "after", b"b", attempt=2)

    assert first == "1/2/3_after.png"
    assert second == "1/2/a2_3_after.png"
    assert store.resolve(first).read_bytes() == b"a"      # 首次证据未被覆盖
    assert store.resolve(second).read_bytes() == b"b"


_LEGACY_DDL = """
CREATE TABLE dsl_case (
  case_id          TEXT PRIMARY KEY,
  zentao_case_id   INTEGER,
  title            TEXT NOT NULL DEFAULT '',
  version          INTEGER NOT NULL DEFAULT 1,
  steps_json       TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'draft',
  sync_status      TEXT NOT NULL DEFAULT 'synced',
  zentao_updated_at TEXT,
  last_synced_at   TEXT,
  updated_at       TEXT NOT NULL
);
CREATE TABLE test_task (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  zentao_task_id   INTEGER NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  suite_id         INTEGER,
  case_ids_json    TEXT NOT NULL,
  driver_config_json TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL
);
"""


def test_migrate_from_legacy_library(tmp_path):
    """旧库（zentao_task_id NOT NULL、无 raw_json）迁移后可用且数据不丢。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEGACY_DDL)
    conn.execute("INSERT INTO test_task(zentao_task_id, name, case_ids_json,"
                 " created_at) VALUES (77,'旧任务','[\"TC-1\"]','2026-01-01T00:00:00')")
    conn.execute("INSERT INTO dsl_case(case_id, zentao_case_id, title, steps_json,"
                 " updated_at) VALUES ('TC-1',1,'旧用例','[]','2026-01-01T00:00:00')")
    conn.commit()

    migrate(conn)

    assert _columns(conn, "test_task")["zentao_task_id"] == 0     # NOT NULL 已放开
    row = conn.execute("SELECT * FROM test_task WHERE id=1").fetchone()
    assert row["zentao_task_id"] == 77 and row["name"] == "旧任务"
    assert row["sync_status"] == "local"

    # 迁移后可插入本地任务（zentao_task_id 为 NULL）
    conn.execute("INSERT INTO test_task(name, case_ids_json, created_at)"
                 " VALUES ('本地任务','[]','2026-01-01T00:00:00')")
    # UNIQUE 约束仍在
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO test_task(zentao_task_id, name, case_ids_json,"
                     " created_at) VALUES (77,'重复','[]','2026-01-01T00:00:00')")
    conn.commit()
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    before = _columns(conn, "test_task")
    migrate(conn)
    migrate(conn)
    assert _columns(conn, "test_task") == before


# ---------- 轨迹记录 ----------

def _seed(conn):
    repos.create_case(conn, case_id="TC-1", zentao_case_id=1, title="t",
                      raw={"steps_text": "x"}, zentao_updated_at="2026-01-01T00:00:00")
    repos.create_task_record(conn, name="n", case_ids=["TC-1"])
    return 1


class _Case:
    case_id = "TC-1"
    zentao_case_id = 1
    version = 1


def test_recorder_roundtrip_full_fields(tmp_path, cfg):
    conn = connect(tmp_path / "t.db")
    _seed(conn)
    rec = Recorder(conn, tmp_path / "shots")
    run_id = rec.begin_run({"task_id": 1, "driver_mode": "auto"})
    ce = rec.begin_case(run_id, _Case())

    res = StepResult(step_no=1, step_id="s1", action="ui.click", status="fail",
                     driver="uia", locate_ms=10, act_ms=5,
                     verify=VerifyResult(status="fail", detail="expect=S001"),
                     target_json={"key": "btn.start_detect"}, source_text="点击开始",
                     device_state={"run": "idle"},
                     error={"type": "verify_failed"}, kb_hit=7,
                     screenshot_after="1/1/1_after.png")
    rec.record_step(ce, res)
    rec.finish_case(ce, "fail", "abc123", 7, 1234, {"category": "timing"})
    rec.finish_run(run_id, {"status": "completed", "pass": 0, "total": 1})

    row = conn.execute("SELECT * FROM step_trace WHERE case_exec_id=?", (ce,)).fetchone()
    assert row["driver"] == "uia"
    assert row["verify_result"] == "fail"
    assert row["verify_detail"] == "expect=S001"
    assert row["source_text"] == "点击开始"
    assert row["step_kb_hit"] == 7
    assert json.loads(row["target_json"])["key"] == "btn.start_detect"
    assert json.loads(row["error_json"])["type"] == "verify_failed"
    assert json.loads(row["device_state_json"])["run"] == "idle"

    exec_row = conn.execute("SELECT * FROM case_exec WHERE id=?", (ce,)).fetchone()
    assert exec_row["status"] == "fail"
    assert exec_row["duration_ms"] == 1234
    assert json.loads(exec_row["root_cause_json"])["category"] == "timing"

    run_row = conn.execute("SELECT * FROM test_run WHERE id=?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"
    assert run_row["finished_at"]


def test_recorder_step_write_failure_does_not_raise(tmp_path, cfg):
    """轨迹写失败不得中断执行（表被删的极端场景）。"""
    conn = connect(tmp_path / "t.db")
    _seed(conn)
    rec = Recorder(conn, tmp_path / "shots")
    run_id = rec.begin_run({"task_id": 1})
    ce = rec.begin_case(run_id, _Case())
    conn.execute("DROP TABLE step_trace")
    rec.record_step(ce, StepResult(step_no=1, step_id="s1", action="sleep",
                                   status="pass"))   # 不应抛异常


def test_shotstore_write_and_resolve(tmp_path):
    store = ShotStore(tmp_path)
    rel = store.write(1, 2, 3, "before", b"abc")
    assert rel == "1/2/3_before.png"
    assert store.resolve(rel).read_bytes() == b"abc"
    assert store.path(1, 2, 3, "after").name == "3_after.png"


# ---------- 仓储 ----------

def test_case_crud_and_status_flow(db_conn):
    repos.create_case(db_conn, case_id="TC-1", zentao_case_id=1, title="A",
                      raw={"steps_text": "1. 点开始"}, zentao_updated_at="t1")
    row = repos.get_case(db_conn, "TC-1")
    assert row["status"] == "draft" and row["version"] == 0
    assert row["raw"]["steps_text"] == "1. 点开始"

    repos.save_case_steps(db_conn, "TC-1", steps=[{"id": "s1", "action": "sleep"}],
                          preconditions=["模拟器场景: a"], notes="备注")
    row = repos.get_case(db_conn, "TC-1")
    assert row["steps"][0]["id"] == "s1"
    assert row["preconditions"] == ["模拟器场景: a"]
    assert row["notes"] == "备注"

    assert repos.activate_case(db_conn, "TC-1") == 1
    assert repos.get_case(db_conn, "TC-1")["version"] == 1
    assert repos.activate_case(db_conn, "TC-1") == 2
    assert repos.get_case(db_conn, "TC-1")["accepted_steps"][0]["id"] == "s1"

    repos.reject_case(db_conn, "TC-1")
    assert repos.get_case(db_conn, "TC-1")["status"] == "rejected"

    repos.create_case(db_conn, case_id="TC-2", zentao_case_id=2, title="B",
                      raw=None, zentao_updated_at="t2")
    assert {r["case_id"] for r in repos.list_cases(db_conn)} == {"TC-1", "TC-2"}
    assert [r["case_id"] for r in repos.list_cases(db_conn, status="rejected")] == ["TC-1"]
    assert repos.get_case_by_zentao(db_conn, 2)["case_id"] == "TC-2"
    assert repos.count_cases_by_sync(db_conn)["synced"] == 2


def test_activate_missing_case_raises(db_conn):
    with pytest.raises(ValueError):
        repos.activate_case(db_conn, "TC-NONE")


def test_update_case_raw_and_touch(db_conn):
    repos.create_case(db_conn, case_id="TC-1", zentao_case_id=1, title="A",
                      raw={"steps_text": "old"}, zentao_updated_at="t1")
    repos.update_case_raw(db_conn, "TC-1", raw={"steps_text": "new"},
                          title="A2", zentao_updated_at="t2", sync_status="stale")
    row = repos.get_case(db_conn, "TC-1")
    assert row["raw"]["steps_text"] == "new"
    assert row["title"] == "A2"
    assert row["sync_status"] == "stale"
    repos.touch_case_sync(db_conn, "TC-1")
    assert repos.get_case(db_conn, "TC-1")["last_synced_at"]


def test_suite_crud(db_conn):
    sid = repos.create_suite(db_conn, name="方案A", case_ids=["TC-1", "TC-2"],
                             preconditions=["模拟器场景: a"],
                             driver_config={"mode": "uia_only"}, description="d")
    suite = repos.get_suite(db_conn, sid)
    assert suite["case_ids"] == ["TC-1", "TC-2"]
    assert suite["preconditions"] == ["模拟器场景: a"]
    assert suite["driver_config"]["mode"] == "uia_only"
    assert repos.get_suite_by_name(db_conn, "方案A")["id"] == sid

    repos.update_suite(db_conn, sid, case_ids=["TC-2"], description="d2")
    assert repos.get_suite(db_conn, sid)["case_ids"] == ["TC-2"]
    assert repos.get_suite(db_conn, sid)["description"] == "d2"

    assert len(repos.list_suites(db_conn)) == 1
    repos.delete_suite(db_conn, sid)
    assert repos.list_suites(db_conn) == []


def test_task_crud_and_sync_status(db_conn):
    repos.create_case(db_conn, case_id="TC-1", zentao_case_id=1, title="A",
                      raw=None, zentao_updated_at="t")
    tid = repos.create_task_record(db_conn, name="任务", case_ids=["TC-1"],
                                   preconditions=["模拟器场景: a"],
                                   driver_config={"mode": "auto"})
    task = repos.get_task(db_conn, tid)
    assert task["zentao_task_id"] is None
    assert task["sync_status"] == "local"
    assert task["preconditions"] == ["模拟器场景: a"]

    repos.set_task_sync(db_conn, tid, "synced", zentao_task_id=555)
    task = repos.get_task(db_conn, tid)
    assert (task["zentao_task_id"], task["sync_status"]) == (555, "synced")
    assert repos.get_task_by_zentao(db_conn, 555)["id"] == tid

    repos.set_task_sync(db_conn, tid, "sync_pending")
    assert repos.get_task(db_conn, tid)["sync_status"] == "sync_pending"
    assert repos.list_tasks(db_conn)[0]["id"] == tid


def test_local_task_allows_null_zentao_id_unique_still_enforced(db_conn):
    a = repos.create_task_record(db_conn, name="本地1", case_ids=["TC-1"])
    b = repos.create_task_record(db_conn, name="本地2", case_ids=["TC-1"])
    assert a != b
    with pytest.raises(sqlite3.IntegrityError):
        repos.create_task_record(db_conn, name="远程", case_ids=[], zentao_task_id=5)
        repos.create_task_record(db_conn, name="远程2", case_ids=[], zentao_task_id=5)


def test_run_and_step_queries(tmp_path, cfg):
    conn = connect(tmp_path / "t.db")
    _seed(conn)
    rec = Recorder(conn, tmp_path / "shots")
    run_id = rec.begin_run({"task_id": 1, "driver_mode": "auto"})
    ce = rec.begin_case(run_id, _Case())
    rec.record_step(ce, StepResult(step_no=1, step_id="s1", action="ui.click",
                                   status="pass", driver="uia",
                                   target_json={"key": "k"}, source_text="原文"))
    rec.finish_case(ce, "pass", None, None, 10, None)
    rec.finish_run(run_id, {"status": "completed"})

    runs = repos.list_runs(conn)
    assert runs[0]["id"] == run_id and runs[0]["stats"]["status"] == "completed"

    detail = repos.run_detail(conn, run_id)
    assert detail["env"]["task_id"] == 1
    assert detail["cases"][0]["case_id"] == "TC-1"

    steps = repos.case_steps(conn, ce)
    assert steps[0]["target"] == {"key": "k"}
    assert steps[0]["source_text"] == "原文"
    assert repos.case_steps(conn, 9999) == []


def test_archive_runs_exports_nested_data_and_requires_delete(tmp_path, cfg):
    conn = connect(tmp_path / "t.db")
    _seed(conn)
    rec = Recorder(conn, tmp_path / "shots")
    run_id = rec.begin_run({"task_id": 1, "driver_mode": "auto"})
    ce = rec.begin_case(run_id, _Case())
    rec.record_step(ce, StepResult(step_no=1, step_id="s1", action="ui.click",
                                   status="fail", driver="uia",
                                   target_json={"key": "k"}))
    rec.finish_case(ce, "fail", "sig", None, 10, {"category": "timing"})
    rec.finish_run(run_id, {"status": "completed", "fail": 1})
    conn.execute("UPDATE test_run SET started_at=?, finished_at=? WHERE id=?",
                 ("2025-01-01T00:00:00", "2025-01-01T00:01:00", run_id))
    conn.commit()

    out_dir = tmp_path / "archive"
    out = repos.archive_runs(conn, before="2026-01-01T00:00:00", out_dir=out_dir)
    assert out["count"] == 1
    assert out["deleted"] == 0
    bundle = json.loads((out_dir / f"run-{run_id}.json").read_text(encoding="utf-8"))
    assert bundle["id"] == run_id
    assert bundle["cases"][0]["steps"][0]["step_id"] == "s1"
    assert conn.execute("SELECT COUNT(*) AS n FROM test_run").fetchone()["n"] == 1

    deleted = repos.archive_runs(conn, before="2026-01-01T00:00:00",
                                 out_dir=out_dir, delete=True)
    assert deleted["deleted"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM test_run").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM case_exec").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM step_trace").fetchone()["n"] == 0


def test_kb_pending_and_get(db_conn):
    db_conn.execute(
        "INSERT INTO kb_entry(type, signature, payload_json, status, confidence,"
        " updated_at) VALUES ('failure','sig1','{\"category\":\"timing\"}',"
        "'pending',0.7,'2026-01-01T00:00:00')")
    db_conn.execute(
        "INSERT INTO kb_entry(type, signature, payload_json, status, confidence,"
        " updated_at) VALUES ('failure','sig2','{}','active',1.0,'2026-01-01T00:00:00')")
    db_conn.commit()
    pending = repos.kb_pending(db_conn)
    assert len(pending) == 1
    assert pending[0]["payload"]["category"] == "timing"
    assert repos.kb_get(db_conn, pending[0]["id"])["signature"] == "sig1"
    assert repos.kb_get(db_conn, 4242) is None


def test_repos_load_defaults_for_missing_json(db_conn):
    """JSON 列异常/为空时仓储不得抛错。"""
    db_conn.execute(
        "INSERT INTO dsl_case(case_id, title, version, steps_json, updated_at)"
        " VALUES ('TC-9','',0,'not-json','2026-01-01T00:00:00')")
    db_conn.commit()
    row = repos.get_case(db_conn, "TC-9")
    assert row["steps"] == [] and row["preconditions"] == []

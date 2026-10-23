"""SQLite 连接与迁移 — WAL 模式；DDL 与设计文档 3.4 一致。

M1 起的增量（见 docs/dev-log.md 决策 D2/D3/D4）：
- ``dsl_case.raw_json``            禅道原文与时间戳快照（stale 重解析 / 人审对照）
- ``dsl_case.accepted_steps_json`` 最近一次生效的 DSL 快照（冲突三版比对）
- ``test_task.zentao_task_id``     放开 NOT NULL（M1 本地优先；禅道建任务属 M2）
- ``test_task.sync_status``        本地/已同步/待补偿
- ``step_trace.source_text`` 等    轨迹落库字段扩充（只增不改语义）
- ``case_exec.attempts/retry_json``、``step_trace.attempt``
  失败用例复跑（flake 过滤）：一条用例仍只占一行 case_exec，每次尝试的摘要与轨迹分列

``migrate()`` 对已存在的旧库做幂等补齐：缺列补列；``test_task`` 若仍是
``zentao_task_id NOT NULL`` 则做一次表重建（不依赖 ALTER TABLE RENAME，
避免 SQLite 3.25+ 的重命名引用改写问题）。
"""
import sqlite3
from pathlib import Path

# test_task 的权威 DDL（重建迁移复用，必须与 _DDL 中的定义一致）
# 注意：本模板经 .format(name=...) 渲染，JSON 默认值里的花括号必须写成 {{}}
_TEST_TASK_DDL = """
CREATE TABLE {name} (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  zentao_task_id   INTEGER UNIQUE,             -- 可空：M1 本地优先，M2 起回填禅道任务号
  name             TEXT NOT NULL,
  suite_id         INTEGER REFERENCES test_suite(id),
  case_ids_json    TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]',  -- 建任务时的方案前置场景快照
  driver_config_json TEXT NOT NULL DEFAULT '{{}}',
  sync_status      TEXT NOT NULL DEFAULT 'local',  -- local|synced|sync_pending
  created_at       TEXT NOT NULL
);
"""

_DDL = """
CREATE TABLE IF NOT EXISTS source_sync_state (
  source           TEXT NOT NULL,
  scope            TEXT NOT NULL,
  watermark        TEXT,
  synced_at        TEXT NOT NULL,
  detail_json      TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(source, scope)
);

CREATE TABLE IF NOT EXISTS dsl_case (
  case_id          TEXT PRIMARY KEY,
  zentao_case_id   INTEGER,
  title            TEXT NOT NULL DEFAULT '',
  version          INTEGER NOT NULL DEFAULT 1,
  steps_json       TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]',
  notes            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'draft',
  sync_status      TEXT NOT NULL DEFAULT 'synced',
  raw_json         TEXT,
  accepted_steps_json TEXT,
  zentao_updated_at TEXT,
  last_synced_at   TEXT,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dsl_zentao ON dsl_case(zentao_case_id);
CREATE INDEX IF NOT EXISTS idx_dsl_status ON dsl_case(status, sync_status);

CREATE TABLE IF NOT EXISTS test_suite (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL UNIQUE,
  case_ids_json    TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]',
  driver_config_json TEXT NOT NULL DEFAULT '{}',
  description      TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_task (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  zentao_task_id   INTEGER UNIQUE,
  name             TEXT NOT NULL,
  suite_id         INTEGER REFERENCES test_suite(id),
  case_ids_json    TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]',
  driver_config_json TEXT NOT NULL DEFAULT '{}',
  sync_status      TEXT NOT NULL DEFAULT 'local',
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_run (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          INTEGER NOT NULL REFERENCES test_task(id),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  env_snapshot_json TEXT NOT NULL,
  stats_json       TEXT NOT NULL DEFAULT '{}',
  status           TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS case_exec (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES test_run(id),
  case_id          TEXT NOT NULL REFERENCES dsl_case(case_id),
  zentao_case_id   INTEGER,
  dsl_version      INTEGER NOT NULL,
  status           TEXT NOT NULL,
  duration_ms      INTEGER NOT NULL DEFAULT 0,
  failure_signature TEXT,
  kb_hit_id        INTEGER,
  root_cause_json  TEXT,
  attempts         INTEGER NOT NULL DEFAULT 1,  -- 执行次数（>1 表示发生失败复跑）
  retry_json       TEXT,                        -- 每次尝试的摘要（复跑才写）
  started_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_exec_run ON case_exec(run_id);
CREATE INDEX IF NOT EXISTS idx_case_exec_sig ON case_exec(failure_signature);

CREATE TABLE IF NOT EXISTS step_trace (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  case_exec_id     INTEGER NOT NULL REFERENCES case_exec(id),
  step_no          INTEGER NOT NULL,
  attempt          INTEGER NOT NULL DEFAULT 1,  -- 第几次执行（复跑时同一 case 会有多份轨迹）
  step_id          TEXT NOT NULL,
  action           TEXT NOT NULL,
  target_json      TEXT,
  driver           TEXT,
  locate_ms        INTEGER,
  act_ms           INTEGER,
  verify_result    TEXT,
  verify_detail    TEXT,
  screenshot_path  TEXT,
  screenshot_before_path TEXT,   -- before 截图（与 screenshot_path=after 分列，双截图可回放）
  device_state_json TEXT,
  source_text      TEXT,
  receipt_json     TEXT,
  step_kb_hit      INTEGER,
  error_json       TEXT,
  driver_chain_json TEXT,
  at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_case ON step_trace(case_exec_id);

CREATE TABLE IF NOT EXISTS kb_entry (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  type             TEXT NOT NULL,
  signature        TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active',
  hits             INTEGER NOT NULL DEFAULT 0,
  confidence       REAL NOT NULL DEFAULT 1.0,
  source_run_id    INTEGER,
  last_success_at  TEXT,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_kb_sig ON kb_entry(type, signature);

CREATE TABLE IF NOT EXISTS router_stats (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  target_key       TEXT NOT NULL,
  driver           TEXT NOT NULL,
  success_count    INTEGER NOT NULL DEFAULT 0,
  fail_count       INTEGER NOT NULL DEFAULT 0,
  fail_streak      INTEGER NOT NULL DEFAULT 0,
  avg_locate_ms    INTEGER NOT NULL DEFAULT 0,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_router ON router_stats(target_key, driver);
"""

# 迁移补齐的列：(表, 列, 列定义)
_ADDED_COLUMNS = (
    ("dsl_case", "raw_json", "TEXT"),
    ("dsl_case", "module_id", "INTEGER"),      # 禅道用例分组（模块）id
    ("dsl_case", "module_name", "TEXT"),       # 分组名（由 modules 接口解析；解析失败为空）
    ("dsl_case", "accepted_steps_json", "TEXT"),
    ("dsl_case", "preconditions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("dsl_case", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("test_task", "sync_status", "TEXT NOT NULL DEFAULT 'local'"),
    ("test_task", "preconditions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("case_exec", "root_cause_json", "TEXT"),
    ("case_exec", "attempts", "INTEGER NOT NULL DEFAULT 1"),
    ("case_exec", "retry_json", "TEXT"),
    ("step_trace", "verify_detail", "TEXT"),
    ("step_trace", "screenshot_before_path", "TEXT"),
    ("step_trace", "source_text", "TEXT"),
    ("step_trace", "step_kb_hit", "INTEGER"),
    ("step_trace", "driver_chain_json", "TEXT"),
    ("step_trace", "attempt", "INTEGER NOT NULL DEFAULT 1"),
    ("step_trace", "receipt_json", "TEXT"),
    ("step_trace", "status", "TEXT"),
    ("test_task", "remote_spec_json", "TEXT"),
    ("case_exec", "dsl_snapshot_json", "TEXT"),
    ("test_run", "owner_session", "TEXT"),
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # serve 模式下连接在主线程创建、RPC 在专用线程池执行，必须允许跨线程使用；
    # 事件循环线程不得直接触碰连接（rpc.py 把全部 dispatch 派发到线程池）。
    # 长任务线程与短 IO 线程仍可能并发触达连接（WAL 下语句级安全），
    # busy_timeout 兜底写写冲突。
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _rebuild_test_task_if_needed(conn)
    for table, column, decl in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, decl)
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """返回 {列名: notnull}；PRAGMA 行用下标访问以兼容任意 row_factory。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1]: r[3] for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   decl: str) -> None:
    if column in _columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _rebuild_test_task_if_needed(conn: sqlite3.Connection) -> None:
    """旧库 test_task.zentao_task_id 为 NOT NULL 时重建为可空（保留 UNIQUE）。"""
    cols = _columns(conn, "test_task")
    if not cols or cols.get("zentao_task_id") != 1:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            "BEGIN;"
            + _TEST_TASK_DDL.format(name="test_task_migrated")
            + "INSERT INTO test_task_migrated(id, zentao_task_id, name, suite_id,"
              " case_ids_json, driver_config_json, created_at)"
              " SELECT id, zentao_task_id, name, suite_id, case_ids_json,"
              " driver_config_json, created_at FROM test_task;"
            + "DROP TABLE test_task;"
            + _TEST_TASK_DDL.format(name="test_task")
            + "INSERT INTO test_task(id, zentao_task_id, name, suite_id,"
              " case_ids_json, driver_config_json, sync_status, created_at)"
              " SELECT id, zentao_task_id, name, suite_id, case_ids_json,"
              " driver_config_json, 'local', created_at FROM test_task_migrated;"
            + "DROP TABLE test_task_migrated;"
            + "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def recover_orphaned_runs(conn) -> int:
    """Recover only runs with a known, terminated owner; preserve legacy ownership."""
    import json
    rows = conn.execute(
        "SELECT id, owner_session FROM test_run WHERE status='running' "
        "AND owner_session IS NOT NULL").fetchall()
    recovered = 0
    for row in rows:
        try:
            pid = int(row["owner_session"].split(":", 1)[0])
        except (ValueError, AttributeError):
            continue
        if _process_alive(pid):
            continue
        with conn:
            conn.execute("UPDATE case_exec SET status='error' "
                         "WHERE run_id=? AND status='running'", (row["id"],))
            conn.execute("UPDATE test_run SET status='interrupted', "
                         "finished_at=datetime('now','localtime'), stats_json=? WHERE id=?",
                         (json.dumps({"status": "interrupted", "recovery": "owner exited",
                                      "evidence_incomplete": True}), row["id"]))
        recovered += 1
    return recovered


def _process_alive(pid: int) -> bool:
    import os
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

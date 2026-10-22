"""各表仓储 — 用例/方案/任务 CRUD 与 run 查询（RPC、CLI、报告共用的读取层）。

事务约定：写操作自行 commit（SQLite 单写者，WAL 下足够）；
JSON 列一律在仓储层完成序列化/反序列化，调用方只处理 dict / pydantic。
"""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

# ---------- 通用 ----------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _loads(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ---------- dsl_case ----------

_CASE_COLUMNS = ("case_id, zentao_case_id, title, version, steps_json,"
                 " preconditions_json, notes, status, sync_status, raw_json,"
                 " accepted_steps_json, zentao_updated_at, last_synced_at,"
                 " updated_at, module_id, module_name")


def _case_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["steps"] = _loads(d.pop("steps_json", None), [])
    d["preconditions"] = _loads(d.pop("preconditions_json", None), [])
    d["raw"] = _loads(d.pop("raw_json", None), None)
    d["accepted_steps"] = _loads(d.pop("accepted_steps_json", None), None)
    return d


def create_case(conn: sqlite3.Connection, *, case_id: str, zentao_case_id: int | None,
                title: str, raw: dict | None, zentao_updated_at: str | None,
                module_id: int | None = None,
                module_name: str | None = None) -> None:
    """新用例入库：草稿态（version=0），DSL 待解析后写入。

    ``module_id/module_name`` 是禅道用例分组（模块）：分组名解析不出来时只存 id，
    原文（raw_json）里始终保有原始字段。
    """
    now = _now()
    conn.execute(
        "INSERT INTO dsl_case(case_id, zentao_case_id, title, version, steps_json,"
        " status, sync_status, raw_json, zentao_updated_at, last_synced_at,"
        " updated_at, module_id, module_name)"
        " VALUES (?,?,?,0,'[]','draft','synced',?,?,?,?,?,?)"
        " ON CONFLICT(case_id) DO UPDATE SET title=excluded.title,"
        " raw_json=excluded.raw_json, zentao_updated_at=excluded.zentao_updated_at,"
        " last_synced_at=excluded.last_synced_at, updated_at=excluded.updated_at,"
        " module_id=excluded.module_id, module_name=excluded.module_name",
        (case_id, zentao_case_id, title, json.dumps(raw, ensure_ascii=False)
         if raw is not None else None, zentao_updated_at, now, now,
         module_id, module_name))
    conn.commit()


def touch_case_sync(conn: sqlite3.Connection, case_id: str) -> None:
    """仅刷新同步时间（远端无变更时调用）。"""
    conn.execute("UPDATE dsl_case SET last_synced_at=? WHERE case_id=?",
                 (_now(), case_id))
    conn.commit()


def update_case_raw(conn: sqlite3.Connection, case_id: str, *, raw: dict,
                    title: str | None = None, zentao_updated_at: str | None = None,
                    sync_status: str, module_id: int | None = None,
                    module_name: str | None = None) -> None:
    """禅道原文变更：刷新原文快照并更新同步状态（不改 DSL 与版本）。

    ``module_*`` 为 None 表示"本次同步没有解析到"，保持库中原值不清空。
    """
    sets, args = ["raw_json=?", "zentao_updated_at=?", "last_synced_at=?",
                  "sync_status=?", "updated_at=?"], [
        json.dumps(raw, ensure_ascii=False), zentao_updated_at, _now(),
        sync_status, _now()]
    if title is not None:
        sets.append("title=?")
        args.append(title)
    if module_id is not None:
        sets.append("module_id=?")
        args.append(module_id)
    if module_name is not None:
        sets.append("module_name=?")
        args.append(module_name)
    args.append(case_id)
    conn.execute(f"UPDATE dsl_case SET {', '.join(sets)} WHERE case_id=?", args)
    conn.commit()


def save_case_steps(conn: sqlite3.Connection, case_id: str, *, steps: list,
                    version: int | None = None, status: str | None = None,
                    sync_status: str | None = None,
                    preconditions: list | None = None,
                    notes: str | None = None,
                    title: str | None = None) -> None:
    """写入 DSL 步骤（解析草稿或人工编辑结果）；None 字段保持不变。"""
    sets, args = ["steps_json=?", "updated_at=?"], [
        json.dumps(steps, ensure_ascii=False), _now()]
    if version is not None:
        sets.append("version=?")
        args.append(version)
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if sync_status is not None:
        sets.append("sync_status=?")
        args.append(sync_status)
    if title is not None:
        # 人工编辑导出文件时可能改了标题，导入必须落库（否则静默丢失）
        sets.append("title=?")
        args.append(title)
    if preconditions is not None:
        sets.append("preconditions_json=?")
        args.append(json.dumps(preconditions, ensure_ascii=False))
    if notes is not None:
        sets.append("notes=?")
        args.append(notes)
    args.append(case_id)
    conn.execute(f"UPDATE dsl_case SET {', '.join(sets)} WHERE case_id=?", args)
    conn.commit()


def activate_case(conn: sqlite3.Connection, case_id: str) -> int:
    """人审通过：草稿 -> active，版本 +1，并把当前步骤冻结为 accepted 快照。"""
    row = conn.execute("SELECT version, steps_json FROM dsl_case WHERE case_id=?",
                       (case_id,)).fetchone()
    if row is None:
        raise ValueError(f"dsl_case 不存在: {case_id}")
    version = int(row["version"] or 0) + 1
    conn.execute(
        "UPDATE dsl_case SET status='active', version=?, accepted_steps_json=?,"
        " sync_status='synced', updated_at=? WHERE case_id=?",
        (version, row["steps_json"], _now(), case_id))
    conn.commit()
    return version


def reject_case(conn: sqlite3.Connection, case_id: str) -> None:
    """人审拒绝：进入待改进清单（回馈禅道用例质量），原文与草稿均留档。"""
    conn.execute("UPDATE dsl_case SET status='rejected', updated_at=? WHERE case_id=?",
                 (_now(), case_id))
    conn.commit()


def get_case(conn: sqlite3.Connection, case_id: str) -> dict | None:
    row = conn.execute(f"SELECT {_CASE_COLUMNS} FROM dsl_case WHERE case_id=?",
                       (case_id,)).fetchone()
    return _case_to_dict(row) if row else None


def get_case_by_zentao(conn: sqlite3.Connection, zentao_case_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_CASE_COLUMNS} FROM dsl_case WHERE zentao_case_id=?",
        (zentao_case_id,)).fetchone()
    return _case_to_dict(row) if row else None


def list_cases(conn: sqlite3.Connection, status: str | None = None,
               sync_status: str | None = None) -> list[dict]:
    sql = f"SELECT {_CASE_COLUMNS} FROM dsl_case"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if sync_status:
        where.append("sync_status=?")
        args.append(sync_status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY case_id"
    return [_case_to_dict(r) for r in conn.execute(sql, args)]


# ---------- test_suite ----------

def create_suite(conn: sqlite3.Connection, *, name: str, case_ids: list[str],
                 preconditions: list[str] | None = None,
                 driver_config: dict | None = None,
                 description: str = "") -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO test_suite(name, case_ids_json, preconditions_json,"
        " driver_config_json, description, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (name, json.dumps(case_ids, ensure_ascii=False),
         json.dumps(preconditions or [], ensure_ascii=False),
         json.dumps(driver_config or {}, ensure_ascii=False),
         description, now, now))
    conn.commit()
    return cur.lastrowid


def update_suite(conn: sqlite3.Connection, suite_id: int, *, name: str | None = None,
                 case_ids: list[str] | None = None,
                 preconditions: list[str] | None = None,
                 driver_config: dict | None = None,
                 description: str | None = None) -> None:
    # (目标列, 值, 是否 JSON 编码)
    fields = (
        ("name", name, False),
        ("case_ids_json", case_ids, True),
        ("preconditions_json", preconditions, True),
        ("driver_config_json", driver_config, True),
        ("description", description, False),
    )
    sets, args = ["updated_at=?"], [_now()]
    for col, val, is_json in fields:
        if val is None:
            continue
        sets.append(f"{col}=?")
        args.append(json.dumps(val, ensure_ascii=False) if is_json else val)
    args.append(suite_id)
    conn.execute(f"UPDATE test_suite SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()


def _suite_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["case_ids"] = _loads(d.pop("case_ids_json", None), [])
    d["preconditions"] = _loads(d.pop("preconditions_json", None), [])
    d["driver_config"] = _loads(d.pop("driver_config_json", None), {})
    return d


def get_suite(conn: sqlite3.Connection, suite_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM test_suite WHERE id=?", (suite_id,)).fetchone()
    return _suite_to_dict(row) if row else None


def get_suite_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM test_suite WHERE name=?", (name,)).fetchone()
    return _suite_to_dict(row) if row else None


def list_suites(conn: sqlite3.Connection) -> list[dict]:
    return [_suite_to_dict(r) for r in
            conn.execute("SELECT * FROM test_suite ORDER BY id")]


def delete_suite(conn: sqlite3.Connection, suite_id: int) -> None:
    conn.execute("DELETE FROM test_suite WHERE id=?", (suite_id,))
    conn.commit()


# ---------- test_task ----------

def create_task_record(conn: sqlite3.Connection, *, name: str, case_ids: list[str],
                       suite_id: int | None = None,
                       preconditions: list[str] | None = None,
                       driver_config: dict | None = None,
                       zentao_task_id: int | None = None,
                       sync_status: str = "local") -> int:
    cur = conn.execute(
        "INSERT INTO test_task(zentao_task_id, name, suite_id, case_ids_json,"
        " preconditions_json, driver_config_json, sync_status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (zentao_task_id, name, suite_id,
         json.dumps(case_ids, ensure_ascii=False),
         json.dumps(preconditions or [], ensure_ascii=False),
         json.dumps(driver_config or {}, ensure_ascii=False),
         sync_status, _now()))
    conn.commit()
    return cur.lastrowid


def _task_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["case_ids"] = _loads(d.pop("case_ids_json", None), [])
    d["preconditions"] = _loads(d.pop("preconditions_json", None), [])
    d["driver_config"] = _loads(d.pop("driver_config_json", None), {})
    return d


def get_task(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM test_task WHERE id=?", (task_id,)).fetchone()
    return _task_to_dict(row) if row else None


def get_task_by_zentao(conn: sqlite3.Connection, zentao_task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM test_task WHERE zentao_task_id=?",
                       (zentao_task_id,)).fetchone()
    return _task_to_dict(row) if row else None


def list_tasks(conn: sqlite3.Connection) -> list[dict]:
    tasks = [_task_to_dict(r) for r in
             conn.execute("SELECT * FROM test_task ORDER BY id DESC")]
    # 每个任务各取最近一次，避免全库 LIMIT 把较早任务的执行记录截掉。
    latest = conn.execute(
        "SELECT r.id, r.task_id, r.started_at, r.finished_at, r.status, r.stats_json"
        " FROM test_run r JOIN"
        " (SELECT task_id, MAX(id) AS id FROM test_run GROUP BY task_id) latest"
        " ON r.id=latest.id").fetchall()
    by_task = {r["task_id"]: {**dict(r), "stats": _loads(r["stats_json"], {})}
               for r in latest}
    for task in tasks:
        recent = by_task.get(task["id"])
        task["runs"] = [recent] if recent else []
    return tasks


def set_task_sync(conn: sqlite3.Connection, task_id: int, sync_status: str,
                  zentao_task_id: int | None = None) -> None:
    if zentao_task_id is None:
        conn.execute("UPDATE test_task SET sync_status=? WHERE id=?",
                     (sync_status, task_id))
    else:
        conn.execute("UPDATE test_task SET sync_status=?, zentao_task_id=? WHERE id=?",
                     (sync_status, zentao_task_id, task_id))
    conn.commit()


# ---------- test_run / case_exec / step_trace 查询 ----------

def list_runs(conn: sqlite3.Connection, limit: int = 50,
              *, task_id: int | None = None) -> list[dict]:
    where = " WHERE task_id=?" if task_id is not None else ""
    params = (task_id, limit) if task_id is not None else (limit,)
    rows = conn.execute(
        "SELECT id, task_id, started_at, finished_at, status, stats_json"
        f" FROM test_run{where} ORDER BY id DESC LIMIT ?", params).fetchall()
    return [{**dict(r), "stats": _loads(r["stats_json"], {})} for r in rows]


def run_detail(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM test_run WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    cases = conn.execute(
        "SELECT * FROM case_exec WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = dict(row)
    out["stats"] = _loads(row["stats_json"], {})
    out["env"] = _loads(row["env_snapshot_json"], {})
    out["cases"] = []
    for c in cases:
        d = dict(c)
        d["root_cause"] = _loads(d.get("root_cause_json"), None)
        # 每次尝试的摘要（复跑才有）：首次失败的签名/归因不会被最终通过吞掉
        d["retry"] = _loads(d.pop("retry_json", None), None)
        out["cases"].append(d)
    return out


def case_steps(conn: sqlite3.Connection, case_exec_id: int,
               attempt: int | None = None) -> list[dict]:
    """步骤轨迹；``attempt=None`` 取**最后一次尝试**。

    复跑的用例在 step_trace 里有多份轨迹（step_no 会重复），混在一起回放会看到
    "同一步骤出现两次且状态矛盾" —— 所以默认只看最后一次，需要看首次失败时显式传 attempt。
    """
    if attempt is None:
        row = conn.execute("SELECT MAX(attempt) FROM step_trace WHERE case_exec_id=?",
                           (case_exec_id,)).fetchone()
        attempt = int((row[0] if row is not None else None) or 1)
    rows = conn.execute(
        "SELECT * FROM step_trace WHERE case_exec_id=? AND attempt=? ORDER BY step_no",
        (case_exec_id, int(attempt))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["target"] = _loads(d.pop("target_json", None), None)
        d["device_state"] = _loads(d.pop("device_state_json", None), None)
        d["receipt"] = _loads(d.pop("receipt_json", None), None)
        d["error"] = _loads(d.pop("error_json", None), None)
        d["driver_chain"] = _loads(d.pop("driver_chain_json", None), None)
        out.append(d)
    return out


def run_case_execs(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM case_exec WHERE run_id=? ORDER BY id",
                        (run_id,)).fetchall()
    return [dict(r) for r in rows]


def archive_runs(conn: sqlite3.Connection, *, before: str, out_dir: str | Path,
                 delete: bool = False, resource_roots: dict | None = None) -> dict:
    """导出指定时间之前的 run；可选地从在线库删除已导出的记录。

    归档包是 JSON 文件，包含 run、case_exec 和 step_trace 三层数据。
    ``delete`` 默认关闭，调用方必须显式指定才会删除；删除顺序遵循外键依赖。
    ``before`` 使用 ISO-8601 本地无时区时间（与库内时间格式一致）。
    """
    try:
        cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"归档截止时间不是有效 ISO-8601: {before}") from e
    if cutoff.tzinfo is not None:
        # 库内时间是本地无时区串（_now 用 time.strftime 落库）：带时区输入必须
        # 折算到**本地**墙钟再剥 tzinfo。折成 UTC 会在 UTC+X 时区漏归档、
        # 负偏移时区多归档（delete=True 时等于误删）。
        cutoff = cutoff.astimezone().replace(tzinfo=None)
    cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    runs = conn.execute(
        "SELECT * FROM test_run WHERE status != 'running' AND COALESCE(finished_at, started_at) < ? "
        "ORDER BY id", (cutoff_text,)).fetchall()
    bundles: list[dict] = []
    for run in runs:
        run_dict = dict(run)
        run_dict["env"] = _loads(run_dict.pop("env_snapshot_json", None), {})
        run_dict["stats"] = _loads(run_dict.pop("stats_json", None), {})
        cases = []
        for case in conn.execute(
                "SELECT * FROM case_exec WHERE run_id=? ORDER BY id",
                (run["id"],)).fetchall():
            case_dict = dict(case)
            case_dict["root_cause"] = _loads(
                case_dict.pop("root_cause_json", None), None)
            steps = []
            for step in conn.execute(
                    "SELECT * FROM step_trace WHERE case_exec_id=? ORDER BY step_no",
                    (case["id"],)).fetchall():
                step_dict = dict(step)
                for key in ("target_json", "device_state_json", "receipt_json",
                            "error_json", "driver_chain_json"):
                    step_dict[key.removesuffix("_json")] = _loads(
                        step_dict.pop(key, None), None)
                steps.append(step_dict)
            case_dict["steps"] = steps
            cases.append(case_dict)
        run_dict["cases"] = cases
        bundles.append(run_dict)

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for bundle in bundles:
        path = target / f"run-{bundle['id']}.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        written.append(str(path))
        if resource_roots:
            import hashlib
            import shutil
            resources = []
            for kind, root in resource_roots.items():
                base = Path(root).resolve()
                source = base / str(bundle["id"])
                if not source.is_dir():
                    continue
                for file in source.rglob("*"):
                    if not file.is_file() or file.is_symlink():
                        continue
                    if not file.resolve().is_relative_to(base):
                        raise ValueError("归档资源越过资源根目录")
                    destination = target / f"run-{bundle['id']}-assets" / kind / file.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, destination)
                    resources.append({"source": f"{kind}/{file.relative_to(base).as_posix()}",
                                      "path": destination.relative_to(target).as_posix(),
                                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
            manifest = target / f"run-{bundle['id']}-assets.json"
            manifest.write_text(json.dumps(resources, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            written.append(str(manifest))

    deleted = 0
    if delete and bundles:
        ids = [bundle["id"] for bundle in bundles]
        placeholders = ",".join("?" for _ in ids)
        with conn:
            case_rows = conn.execute(
                f"SELECT id FROM case_exec WHERE run_id IN ({placeholders})", ids
            ).fetchall()
            case_ids = [row["id"] for row in case_rows]
            if case_ids:
                case_placeholders = ",".join("?" for _ in case_ids)
                conn.execute(
                    f"DELETE FROM step_trace WHERE case_exec_id IN ({case_placeholders})",
                    case_ids)
                conn.execute(
                    f"DELETE FROM case_exec WHERE id IN ({case_placeholders})",
                    case_ids)
            conn.execute(f"DELETE FROM test_run WHERE id IN ({placeholders})", ids)
            deleted = len(ids)

    return {"before": cutoff_text, "count": len(bundles), "files": written,
            "deleted": deleted, "delete_requested": delete}


# ---------- kb_entry ----------

def kb_pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, type, signature, payload_json, confidence, updated_at"
        " FROM kb_entry WHERE status='pending' ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = _loads(d.pop("payload_json", None), {})
        out.append(d)
    return out


def kb_get(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM kb_entry WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = _loads(d.pop("payload_json", None), {})
    return d


def count_cases_by_sync(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT sync_status, COUNT(*) AS n FROM dsl_case"
                        " GROUP BY sync_status").fetchall()
    return {r["sync_status"]: r["n"] for r in rows}

"""run 级统计 — 驱动降级率（设计 10 章）与回归质量分析（设计 9.1 / 14.3）。

三段互不依赖的口径：

1. ``driver_stats`` / ``aggregate``：**单 run 内**的驱动降级率与 uia→vis 迁移。
   数据地基是 ``step_trace.driver_chain_json``（每次驱动尝试的成败与耗时）。
   - **尝试（attempt）**：一次驱动在该步骤上完整跑一遍（定位 + 动作）；
   - **降级（degraded）**：同一步骤内前序驱动失败、后续驱动成功（如 ``uia→vision``）；
   - **降级率** = 降级步骤数 / 有驱动尝试的步骤数。
   旧数据（加入该列之前）没有尝试链，回退为 ``driver`` 列构造的单元素链，
   统计不会因此失真为 0；链里 ``ok=None`` 表示历史数据无法判定成败。

2. ``case_stability``：**跨 run** 的用例稳定性（偶发 or 必现）。
   读 ``case_exec`` 最近 N 次执行，产出通过率/翻转次数/当前连续状态与
   ``stable|flaky|broken|unknown`` 判定。这是按需计算的**派生视图**：
   不写进 run 的 stats —— 否则历史 run 的稳定性会随新 run 不断变化，落库即失真。

3. ``compare_runs``：两次 run 的用例级对比（新增失败/修复/持续失败/增删用例），
   供回归收尾"这次改动破坏了什么"直接使用。
"""
import json


def driver_stats(conn, run_id: int) -> dict:
    """从轨迹汇总一次 run 的驱动使用与降级情况（写进 run stats，报告/UI 直接消费）。

    复跑/步骤级重试会让同一步骤在 step_trace 里留下多行；统计口径与报告一致，
    只取每一步**最后一次尝试**的轨迹，否则失败复跑通过的用例会把
    steps 分母与 failed_steps 双双抬高。
    """
    rows = conn.execute(
        "SELECT s.driver, s.driver_chain_json FROM step_trace s"
        " JOIN case_exec c ON c.id = s.case_exec_id"
        " WHERE c.run_id=? AND s.id = ("
        "   SELECT MAX(s3.id) FROM step_trace s3"
        "   WHERE s3.case_exec_id = s.case_exec_id"
        "     AND s3.step_no = s.step_no"
        "     AND s3.attempt = (SELECT MAX(s2.attempt) FROM step_trace s2"
        "                       WHERE s2.case_exec_id = s.case_exec_id"
        "                         AND s2.step_no = s.step_no))"
        " ORDER BY s.id", (run_id,)).fetchall()
    return aggregate(rows)


def aggregate(rows) -> dict:
    """纯函数聚合（传入 step_trace 行序列），便于单测直接构造链条。"""
    by_driver: dict[str, dict] = {}
    transitions: dict[tuple[str, str], dict] = {}
    steps = degraded = failed = 0

    for row in rows:
        chain = _chain_of(row)
        if not chain:
            continue
        steps += 1
        final_ok = chain[-1].get("ok")
        if len(chain) >= 2 and final_ok is True:
            degraded += 1
        if final_ok is False:
            failed += 1

        for idx, attempt in enumerate(chain):
            name = str(attempt.get("driver") or "?")
            stat = by_driver.setdefault(
                name, {"attempts": 0, "success": 0, "fail": 0, "ms": 0})
            stat["attempts"] += 1
            if attempt.get("ok") is True:
                stat["success"] += 1
            elif attempt.get("ok") is False:
                stat["fail"] += 1
            ms = attempt.get("ms")
            stat["ms"] += int(ms) if isinstance(ms, (int, float)) else 0
            if idx + 1 < len(chain):
                nxt = str(chain[idx + 1].get("driver") or "?")
                tr = transitions.setdefault((name, nxt),
                                            {"count": 0, "resolved": 0})
                tr["count"] += 1
                if final_ok is True:
                    tr["resolved"] += 1

    for stat in by_driver.values():
        decided = stat["success"] + stat["fail"]
        stat["success_rate"] = round(stat["success"] / decided, 4) if decided else None
        stat["avg_ms"] = round(stat["ms"] / stat["attempts"]) if stat["attempts"] else 0

    return {
        "steps": steps,
        "degraded_steps": degraded,
        "failed_steps": failed,
        "downgrade_rate": round(degraded / steps, 4) if steps else None,
        "by_driver": by_driver,
        "transitions": [
            {"from": src, "to": dst, "count": val["count"],
             "resolved": val["resolved"]}
            for (src, dst), val in sorted(transitions.items())
        ],
    }


# ---------- 跨 run：用例稳定性 ----------

#: 稳定性的四档判据（样本不足时不硬下结论，宁可为 unknown）
LEVEL_STABLE = "stable"       # 窗口内全部通过（且没有靠复跑兜住的）
LEVEL_FLAKY = "flaky"         # 有通过也有失败，或"全通过但出现过失败复跑" —— 疑似偶发
LEVEL_BROKEN = "broken"       # 窗口内从未通过 —— 必现失败
LEVEL_UNKNOWN = "unknown"     # 无记录或样本不足（< 2 次）

#: 默认稳定性窗口：每个用例回看最近多少次执行（报告与 UI 共用，勿各写整数）
DEFAULT_WINDOW = 10


def case_stability(conn, case_ids=None, *, window: int = DEFAULT_WINDOW,
                   exclude_run_id: int | None = None) -> dict[str, dict]:
    """按用例汇总最近 ``window`` 次执行的稳定性（新 -> 旧）。

    ``case_ids=None`` 表示统计全库所有用例；显式传列表时按列表过滤，
    **传空列表返回空结果**（不降级成"全库"，避免调用方一个空集合就触发全表扫描）。

    单次样本（``runs < 2``）无法谈稳定性，判 ``unknown`` —— 首次失败会被
    如实标成"样本不足"而不是"必现"，避免报告用一次结果下结论。

    **复跑口径**：状态是 pass 但 ``attempts > 1``（失败复跑后才通过）的批次
    ``retried`` 计入 —— 全通过但出现过复跑的用例判 ``flaky``。
    "要跑两次才过"本身就是不稳定的证据，不能因为最终状态是 pass 就当稳定。
    """
    ids = None if case_ids is None else [str(c) for c in case_ids if str(c)]
    if ids is not None and not ids:
        return {}

    where: list[str] = []
    params: list = []
    if ids is not None:
        where.append(f"case_id IN ({','.join('?' * len(ids))})")
        params += ids
    if exclude_run_id is not None:
        where.append("run_id <> ?")
        params.append(int(exclude_run_id))
    # Compare stability within the latest DSL/product/driver/retry conditions.
    where.append("dsl_version = (SELECT latest.dsl_version FROM case_exec latest "
                 "WHERE latest.case_id=case_exec.case_id ORDER BY latest.id DESC LIMIT 1)")
    where.append("COALESCE((SELECT json_extract(env_snapshot_json, '$.test_app_version') "
                 "FROM test_run WHERE id=case_exec.run_id),'') = COALESCE(("
                 "SELECT json_extract(r.env_snapshot_json, '$.test_app_version') "
                 "FROM case_exec latest JOIN test_run r ON r.id=latest.run_id "
                 "WHERE latest.case_id=case_exec.case_id ORDER BY latest.id DESC LIMIT 1),'')")
    for key in ("driver_mode", "case_retry", "emulator_version"):
        where.append(
            f"COALESCE((SELECT json_extract(env_snapshot_json, '$.{key}') "
            "FROM test_run WHERE id=case_exec.run_id),'') = COALESCE(("
            f"SELECT json_extract(r.env_snapshot_json, '$.{key}') "
            "FROM case_exec latest JOIN test_run r ON r.id=latest.run_id "
            "WHERE latest.case_id=case_exec.case_id ORDER BY latest.id DESC LIMIT 1),'')")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    # 窗口函数在 SQL 侧截断每个用例的最近 N 次，避免把大库整表拉进内存
    rows = conn.execute(
        "SELECT case_id, run_id, status, started_at, attempts FROM ("
        "  SELECT case_id, run_id, status, started_at, attempts,"
        "         ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY id DESC) AS rn"
        f"  FROM case_exec {clause}"
        ") WHERE rn <= ? ORDER BY case_id, rn",
        (*params, max(1, int(window)))).fetchall()

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(_cell(row, "case_id")), []).append(row)
    return {cid: _stability_of(items) for cid, items in grouped.items()}


def _stability_of(items: list) -> dict:
    """单个用例的稳定性（items 已按新 -> 旧排好序）。"""
    recent = [str(_cell(r, "status") or "unknown") for r in items]
    counts: dict[str, int] = {}
    for status in recent:
        counts[status] = counts.get(status, 0) + 1

    runs = len(recent)
    passed = counts.get("pass", 0)
    # 靠复跑兜住的批次：状态是 pass，但"要跑两次才过"本身就是不稳定的证据
    retried = sum(1 for r in items
                  if int(_cell(r, "attempts") or 1) > 1 and
                  str(_cell(r, "status")) == "pass")
    chrono = list(reversed(recent))                 # 旧 -> 新，翻转为相邻比较用
    flips = sum(1 for a, b in zip(chrono, chrono[1:]) if a != b)
    streak = {"status": recent[0], "len": 1}
    for status in recent[1:]:
        if status != streak["status"]:
            break
        streak["len"] += 1

    if runs < 2:
        level = LEVEL_UNKNOWN
    elif passed == runs:
        level = LEVEL_FLAKY if retried else LEVEL_STABLE
    elif passed == 0:
        level = LEVEL_BROKEN
    else:
        level = LEVEL_FLAKY

    return {
        "runs": runs,
        "pass": passed,
        "fail": counts.get("fail", 0),
        "blocked": counts.get("blocked", 0),
        "skip": counts.get("skip", 0),
        "other": runs - passed - counts.get("fail", 0)
                 - counts.get("blocked", 0) - counts.get("skip", 0),
        # 通过率分母是**全部样本**：被阻塞/跳过的批次不计入通过，
        # 否则"经常被阻塞"的用例会显示成 100% 稳定。
        "pass_rate": round(passed / runs, 4),
        "recent": recent,
        "flips": flips,
        "retried": retried,          # 窗口内"复跑后才通过"的次数
        "streak": streak,
        "last_status": recent[0],
        "last_run_id": int(_cell(items[0], "run_id") or 0),
        "last_at": str(_cell(items[0], "started_at") or ""),
        "level": level,
    }


# ---------- 跨 run：两次 run 对比 ----------

def compare_runs(conn, base_run_id: int, target_run_id: int) -> dict:
    """用例级对比两次 run（基线 -> 目标），回答"这次改动破坏了什么"。

    判定口径（``ok`` 仅指 ``pass``）：

    - ``new_failures``：基线通过、目标未通过（含 blocked/skip，条目里带状态可自行分辨）；
    - ``fixed``：基线未通过、目标通过；
    - ``still_failing``：两次都未通过，``same_cause`` 表示失败签名是否一致
      （``None`` = 至少一侧没有签名，无法判定）；
    - ``added`` / ``removed``：只在一侧出现的用例（改了方案范围）。
    """
    base_run_id, target_run_id = int(base_run_id), int(target_run_id)
    if base_run_id == target_run_id:
        raise ValueError("基线与目标不能是同一个 run")
    runs = {}
    for run_id in (base_run_id, target_run_id):
        row = conn.execute("SELECT * FROM test_run WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"run 不存在: {run_id}")
        runs[run_id] = row

    base_cases = _exec_map(conn, base_run_id)
    target_cases = _exec_map(conn, target_run_id)

    new_failures, fixed, still_failing = [], [], []
    passed_both = 0
    for case_id in sorted(set(base_cases) & set(target_cases)):
        before, after = base_cases[case_id], target_cases[case_id]
        if before["ok"] and not after["ok"]:
            new_failures.append({
                "case_id": case_id, "base_status": before["status"],
                "target_status": after["status"],
                "signature": after["signature"], "kb_hit_id": after["kb_hit_id"],
            })
        elif not before["ok"] and after["ok"]:
            fixed.append({
                "case_id": case_id, "base_status": before["status"],
                "target_status": after["status"],
                "base_signature": before["signature"],
            })
        elif not before["ok"] and not after["ok"]:
            still_failing.append({
                "case_id": case_id, "base_status": before["status"],
                "target_status": after["status"],
                "base_signature": before["signature"],
                "target_signature": after["signature"],
                "same_cause": _same_cause(before["signature"], after["signature"]),
            })
        else:
            passed_both += 1

    added = [{"case_id": c, "target_status": target_cases[c]["status"]}
             for c in sorted(set(target_cases) - set(base_cases))]
    removed = [{"case_id": c, "base_status": base_cases[c]["status"]}
               for c in sorted(set(base_cases) - set(target_cases))]

    out = {
        "new_failures": new_failures,
        "fixed": fixed,
        "still_failing": still_failing,
        "added": added,
        "removed": removed,
        "counts": {
            "new_failures": len(new_failures), "fixed": len(fixed),
            "still_failing": len(still_failing), "added": len(added),
            "removed": len(removed), "passed_both": passed_both,
        },
    }
    for key, run_id in (("base", base_run_id), ("target", target_run_id)):
        cases = base_cases if key == "base" else target_cases
        out[key] = _run_digest(runs[run_id], cases)
    out["pass_rate_delta"] = _delta(out["base"]["pass_rate"], out["target"]["pass_rate"])
    return out


def _exec_map(conn, run_id: int) -> dict[str, dict]:
    """``case_id -> 该 run 内的执行结果``（同一用例重跑取最后一次，按 id 递增覆盖）。"""
    rows = conn.execute(
        "SELECT case_id, status, failure_signature, kb_hit_id FROM case_exec"
        " WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        status = str(_cell(row, "status") or "unknown")
        out[str(_cell(row, "case_id"))] = {
            "status": status, "ok": status == "pass",
            "signature": _cell(row, "failure_signature"),
            "kb_hit_id": _cell(row, "kb_hit_id"),
        }
    return out


def _run_digest(row, cases: dict) -> dict:
    """对比表头用的 run 摘要（含驱动降级率与被测版本，便于解释差异来源）。"""
    counts: dict[str, int] = {}
    for case in cases.values():
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    total = len(cases)
    stats = _json_or_empty(_cell(row, "stats_json"))
    env = _json_or_empty(_cell(row, "env_snapshot_json"))
    drivers = stats.get("drivers") or {}
    return {
        "id": int(_cell(row, "id")),
        "task_id": _cell(row, "task_id"),
        "status": _cell(row, "status"),
        "started_at": _cell(row, "started_at"),
        "finished_at": _cell(row, "finished_at"),
        "total": total,
        "counts": counts,
        "pass_rate": round(counts.get("pass", 0) / total, 4) if total else None,
        "test_app_version": env.get("test_app_version"),
        "downgrade_rate": drivers.get("downgrade_rate"),
    }


def _same_cause(before, after):
    if not before or not after:
        return None
    return str(before) == str(after)


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(float(after) - float(before), 4)


# ---------- 失败签名历史画像（设计 9.4 快速定位体验） ----------

def signature_history(conn, signature: str) -> dict | None:
    """某失败签名的历史画像：出现次数 / 根因分布 / 最近一次（含失败步骤）/ 建议。

    失败用例报告自动附加"这个错以前出现过几次、都是什么原因、上次是怎么处理的"，
    让定位不必从零开始。数据全来自既有列（``case_exec`` + ``test_run`` + ``step_trace``），
    按需计算不落库（历史会随新 run 变化，落库即失真）。
    """
    sig = str(signature or "").strip()
    if not sig:
        return None
    rows = conn.execute(
        "SELECT c.id, c.run_id, c.case_id, c.zentao_case_id, c.status,"
        " c.root_cause_json, c.started_at, r.started_at AS run_at, r.task_id"
        " FROM case_exec c LEFT JOIN test_run r ON r.id = c.run_id"
        " WHERE c.failure_signature=? ORDER BY c.id", (sig,)).fetchall()
    if not rows:
        return None

    dist: dict[str, int] = {}
    for row in rows:
        rc = _json_or_empty(_cell(row, "root_cause_json"))
        category = str(rc.get("category") or "unknown")
        dist[category] = dist.get(category, 0) + 1

    suggestion = ""
    for row in reversed(rows):          # 新的优先，取最近一次带建议的归因
        rc = _json_or_empty(_cell(row, "root_cause_json"))
        if rc.get("suggestion"):
            suggestion = str(rc["suggestion"])
            break

    last = rows[-1]
    return {
        "signature": sig,
        "count": len(rows),
        "categories": [
            {"category": key, "count": val}
            for key, val in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "last": {
            "run_id": _cell(last, "run_id"),
            "task_id": _cell(last, "task_id"),
            "case_exec_id": _cell(last, "id"),
            "case_id": _cell(last, "case_id"),
            "zentao_case_id": _cell(last, "zentao_case_id"),
            "step_no": _failing_step_no(conn, int(_cell(last, "id"))),
            "at": _cell(last, "run_at") or _cell(last, "started_at"),
        },
        "suggestion": suggestion,
    }


def _failing_step_no(conn, case_exec_id: int) -> int | None:
    """反查该次执行"最像失败点"的步骤号（验证失败 > 报错步骤 > 其他）。"""
    row = conn.execute(
        "SELECT step_no FROM step_trace WHERE case_exec_id=?"
        " ORDER BY CASE"
        "   WHEN verify_result='fail' THEN 0"
        "   WHEN error_json LIKE '%_failed%' OR error_json LIKE '%_error%'"
        "        OR error_json LIKE '%timeout%' THEN 1"
        "   ELSE 2 END, step_no LIMIT 1", (case_exec_id,)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


# ---------- 内部 ----------

def _json_or_empty(raw) -> dict:
    if isinstance(raw, str) and raw:
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return out if isinstance(out, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _chain_of(row) -> list[dict]:
    """取一行轨迹的尝试链；无链时回退 ``driver`` 单元素链。"""
    raw = _cell(row, "driver_chain") or _cell(row, "driver_chain_json")
    driver = _cell(row, "driver")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if isinstance(raw, list):
        chain = [a for a in raw if isinstance(a, dict)]
        if chain:
            return chain
    if driver:
        return [{"driver": str(driver), "ok": None, "ms": 0}]
    return []


def _cell(row, key):
    """兼容 dict 与 sqlite3.Row 的按键取值。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None

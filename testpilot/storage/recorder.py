"""轨迹记录 — step_trace 落库 + 截图三级目录（设计 8.7）。

原则：**写失败不阻断执行**（降级为错误日志）；轨迹字段一律落库，
不做"事后补记"（设计 1.3 第 2 条：轨迹是一切的地基）。
"""
import json
import logging
import sqlite3
import time
import os
import uuid
from pathlib import Path

from testpilot.storage.shotstore import ShotStore

log = logging.getLogger("testpilot.recorder")


class Recorder:
    def __init__(self, conn: sqlite3.Connection, shot_root: Path):
        self.session = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.evidence_errors = 0
        self.conn = conn
        self.shot_root = Path(shot_root)
        self.shots = ShotStore(self.shot_root)

    # ---------- run ----------

    def begin_run(self, env: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO test_run(task_id, started_at, env_snapshot_json, status) "
            "VALUES (?,?,?, 'running')",
            (env["task_id"], _now(), json.dumps(env, ensure_ascii=False)))
        self.conn.execute("UPDATE test_run SET owner_session=? WHERE id=?",
                          (self.session, cur.lastrowid))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute(
            "UPDATE test_run SET status=?, stats_json=?, finished_at=? WHERE id=?",
            (stats.get("status", "completed"),
             json.dumps(stats, ensure_ascii=False), _now(), run_id))
        self.conn.commit()

    # ---------- case ----------

    def begin_case(self, run_id: int, case) -> int:
        cur = self.conn.execute(
            "INSERT INTO case_exec(run_id, case_id, zentao_case_id, dsl_version,"
            " status, started_at) VALUES (?,?,?,?, 'running', ?)",
            (run_id, case.case_id, case.zentao_case_id, case.version, _now()))
        self.conn.execute("UPDATE case_exec SET dsl_snapshot_json=? WHERE id=?",
                          (case.model_dump_json() if hasattr(case, "model_dump_json") else
                           json.dumps({"case_id": case.case_id, "version": case.version}),
                           cur.lastrowid))
        self.conn.commit()
        return cur.lastrowid

    def finish_case(self, case_exec_id: int, status: str, sig, kb_hit,
                    duration_ms: int = 0, root_cause: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE case_exec SET status=?, failure_signature=?, kb_hit_id=?,"
            " duration_ms=?, root_cause_json=? WHERE id=?",
            (status, sig, kb_hit, int(duration_ms or 0),
             json.dumps(root_cause, ensure_ascii=False) if root_cause else None,
             case_exec_id))
        self.conn.commit()

    def record_attempts(self, case_exec_id: int, attempts: list[dict]) -> None:
        """落"每次尝试"的摘要（复跑才写）。

        为什么必须留：用例复跑通过后，``case_exec`` 的最终态是 pass，
        失败签名/归因按既有语义被清空 —— 首次失败的证据就只能从这份摘要里看。
        """
        if not attempts:
            return
        try:
            self.conn.execute(
                "UPDATE case_exec SET attempts=?, retry_json=? WHERE id=?",
                (len(attempts),
                 json.dumps(attempts, ensure_ascii=False) if len(attempts) > 1
                 else None, case_exec_id))
            self.conn.commit()
        except sqlite3.Error as e:
            log.error(f"case_exec 尝试摘要写入失败(不阻断): {e}")

    # ---------- step ----------

    def record_step(self, case_exec_id: int, res, attempt: int = 1) -> None:
        try:
            self.conn.execute(
                "INSERT INTO step_trace(case_exec_id, step_no, attempt, step_id,"
                " status, action, target_json, driver, locate_ms, act_ms, verify_result,"
                " verify_detail, screenshot_path, screenshot_before_path,"
                " device_state_json, receipt_json,"
                " source_text, step_kb_hit, error_json, driver_chain_json, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_exec_id, res.step_no, max(1, int(attempt or 1)),
                 res.step_id, res.status, res.action,
                 json.dumps(res.target_json, ensure_ascii=False)
                 if res.target_json else None,
                 res.driver, res.locate_ms, res.act_ms,
                 (res.verify.status if res.verify else None),
                 (res.verify.detail if res.verify else None),
                 res.screenshot_after or res.screenshot_before,
                 getattr(res, "screenshot_before", None),
                 json.dumps(res.device_state, ensure_ascii=False)
                 if res.device_state is not None else None,
                 json.dumps(getattr(res, "device_receipt", None), ensure_ascii=False)
                 if getattr(res, "device_receipt", None) is not None else None,
                 res.source_text,
                 res.kb_hit,
                 json.dumps(res.error, ensure_ascii=False) if res.error else None,
                 json.dumps(res.driver_chain, ensure_ascii=False)
                 if res.driver_chain else None,
                 _now()))
            self.conn.commit()
        except Exception as e:  # 轨迹写失败不得中断回归（JSON 序列化失败等同样降级）
            self.evidence_errors += 1
            log.error(f"step_trace 写入失败(不阻断): {e}", exc_info=True)

    # ---------- 截图 ----------

    def save_screenshot(self, run_id: int, case_exec_id: int, step_no: int,
                        phase: str, img: bytes, attempt: int = 1) -> str:
        """返回相对路径（库内只存相对路径，设计 3.4）。

        ``attempt`` 参与命名：复跑时同一步骤的截图不再覆盖上一次尝试的取证。
        """
        before = self.shots.errors
        path = self.shots.write(run_id, case_exec_id, step_no, phase, img, attempt=attempt)
        self.evidence_errors += self.shots.errors - before
        return path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

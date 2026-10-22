"""测试方案应用服务 — 本地持久化用例集（设计 3.6 / M1-4）。

方案 = 有序用例列表 + 前置条件汇总 + 方案级驱动配置。
- 前置条件默认由成员用例的 DSL preconditions 汇总（可显式覆盖）；
- 建任务时把方案**展开为快照**写进 test_task，方案后续修改不影响历史任务；
- 创建/更新时做可执行性门禁提示（未 active / 无步骤的用例会被列出，不静默放过）。
"""
import logging
import sqlite3

from testpilot.core.errors import TestPilotError
from testpilot.storage import repos

log = logging.getLogger("testpilot.suites")

VALID_MODES = ("auto", "uia_first", "vision_first", "uia_only", "vision_only")


class SuiteService:
    def __init__(self, conn, cases):
        self.conn = conn
        self.cases = cases      # CaseService（前置条件汇总 + 可执行性门禁）

    def create(self, name: str, case_ids: list[str], description: str = "",
               driver_config: dict | None = None,
               preconditions: list[str] | None = None) -> dict:
        if not case_ids:
            raise TestPilotError("方案至少需要一条用例")
        if repos.get_suite_by_name(self.conn, name) is not None:
            raise TestPilotError(f"方案名已存在: {name}")
        self._validate_driver_config(driver_config)
        pres = preconditions if preconditions is not None \
            else self.cases.preconditions_of(case_ids)
        suite_id = repos.create_suite(self.conn, name=name, case_ids=case_ids,
                                      preconditions=pres,
                                      driver_config=driver_config or {},
                                      description=description)
        log.info(f"方案已创建: #{suite_id} {name} cases={len(case_ids)}")
        return self.get(suite_id)

    def update(self, suite_id: int, *, name: str | None = None,
               case_ids: list[str] | None = None, description: str | None = None,
               driver_config: dict | None = None,
               preconditions: list[str] | None = None) -> dict:
        if self.get(suite_id) is None:
            raise TestPilotError(f"方案不存在: {suite_id}")
        if name is not None:
            other = repos.get_suite_by_name(self.conn, name)
            if other is not None and other["id"] != suite_id:
                raise TestPilotError(f"方案名已存在: {name}")
        self._validate_driver_config(driver_config)
        if case_ids is not None and not case_ids:
            raise TestPilotError("方案至少需要一条用例")
        if preconditions is None and case_ids is not None:
            preconditions = self.cases.preconditions_of(case_ids)
        try:
            repos.update_suite(self.conn, suite_id, name=name, case_ids=case_ids,
                               preconditions=preconditions,
                               driver_config=driver_config,
                               description=description)
        except sqlite3.IntegrityError as e:
            # UNIQUE/FK 违例不该以 INTERNAL_ERROR 形态漏给界面
            raise TestPilotError(f"方案更新失败（名称冲突或约束不满足）: {e}") from e
        log.info(f"方案已更新: #{suite_id}")
        return self.get(suite_id)

    def get(self, suite_id: int) -> dict | None:
        suite = repos.get_suite(self.conn, suite_id)
        if suite is None:
            return None
        suite["not_runnable"] = self.cases.not_runnable(suite["case_ids"])
        return suite

    def get_by_name(self, name: str) -> dict | None:
        suite = repos.get_suite_by_name(self.conn, name)
        if suite is None:
            return None
        suite["not_runnable"] = self.cases.not_runnable(suite["case_ids"])
        return suite

    def resolve(self, ref) -> dict:
        """按 id 或名称解析方案。"""
        suite = None
        if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
            suite = self.get(int(ref))
        else:
            suite = self.get_by_name(str(ref))
        if suite is None:
            raise TestPilotError(f"方案不存在: {ref}")
        return suite

    def list(self) -> list[dict]:
        out = []
        for s in repos.list_suites(self.conn):
            s["not_runnable"] = self.cases.not_runnable(s["case_ids"])
            out.append(s)
        return out

    def delete(self, suite_id: int) -> None:
        if repos.get_suite(self.conn, suite_id) is None:
            raise TestPilotError(f"方案不存在: {suite_id}")
        referenced = self.conn.execute(
            "SELECT COUNT(*) AS n FROM test_task WHERE suite_id=?",
            (suite_id,)).fetchone()["n"]
        if referenced:
            # FK=ON 下直接删会抛裸 sqlite 异常：给出可行动的业务提示
            raise TestPilotError(
                f"方案被 {referenced} 个任务引用，不能删除"
                f"（任务引用的是建任务时的快照，可先确认相关任务不再需要）")
        repos.delete_suite(self.conn, suite_id)
        log.info(f"方案已删除: #{suite_id}")

    def expand(self, suite_id: int) -> dict:
        """展开为建任务所需的快照（用例顺序 / 前置场景 / 驱动配置）。"""
        suite = self.get(suite_id)
        if suite is None:
            raise TestPilotError(f"方案不存在: {suite_id}")
        return {"case_ids": list(suite["case_ids"]),
                "preconditions": list(suite["preconditions"]),
                "driver_config": dict(suite["driver_config"])}

    @staticmethod
    def _validate_driver_config(cfg: dict | None) -> None:
        if not cfg:
            return
        mode = cfg.get("mode")
        if mode is not None and mode not in VALID_MODES:
            raise TestPilotError(f"非法驱动模式: {mode}（可选 {', '.join(VALID_MODES)}）")

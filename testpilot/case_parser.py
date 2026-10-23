"""用例应用服务 — 拉取 / 增量同步 / LLM 解析 / 人审流转（设计 3.1、3.6、M1-1~M1-3）。

生命周期：
    禅道用例 --pull/sync--> 本地原文快照(draft, version=0)
             --parse--> DSL 草稿(steps + uncertain 标记)
             --accept--> active, version+1（执行唯一入口）
             --reject--> rejected（进待改进清单，回馈禅道用例质量）

同步语义（设计 3.6）：
- 远端无变更           -> 仅刷新 last_synced_at
- 远端有变更 & 本地已确认 -> ``sync_status='stale'``（需重新解析 + 人审）
- 远端有变更 & 本地仍是草稿 -> ``sync_status='conflict'``（三版并列比对）

安全门禁：``to_testcase`` 只放行 ``status='active'`` 的用例，
草稿/被拒/待审一律拒绝执行 —— 保证"执行记录钉住已确认版本"。
"""
import json
import hashlib
import logging
from contextlib import nullcontext

from testpilot.core.dsl import TestCase
from testpilot.core.errors import AbortRequested, LlmError, TestPilotError
from testpilot.storage import repos

log = logging.getLogger("testpilot.cases")

VALID_STATUS = ("draft", "active", "rejected", "deprecated")
VALID_SYNC = ("synced", "stale", "conflict")


class CaseService:
    def __init__(self, conn, zentao, llm, cfg: dict | None = None,
                 capabilities_provider=None):
        self.conn = conn
        self.zentao = zentao
        self.llm = llm
        self.cfg = cfg or {}
        # 模拟器能力清单提供者（设计 11.2：白名单注入解析 prompt 防幻觉）
        self.capabilities_provider = capabilities_provider
        self.abort = None
        self.progress = None

    # ---------- 拉取 / 同步（M1-1） ----------

    def pull(self, zentao_ids: list[int], parse: bool = False) -> dict:
        """按禅道用例 ID 拉取入库（幂等：重复拉取只刷新快照）。

        单个 ID 拉不到**整体报错**：显式失败让调用方立刻发现 ID 写错
        （增量 sync 才是"跳过失败继续"的场景）。
        """
        counters = _counters()
        results = []
        for zid in zentao_ids:
            self._checkpoint("pull", zid)
            raw = self.zentao.fetch_case(int(zid))
            action = self._ingest(raw, counters)
            results.append({"case_id": raw.get("case_id"), "action": action})
        out = {**counters, "cases": results}
        if parse:
            ids = [r["case_id"] for r in results if r.get("case_id")]
            out["parse"] = self.parse_many(ids)
        return out

    def sync(self, product_id: int | None = None, parse: bool = False,
             full: bool = False) -> dict:
        """增量同步：优先用本地已知远端时间水位缩小禅道详情请求范围。

        full=True 用于人工校准/排障：忽略水位，完整扫描远端列表。默认模式仍会在
        ingest 时逐条比较 updated_at，因此即使上游 since 只是候选过滤也不会误覆盖。
        """
        counters = _counters()
        pid = product_id or (self.cfg.get("zentao") or {}).get("product_id")
        since = None if full else self._sync_watermark(pid)
        raws = self.zentao.fetch_cases(product_id, since=since)
        for raw in raws:
            self._checkpoint("sync", raw.get("case_id"))
            action = self._ingest(raw, counters)
            if action != "unchanged":
                log.info(f"用例同步 {raw.get('case_id')}: {action}")
        out = {**counters, "scanned": len(raws), "since": since, "full": bool(full)}
        if parse:
            # rejected 用例按设计不再自动解析（人审已否决）：留在清单里只会
            # 每次 sync --parse 都产出注定失败的条目
            stale_ids = [r["case_id"] for r in self.list_cases()
                         if (r["sync_status"] in ("stale", "conflict") or not r.get("steps"))
                         and r["status"] != "rejected"]
            out["parse"] = self.parse_many(stale_ids) if stale_ids else {}
        return out

    def _sync_watermark(self, product_id: int | None) -> str | None:
        """返回当前产品已入库用例的最大远端更新时间。

        水位按 raw.product 隔离；不同产品不能共用 MAX(zentao_updated_at)，否则切换产品
        时可能把该产品较早创建的用例全部跳过。
        """
        latest = ""
        for row in repos.list_cases(self.conn):
            raw = row.get("raw") or {}
            if product_id is not None:
                try:
                    if int(raw.get("product")) != int(product_id):
                        continue
                except (TypeError, ValueError):
                    continue
            stamp = str(row.get("zentao_updated_at") or "")
            if stamp > latest:
                latest = stamp
        return latest or None

    def _ingest(self, raw: dict, counters: dict) -> str:
        """把一条禅道原文并入本地，返回动作名。"""
        cid = raw.get("case_id")
        if not cid:
            counters["skipped"] += 1
            return "skipped"
        local = repos.get_case(self.conn, cid)
        if local is None:
            repos.create_case(
                self.conn, case_id=cid, zentao_case_id=raw.get("zentao_case_id"),
                title=raw.get("title", ""), raw=raw,
                zentao_updated_at=raw.get("updated_at"),
                module_id=_as_int(raw.get("module")),
                module_name=raw.get("module_name"))
            counters["new"] += 1
            return "new"

        remote_ts = str(raw.get("updated_at") or "")
        local_ts = str(local.get("zentao_updated_at") or "")
        # 远端时间戳缺失时**视为未变更**：拿"空 <= 任何值"去比较恒不成立，
        # 会让每次 sync 都把用例误标 stale/conflict，反复触发重解析打断人审闭环
        if not remote_ts or (local_ts and remote_ts <= local_ts):
            repos.touch_case_sync(self.conn, cid)
            counters["unchanged"] += 1
            return "unchanged"

        sync_status = "conflict" if local["status"] == "draft" else "stale"
        repos.update_case_raw(self.conn, cid, raw=raw,
                              title=raw.get("title"), zentao_updated_at=remote_ts,
                              sync_status=sync_status,
                              module_id=_as_int(raw.get("module")),
                              module_name=raw.get("module_name"))
        counters[sync_status] += 1
        return sync_status

    # ---------- 查询 ----------

    def list_cases(self, status: str | None = None,
                   sync_status: str | None = None) -> list[dict]:
        if status is not None and status not in VALID_STATUS:
            raise TestPilotError(f"非法用例状态: {status}"
                                 f"（可选 {', '.join(VALID_STATUS)}）")
        if sync_status is not None and sync_status not in VALID_SYNC:
            raise TestPilotError(f"非法同步状态: {sync_status}"
                                 f"（可选 {', '.join(VALID_SYNC)}）")
        return repos.list_cases(self.conn, status=status, sync_status=sync_status)

    def show(self, case_id: str) -> dict:
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        return self._review_view(row)

    def _review_view(self, row: dict) -> dict:
        """人审视图：DSL 草稿 + 禅道原文对照 + 不确定标记统计。

        `accepted_steps` 是**上次人审通过时冻结的步骤快照**。设计 3.6 要求
        `sync_status=conflict` 时并列显示三版（禅道最新原文 / 本地草稿 / 本地已确认版本）
        —— 少了它只能看到两版，而 conflict 的整个意义就在于三版比对。
        """
        raw = row.get("raw") or {}
        steps = row.get("steps") or []
        uncertain = [s.get("id") for s in steps if s.get("uncertain")]
        return {
            "case_id": row["case_id"],
            "zentao_case_id": row.get("zentao_case_id"),
            "title": row.get("title"),
            "version": row.get("version"),
            "status": row.get("status"),
            "sync_status": row.get("sync_status"),
            "preconditions": row.get("preconditions") or [],
            "notes": row.get("notes") or "",
            "updated_at": row.get("updated_at"),
            "zentao_updated_at": row.get("zentao_updated_at"),
            "source_text": raw.get("steps_text", ""),
            "steps": steps,
            # 上一次人审通过的步骤（version=0 时为 None）；供三版对照
            "revision": hashlib.sha256(
                json.dumps({k: row.get(k) for k in
                    ("steps", "preconditions", "status", "version", "sync_status",
                     "notes", "zentao_updated_at")}, sort_keys=True).encode()).hexdigest(),
            "accepted_steps": row.get("accepted_steps"),
            "uncertain_steps": uncertain,
            "step_count": len(steps),
        }

    # ---------- LLM 解析（M1-2） ----------

    def parse(self, case_id: str, app_target: dict | None = None) -> dict:
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        if row["status"] == "rejected":
            raise TestPilotError(f"用例 {case_id} 已被人审拒绝，请先重新拉取或导入")

        payload = self._parse_payload(row)
        if app_target:
            payload["application"] = dict(app_target)
        if not payload["steps_text"]:
            raise TestPilotError(
                f"用例 {case_id} 缺少禅道原文步骤，无法解析（先执行 case pull）")
        self._checkpoint("parse", case_id)
        revision = self._review_view(row)["revision"]
        self._inject_capabilities()
        scope = getattr(self.llm, "request_context", None)
        with scope(abort=self.abort) if scope else nullcontext():
            draft = self.llm.parse_case(payload)
        self._checkpoint("parse", case_id)
        original_pre = payload["preconditions"]
        draft["preconditions"] = list(dict.fromkeys(
            [*original_pre, *(draft.get("preconditions") or [])]))
        steps = draft.get("steps") or []
        if app_target and steps:
            launch = next((s for s in steps if s.get("action") == "app.launch"), None)
            if launch is not None:
                launch["args"] = dict(app_target)
            else:
                ids = {s.get("id") for s in steps}
                sid = "workflow_launch"
                while sid in ids:
                    sid += "_1"
                steps.insert(0, {"id": sid, "action": "app.launch", "args": dict(app_target),
                                 "source_text": "按本次测试设置启动或附加被测软件"})
        if not steps:
            raise LlmError(f"解析结果不含任何步骤: case={case_id}")

        # 重解析消费了当前远端原文，stale/conflict 语义已兑现：重置为 synced，
        # 否则用例会永远卡在 stale 而无法 accept（人审闭环断路）
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.show(case_id)["revision"] != revision:
                raise TestPilotError(f"用例 {case_id} 在解析期间已变更，请重新解析")
            repos.save_case_steps(
                self.conn, case_id, steps=steps, status="draft", sync_status="synced",
                preconditions=draft.get("preconditions") or [],
                notes=draft.get("notes") or "")
        log.info(f"用例解析完成: {case_id} steps={len(steps)} "
                 f"uncertain={draft.get('uncertain_count', 0)}")
        return {
            "case_id": case_id,
            "step_count": len(steps),
            "uncertain_steps": [s.get("id") for s in steps if s.get("uncertain")],
            "notes": draft.get("notes", ""),
            "prompt_version": draft.get("prompt_version"),
        }

    def parse_many(self, case_ids: list[str] | None = None,
                   only_stale: bool = True, app_target: dict | None = None) -> dict:
        """逐条生成草稿；包含初次拉取的空 DSL，失败隔离，取消保留已完成结果。"""
        if case_ids is None:
            case_ids = [r["case_id"] for r in self.list_cases()
                        if r["status"] != "rejected" and (not only_stale
                        or r["sync_status"] in ("stale", "conflict") or not r.get("steps"))]
        if not isinstance(case_ids, list) or len(case_ids) > 500:
            raise TestPilotError("caseIds 必须为列表，每批最多 500 条")
        if any(not isinstance(cid, str) or not cid.strip() for cid in case_ids):
            raise TestPilotError("caseIds 必须包含非空字符串")
        case_ids = list(dict.fromkeys(case_ids))
        done, failed = [], []

        def report(cid=None):
            result = {"parsed": len(done), "failed": failed, "items": done,
                      "total": len(case_ids)}
            if self.progress:
                self.progress({"phase": "parse", "case_id": cid, "total": len(case_ids),
                               "completed": len(done) + len(failed), "result": result})
            return result

        report()
        for cid in case_ids:
            self._checkpoint("parse", cid)
            try:
                done.append(self.parse(cid, app_target=app_target))
            except AbortRequested:
                raise
            except Exception as exc:
                failed.append({"case_id": cid, "error": str(exc)})
                log.warning(f"解析失败: {cid} {exc}")
            report(cid)
        return report()

    def _parse_payload(self, row: dict) -> dict:
        raw = row.get("raw") or {}
        return {
            "case_id": row["case_id"],
            "zentao_case_id": row.get("zentao_case_id"),
            "title": row.get("title", ""),
            "steps_text": raw.get("steps_text", ""),
            "steps": raw.get("steps") or [],
            "preconditions": list(dict.fromkeys([*(raw.get("preconditions") or []),
                                                *(row.get("preconditions") or [])])),
            "product": raw.get("product"),
        }

    def _checkpoint(self, phase, case_id=None) -> None:
        if self.abort is not None:
            self.abort.check()
        if self.progress:
            self.progress({"phase": phase, "case_id": case_id})

    def _inject_capabilities(self) -> None:
        self.llm.emulator_capabilities = None
        if not self.capabilities_provider:
            return
        try:
            caps = self.capabilities_provider()
        except Exception as e:      # 模拟器未起不影响纯 UI 用例解析
            log.info(f"模拟器能力清单不可用（跳过白名单注入）: {e}")
            return
        if caps:
            self.llm.emulator_capabilities = caps

    # ---------- 人审（M1-3） ----------

    def export_case(self, case_id: str, fmt: str = "json") -> str:
        """导出 DSL（json/yaml），供人工编辑后再导入。"""
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        data = {
            "case_id": row["case_id"],
            "zentao_case_id": row.get("zentao_case_id"),
            "title": row.get("title", ""),
            "version": row.get("version", 0),
            "preconditions": row.get("preconditions") or [],
            "notes": row.get("notes") or "",
            "steps": row.get("steps") or [],
        }
        return dumps(data, fmt)

    def import_draft(self, text: str, fmt: str = "json",
                     case_id: str | None = None) -> dict:
        """导入人工编辑后的 DSL 草稿；结构与校验由 pydantic 兜底。"""
        data = loads(text, fmt)
        if not isinstance(data, dict):
            raise TestPilotError("DSL 文件顶层必须是对象")
        if case_id:
            data["case_id"] = case_id
        case: TestCase = TestCase.model_validate(data)

        if repos.get_case(self.conn, case.case_id) is None:
            repos.create_case(self.conn, case_id=case.case_id,
                              zentao_case_id=case.zentao_case_id,
                              title=case.title, raw=None, zentao_updated_at=None)
        repos.save_case_steps(
            self.conn, case.case_id,
            steps=[s.model_dump(mode="json") for s in case.steps],
            status="draft", preconditions=list(case.preconditions),
            notes=case.notes, title=case.title)
        log.info(f"DSL 草稿已导入: {case.case_id} steps={len(case.steps)}")
        return {"case_id": case.case_id, "step_count": len(case.steps),
                "uncertain_steps": [s.id for s in case.steps if s.uncertain]}

    def accept(self, case_id: str) -> dict:
        """人审通过：draft/rejected -> active，版本 +1（首个生效版本为 1）。"""
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        if not (row.get("steps") or []):
            raise TestPilotError(f"用例 {case_id} 尚无 DSL 步骤，无法激活（先 parse/import）")
        sync_status = row.get("sync_status")
        if sync_status in ("stale", "conflict"):
            # 冻结的 accepted 步骤仍是旧版本：直接 accept 会把远端变更静默"确认掉"，
            # 且 stale 标记被清除后三版比对入口永久丢失
            raise TestPilotError(
                f"用例 {case_id} 远端有未处理变更（sync_status={sync_status}）："
                f"先 case parse 重解析出新草稿，再对人审通过")
        if row.get("status") == "active":
            # 已生效且无新草稿（parse/import 都会把状态打回 draft）：重复 accept
            # 不该再涨版本号，否则 version 语义（人审一次 +1）被空转调用破坏
            raise TestPilotError(
                f"用例 {case_id} 已是生效版本，无待审草稿"
                f"（先 parse/import 产出新草稿再人审）")
        from testpilot.core.dsl import validate_execution
        try:
            validate_execution(TestCase.model_validate({
                "case_id": case_id, "steps": row["steps"]}))
        except ValueError as e:
            raise TestPilotError(f"DSL 不可执行，请修正后审核: {e}") from e
        version = repos.activate_case(self.conn, case_id)
        uncertain = [s.get("id") for s in row["steps"] if s.get("uncertain")]
        log.info(f"用例人审通过: {case_id} -> active v{version}"
                 f"（uncertain={len(uncertain)}）")
        return {"case_id": case_id, "version": version, "status": "active",
                "uncertain_steps": uncertain}

    def reject(self, case_id: str) -> dict:
        """人审拒绝：进入待改进清单（回馈禅道用例质量），原文留档。"""
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        repos.reject_case(self.conn, case_id)
        log.info(f"用例人审拒绝: {case_id} -> rejected")
        return {"case_id": case_id, "status": "rejected"}

    # ---------- 供执行层使用 ----------

    def to_testcase(self, case_id: str) -> TestCase:
        """DSL -> pydantic 用例；只放行 active（执行记录钉住已确认版本）。"""
        row = repos.get_case(self.conn, case_id)
        if row is None:
            raise TestPilotError(f"本地不存在该用例: {case_id}")
        if row["status"] != "active":
            raise TestPilotError(
                f"用例 {case_id} 当前状态 {row['status']}，仅 active 可执行"
                f"（执行人审：case accept {case_id}）")
        return TestCase.model_validate({
            "case_id": row["case_id"],
            "zentao_case_id": row.get("zentao_case_id"),
            "title": row.get("title", ""),
            "version": row.get("version", 1),
            "preconditions": row.get("preconditions") or [],
            "steps": row.get("steps") or [],
            "notes": row.get("notes") or "",
        })

    def preconditions_of(self, case_ids: list[str]) -> list[str]:
        """汇总用例集的前置条件（方案 preconditions 的来源，去重保序）。"""
        out: list[str] = []
        for cid in case_ids:
            row = repos.get_case(self.conn, cid)
            for p in (row or {}).get("preconditions") or []:
                if p not in out:
                    out.append(p)
        return out

    def not_runnable(self, case_ids: list[str]) -> list[dict]:
        """返回不可执行的用例清单（方案/任务创建前的门禁提示）。"""
        bad = []
        for cid in case_ids:
            row = repos.get_case(self.conn, cid)
            if row is None:
                bad.append({"case_id": cid, "reason": "本地不存在"})
            elif row["status"] != "active":
                bad.append({"case_id": cid, "reason": f"状态 {row['status']}"})
            elif not (row.get("steps") or []):
                bad.append({"case_id": cid, "reason": "无 DSL 步骤"})
        return bad


# ---------- 序列化工具 ----------

def dumps(data: dict, fmt: str = "json") -> str:
    if fmt == "yaml":
        import yaml
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json.dumps(data, ensure_ascii=False, indent=2)


def loads(text: str, fmt: str = "json"):
    if fmt == "yaml":
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _counters() -> dict:
    return {"new": 0, "unchanged": 0, "stale": 0, "conflict": 0, "skipped": 0,
            "failed": 0}

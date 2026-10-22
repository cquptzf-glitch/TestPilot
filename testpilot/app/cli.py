"""CLI 入口 — TestPilot 命令行（设计 2.3；M1 完整命令链）。

命令分组：
    db-init / doctor                 初始化与自检
    case  pull|sync|list|show|parse|export|import|accept|reject
    suite list|show|create|update|delete
    task  list|show|create|run|sync-pending
    run   list|show|report|compare|archive（run 查询 / 报告 / 两次对比 / 归档）
    kb    pending|confirm|reject|ctx-list|ctx-set|ctx-rm|env-matrix
    explore mcp|export|import        M5+ 探索：MCP server / 会话固化回 DSL
    serve                            壳形态（JSON-RPC over WebSocket）

约定：所有命令输出 JSON（``--pretty`` 美化），错误统一打到 stderr 并返回非 0。
"""
import argparse
import json
import logging
import sys

from testpilot import __version__

log = logging.getLogger("testpilot.cli")


# ---------- 装配 ----------

def build_registry():
    """注册内置驱动 manifest（扩驱动只改这里，不改引擎）。"""
    from testpilot.core.registry import DriverRegistry
    from testpilot.drivers.emulator_adapter import MANIFEST as EMU
    from testpilot.drivers.uia_driver import MANIFEST as UIA
    from testpilot.drivers.vision_driver import MANIFEST as VIS

    reg = DriverRegistry()
    for manifest in (UIA, VIS, EMU):
        reg.register(manifest)
    return reg


def build_orchestrator(cfg: dict):
    from testpilot.engine.orchestrator import TaskOrchestrator
    return TaskOrchestrator(cfg, build_registry())


def _connect(cfg: dict):
    from testpilot.storage.db import connect
    return connect(cfg["app"]["db"])


# ---------- 输出 ----------

def emit(data, pretty: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False,
                      indent=2 if pretty else None, default=str)
    print(text)


def _ids(text: str) -> list[int]:
    return [int(p) for p in str(text).replace("，", ",").split(",") if p.strip()]


def _case_ids(text: str) -> list[str]:
    return [p.strip() for p in str(text).replace("，", ",").split(",") if p.strip()]


# ---------- 各命令实现 ----------

def cmd_db_init(cfg, args) -> int:
    conn = _connect(cfg)
    tables = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    emit({"db": cfg["app"]["db"], "tables": tables}, args.pretty)
    conn.close()
    return 0


def cmd_doctor(cfg, args) -> int:
    orch = build_orchestrator(cfg)
    try:
        emit(orch.health(), args.pretty)
    finally:
        orch.close()
    return 0


# ---- case ----

def cmd_case(cfg, args) -> int:
    orch = build_orchestrator(cfg)
    try:
        cases = orch.cases
        if args.action == "pull":
            emit(cases.pull(_ids(args.ids), parse=args.parse), args.pretty)
        elif args.action == "sync":
            emit(cases.sync(args.product, parse=args.parse), args.pretty)
        elif args.action == "list":
            rows = cases.list_cases(status=args.status,
                                    sync_status=args.sync_status)
            from testpilot.storage.repos import count_cases_by_sync
            emit({"count": len(rows),
                  "sync_summary": count_cases_by_sync(orch.conn),
                  "cases": [{"case_id": r["case_id"], "title": r["title"],
                             "status": r["status"], "sync_status": r["sync_status"],
                             "version": r["version"],
                             "steps": len(r.get("steps") or []),
                             "zentao_case_id": r["zentao_case_id"]}
                            for r in rows]}, args.pretty)
        elif args.action == "show":
            emit(cases.show(args.case_id), args.pretty)
        elif args.action == "parse":
            if args.all_stale:
                emit(cases.parse_many(None, only_stale=True), args.pretty)
            elif args.case_id:
                emit(cases.parse(args.case_id), args.pretty)
            else:
                raise SystemExit("case parse 需要 <case_id> 或 --all-stale")
        elif args.action == "export":
            text = cases.export_case(args.case_id, args.format)
            if args.out:
                from pathlib import Path
                Path(args.out).write_text(text, encoding="utf-8")
                emit({"written": args.out}, args.pretty)
            else:
                print(text)
        elif args.action == "import":
            from pathlib import Path
            path = Path(args.file)
            fmt = args.format or ("yaml" if path.suffix in (".yaml", ".yml") else "json")
            emit(cases.import_draft(path.read_text(encoding="utf-8"), fmt,
                                    case_id=args.case_id), args.pretty)
        elif args.action == "accept":
            emit(cases.accept(args.case_id), args.pretty)
        elif args.action == "reject":
            emit(cases.reject(args.case_id), args.pretty)
        else:  # pragma: no cover - argparse 已限制
            raise SystemExit(f"未知 case 动作: {args.action}")
    finally:
        orch.close()
    return 0


# ---- suite ----

def cmd_suite(cfg, args) -> int:
    orch = build_orchestrator(cfg)
    try:
        suites = orch.suites
        if args.action == "list":
            emit({"suites": suites.list()}, args.pretty)
        elif args.action == "show":
            emit(suites.resolve(args.suite), args.pretty)
        elif args.action == "create":
            emit(suites.create(
                name=args.name, case_ids=_case_ids(args.cases),
                description=args.description or "",
                driver_config=_kv(args.driver) if args.driver else None,
                preconditions=_case_ids(args.preconditions)
                if args.preconditions else None), args.pretty)
        elif args.action == "update":
            emit(suites.update(
                int(args.suite),
                name=args.name,
                case_ids=_case_ids(args.cases) if args.cases else None,
                description=args.description,
                driver_config=_kv(args.driver) if args.driver else None,
                preconditions=_case_ids(args.preconditions)
                if args.preconditions else None), args.pretty)
        elif args.action == "delete":
            suites.delete(int(args.suite))
            emit({"deleted": int(args.suite)}, args.pretty)
    finally:
        orch.close()
    return 0


# ---- task / run ----

def cmd_task(cfg, args) -> int:
    orch = build_orchestrator(cfg)
    try:
        if args.action == "list":
            emit({"tasks": orch.list_tasks()}, args.pretty)
        elif args.action == "show":
            emit(orch.task_detail(int(args.task)), args.pretty)
        elif args.action == "create":
            emit(orch.create_task({
                "name": args.name,
                "suite_id": int(args.suite) if args.suite else None,
                "case_ids": _case_ids(args.cases) if args.cases else [],
                "preconditions": _case_ids(args.preconditions)
                if args.preconditions else None,
                "driver_config": _kv(args.driver) if args.driver else None,
                "force": args.force,
                # 不带 --remote 传 None：由配置 zentao.create_task 决定是否走禅道
                "remote": True if args.remote else None,
                "product_id": args.product,
            }), args.pretty)
        elif args.action == "run":
            run_cfg = {}
            if args.mode:
                run_cfg["mode"] = args.mode
            if getattr(args, "retry", None) is not None:
                run_cfg["case_retry"] = args.retry
            stats = orch.run_task(int(args.task), run_cfg or None,
                                  writeback=not args.no_writeback)
            emit(stats, args.pretty)
            if stats.get("status") != "completed" or any(
                    stats.get(key, 0) for key in ("fail", "error", "blocked")):
                return 1
        elif args.action == "sync-pending":
            emit({"results": orch.sync_pending_tasks()}, args.pretty)
    finally:
        orch.close()
    return 0


def cmd_run(cfg, args) -> int:
    orch = build_orchestrator(cfg)
    try:
        if args.action == "list":
            emit({"runs": repos_list(orch.conn, args.limit)}, args.pretty)
        elif args.action == "show":
            detail = repos_run_detail(orch.conn, int(args.run_id))
            if detail is not None:
                # 与 RPC run.detail 同口径：稳定性与签名历史都是按需计算的派生视图
                from testpilot.engine.stats import case_stability, signature_history

                detail["stability"] = case_stability(
                    orch.conn, [c["case_id"] for c in detail["cases"]])
                for case in detail["cases"]:
                    if case.get("failure_signature"):
                        case["history"] = signature_history(
                            orch.conn, case["failure_signature"])
            emit(detail, args.pretty)
        elif args.action == "report":
            path = orch_report(orch, int(args.run_id))
            emit({"report": str(path)}, args.pretty)
        elif args.action == "compare":
            from testpilot.engine.stats import compare_runs

            emit(compare_runs(orch.conn, int(args.base), int(args.target)),
                 args.pretty)
        elif args.action == "archive":
            from pathlib import Path

            from testpilot.storage.repos import archive_runs

            out_dir = args.out_dir or str(Path(cfg["app"]["db"]).resolve().parent / "archive")
            out = archive_runs(orch.conn, before=args.before,
                               out_dir=out_dir, delete=args.delete,
                               resource_roots={"shots": cfg["app"]["screenshot_root"],
                                               "reports": cfg["app"]["report_root"]})
            emit(out, args.pretty)
    finally:
        orch.close()
    return 0


def repos_list(conn, limit: int):
    from testpilot.storage.repos import list_runs
    return list_runs(conn, limit)


def repos_run_detail(conn, run_id: int):
    from testpilot.storage.repos import run_detail
    return run_detail(conn, run_id)


def orch_report(orch, run_id: int):
    from testpilot.report.reporter import Reporter
    return Reporter(orch.conn, orch.cfg).generate(run_id)


# ---- kb ----

def cmd_kb(cfg, args) -> int:
    from testpilot.knowledge.service import KnowledgeService
    from testpilot.storage.repos import kb_pending

    conn = _connect(cfg)
    try:
        if args.action == "pending":
            rows = kb_pending(conn)
            emit({"count": len(rows), "entries": rows}, args.pretty)
        elif args.action == "ctx-list":
            rows = KnowledgeService(conn, cfg["knowledge"]).list_case_ctx(
                args.case)
            emit({"count": len(rows), "entries": rows}, args.pretty)
        elif args.action == "ctx-set":
            payload = {}
            if args.scene is not None:
                payload["scene"] = args.scene
            if args.pitfalls is not None:
                payload["pitfalls"] = args.pitfalls
            if args.notes is not None:
                payload["notes"] = args.notes
            emit(KnowledgeService(conn, cfg["knowledge"]).save_case_ctx(
                args.case, payload), args.pretty)
        elif args.action == "ctx-rm":
            emit(KnowledgeService(conn, cfg["knowledge"]).delete_case_ctx(
                args.case), args.pretty)
        elif args.action == "env-matrix":
            rows = KnowledgeService(conn, cfg["knowledge"]).env_compat_matrix(
                test_app_version=args.version, suite_id=args.suite,
                limit=args.limit)
            emit({"count": len(rows), "entries": rows}, args.pretty)
        else:
            verdict = "active" if args.action == "confirm" else "rejected"
            KnowledgeService(conn, cfg["knowledge"]).confirm(int(args.entry), verdict)
            emit({"entry": int(args.entry), "status": verdict}, args.pretty)
    finally:
        conn.close()
    return 0


# ---- explore（M5+ 探索：固化回 DSL / MCP server） ----

def cmd_explore(cfg, args) -> int:
    from pathlib import Path

    from testpilot.explore.session import ExplorationSession

    if args.action == "mcp":
        from testpilot.explore import mcp_server
        return mcp_server.build_server(cfg).serve_stdio(sys.stdin, sys.stdout)

    path = Path(args.file)
    session = ExplorationSession.from_dict(
        json.loads(path.read_text(encoding="utf-8")))
    if getattr(args, "case_id", None):
        session.case_id = args.case_id

    if args.action == "export":
        text = session.to_draft_text(args.format)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            emit({"written": args.out,
                  "steps": len(session.to_case()["steps"])}, args.pretty)
        else:
            print(text)
        return 0

    orch = build_orchestrator(cfg)
    try:
        emit(orch.cases.import_draft(session.to_draft_text("json"), "json"),
             args.pretty)
    finally:
        orch.close()
    return 0


# ---- serve ----

def cmd_serve(cfg, args) -> int:
    try:
        import asyncio

        from testpilot.rpc import RpcService
    except ImportError:
        print("serve 需要 rpc 可选依赖: pip install testpilot[rpc]", file=sys.stderr)
        return 2

    orch = build_orchestrator(cfg)
    from testpilot.storage.db import recover_orphaned_runs
    recover_orphaned_runs(orch.conn)
    svc = RpcService(orch)
    try:
        asyncio.run(svc.serve(cfg["rpc"]["host"], cfg["rpc"]["port"]))
    except KeyboardInterrupt:      # Ctrl-C 正常退出
        log.info("RPC 服务收到中断信号")
        orch.abort()               # 让执行中的 run 在最近取消点安全收尾
        svc.drain()                # 排空后再关库连接，否则收尾 SQL 全部撞 "closed database"
    finally:
        svc.close()
        orch.close()
    return 0


# ---------- 参数解析 ----------

def _kv(text: str) -> dict:
    """``--driver mode=uia_only,retry=true`` -> {"mode": "uia_only", ...}。"""
    out = {}
    for part in str(text).replace("，", ",").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SystemExit(f"--driver 需要 key=value 形式: {part!r}")
        k, v = part.split("=", 1)
        out[k.strip()] = _coerce(v.strip())
    return out


def _coerce(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testpilot",
                                description="TestPilot AI 自动化测试平台")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="配置文件路径（默认打包内 default.yaml）")
    p.add_argument("--pretty", action="store_true", help="JSON 美化输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("db-init", help="初始化数据库（建表 + 迁移）")
    sub.add_parser("doctor", help="引擎自检（DB/驱动/禅道/模拟器）")

    # case
    c = sub.add_parser("case", help="用例管理：拉取/同步/解析/人审")
    cs = c.add_subparsers(dest="action", required=True)
    csp = cs.add_parser("pull", help="按禅道用例 ID 拉取入库")
    csp.add_argument("--ids", required=True, help="禅道用例 ID，逗号分隔")
    csp.add_argument("--parse", action="store_true", help="拉取后立即 LLM 解析")
    csy = cs.add_parser("sync", help="按产品增量同步（变更标 stale/conflict）")
    csy.add_argument("--product", type=int, default=None, help="产品 ID")
    csy.add_argument("--parse", action="store_true", help="同步后重解析 stale 用例")
    cl = cs.add_parser("list", help="本地用例列表")
    cl.add_argument("--status", choices=["draft", "active", "rejected", "deprecated"])
    cl.add_argument("--sync-status", dest="sync_status",
                    choices=["synced", "stale", "conflict"])
    csh = cs.add_parser("show", help="用例详情（DSL + 禅道原文对照 + 不确定标记）")
    csh.add_argument("case_id")
    cp = cs.add_parser("parse", help="LLM 解析为 DSL 草稿")
    cp.add_argument("case_id", nargs="?", default=None)
    cp.add_argument("--all-stale", action="store_true", dest="all_stale",
                    help="批量重解析全部 stale/conflict 用例")
    ce = cs.add_parser("export", help="导出 DSL（json/yaml）")
    ce.add_argument("case_id")
    ce.add_argument("--format", choices=["json", "yaml"], default="json")
    ce.add_argument("--out", help="输出文件路径（缺省打印到 stdout）")
    ci = cs.add_parser("import", help="导入人工编辑后的 DSL 草稿")
    ci.add_argument("--file", required=True)
    ci.add_argument("--format", choices=["json", "yaml"], default=None)
    ci.add_argument("--case-id", dest="case_id", default=None,
                    help="覆盖文件中的 case_id")
    ca = cs.add_parser("accept", help="人审通过（draft -> active，版本 +1）")
    ca.add_argument("case_id")
    cr = cs.add_parser("reject", help="人审拒绝（进待改进清单）")
    cr.add_argument("case_id")

    # suite
    s = sub.add_parser("suite", help="测试方案管理")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list", help="方案列表")
    ssh = ss.add_parser("show", help="方案详情（id 或名称）")
    ssh.add_argument("suite")
    sc = ss.add_parser("create", help="创建方案")
    sc.add_argument("--name", required=True)
    sc.add_argument("--cases", required=True, help="用例 ID，逗号分隔，有序")
    sc.add_argument("--description", default="")
    sc.add_argument("--driver", default=None, help="方案级驱动配置 key=value,...")
    sc.add_argument("--preconditions", default=None,
                    help="显式前置条件（缺省由成员用例汇总）")
    su = ss.add_parser("update", help="更新方案")
    su.add_argument("suite")
    su.add_argument("--name", default=None)
    su.add_argument("--cases", default=None)
    su.add_argument("--description", default=None)
    su.add_argument("--driver", default=None)
    su.add_argument("--preconditions", default=None)
    sd = ss.add_parser("delete", help="删除方案")
    sd.add_argument("suite")

    # task
    t = sub.add_parser("task", help="任务创建与执行")
    ts = t.add_subparsers(dest="action", required=True)
    ts.add_parser("list", help="任务列表")
    tsh = ts.add_parser("show", help="任务详情（含 run 历史）")
    tsh.add_argument("task")
    tc = ts.add_parser("create", help="建任务（本地优先；--remote 走禅道）")
    tc.add_argument("--name", required=True)
    tc.add_argument("--suite", default=None, help="方案 ID（与 --cases 二选一）")
    tc.add_argument("--cases", default=None, help="临时选用例 ID，逗号分隔")
    tc.add_argument("--preconditions", default=None)
    tc.add_argument("--driver", default=None, help="驱动配置 key=value,...")
    tc.add_argument("--product", type=int, default=None, help="禅道产品 ID")
    tc.add_argument("--remote", action="store_true", help="在禅道创建测试任务")
    tc.add_argument("--force", action="store_true",
                    help="存在不可执行用例时仍建任务（默认拒绝）")
    tr = ts.add_parser("run", help="执行任务")
    tr.add_argument("task")
    tr.add_argument("--mode", default=None, help="覆盖驱动模式")
    tr.add_argument("--retry", type=int, default=None,
                    help="失败用例复跑次数（0=关闭，上限 3；过滤环境抖动）")
    tr.add_argument("--no-writeback", action="store_true", dest="no_writeback",
                    help="跳过禅道汇总回写")
    ts.add_parser("sync-pending", help="重推 sync_pending 任务汇总（补偿）")

    # run
    r = sub.add_parser("run", help="run 查询与报告")
    rs = r.add_subparsers(dest="action", required=True)
    rl = rs.add_parser("list", help="run 列表")
    rl.add_argument("--limit", type=int, default=50)
    rsh = rs.add_parser("show", help="run 详情（用例/步骤/归因）")
    rsh.add_argument("run_id")
    rr = rs.add_parser("report", help="生成 run 报告（Markdown）")
    rr.add_argument("run_id")
    rc = rs.add_parser("compare", help="两次 run 对比（新增失败/修复/持续失败）")
    rc.add_argument("--base", type=int, required=True, help="基线 run ID（较旧）")
    rc.add_argument("--target", type=int, required=True, help="目标 run ID（较新）")
    ra = rs.add_parser("archive", help="归档截止时间之前的 run（默认只导出，不删除）")
    ra.add_argument("--before", required=True,
                    help="ISO-8601 截止时间，例如 2026-01-01T00:00:00")
    ra.add_argument("--out-dir", default=None,
                    help="归档输出目录（缺省为 data/archive）")
    ra.add_argument("--delete", action="store_true",
                    help="导出成功后从在线库删除对应 run（危险操作）")

    # kb
    k = sub.add_parser("kb", help="知识库人审队列 / 用例上下文 / 环境矩阵")
    ks = k.add_subparsers(dest="action", required=True)
    ks.add_parser("pending", help="待审条目")
    kc = ks.add_parser("confirm", help="通过并生效")
    kc.add_argument("entry")
    kr = ks.add_parser("reject", help="拒绝（留档不删）")
    kr.add_argument("entry")
    kctxl = ks.add_parser("ctx-list", help="用例上下文列表（人审维护的 case_ctx）")
    kctxl.add_argument("--case", default=None, help="按用例 ID 过滤（本地或禅道 ID）")
    kctxs = ks.add_parser("ctx-set", help="写入/更新用例上下文（缺席字段不改，空串清除）")
    kctxs.add_argument("--case", required=True, help="用例 ID（本地 TC-xxx 或禅道数字 ID）")
    kctxs.add_argument("--scene", default=None, help="模拟器场景（前置依赖）")
    kctxs.add_argument("--pitfalls", default=None, help="已知坑，分号分隔；空串清除")
    kctxs.add_argument("--notes", default=None, help="人审备注；空串清除")
    kctxr = ks.add_parser("ctx-rm", help="删除用例上下文")
    kctxr.add_argument("case")
    kem = ks.add_parser("env-matrix", help="环境通过率矩阵（env_compat）")
    kem.add_argument("--version", default=None, help="被测版本（缺省全部版本）")
    kem.add_argument("--suite", type=int, default=None, help="方案 ID")
    kem.add_argument("--limit", type=int, default=12)

    # explore（M5+：探索路径固化回 DSL / 模拟器 MCP server）
    e = sub.add_parser("explore", help="M5+ 探索：会话固化回 DSL、模拟器 MCP server")
    es = e.add_subparsers(dest="action", required=True)
    es.add_parser("mcp", help="以 stdio 运行模拟器 MCP server（供 agent 接入）")
    eex = es.add_parser("export", help="探索会话 JSON -> DSL 草稿文本")
    eex.add_argument("--file", required=True, help="探索会话 JSON 路径")
    eex.add_argument("--case-id", dest="case_id", default=None,
                     help="覆盖草稿 case_id")
    eex.add_argument("--format", choices=["json", "yaml"], default="json")
    eex.add_argument("--out", default=None, help="输出文件（缺省打印到 stdout）")
    eim = es.add_parser("import", help="探索会话 JSON -> 导入本地草稿（待人审）")
    eim.add_argument("--file", required=True)
    eim.add_argument("--case-id", dest="case_id", default=None)

    sub.add_parser("serve", help="启动 JSON-RPC 服务（壳形态）")

    sch = sub.add_parser("schedule", help="定时回归（--once 或常驻）")
    sch.add_argument("--task", required=True, help="任务 ID（可重复 --task 多次）",
                     action="append")
    sch.add_argument("--every-min", type=int, default=None, dest="every_min",
                     help="间隔分钟（缺省取配置 zentao.sync_interval_min，默认 60）")
    sch.add_argument("--once", action="store_true", help="立即跑一轮后退出")
    sch.add_argument("--poll-seconds", type=int, default=10, dest="poll_seconds")
    return p


# ---- schedule ----

def cmd_schedule(cfg, args) -> int:
    from testpilot.app.scheduler import Scheduler

    every_min = args.every_min
    if every_min is None:      # help 承诺的缺省：取配置，避免 Scheduler 收到 None 崩溃
        every_min = int(cfg["zentao"].get("sync_interval_min", 60))
    orch = build_orchestrator(cfg)
    try:
        sched = Scheduler(orch)
        for task_id in args.task:
            sched.add(int(task_id), every_min)
        if args.once:
            emit({"results": sched.run_once()}, args.pretty)
        else:
            sched.run_forever(poll_seconds=args.poll_seconds)
            emit({"results": sched.status()}, args.pretty)
    finally:
        orch.close()
    return 0


_HANDLERS = {
    "db-init": cmd_db_init,
    "doctor": cmd_doctor,
    "case": cmd_case,
    "suite": cmd_suite,
    "task": cmd_task,
    "run": cmd_run,
    "kb": cmd_kb,
    "explore": cmd_explore,
    "serve": cmd_serve,
    "schedule": cmd_schedule,
}


def _extract_globals(argv: list[str]) -> tuple[bool, str | None, list[str]]:
    """抽出 ``--pretty`` / ``--config``，让它们可以出现在命令行任意位置。

    argparse 的根级选项只能写在子命令之前，而 README 与用户习惯都是
    ``testpilot case list --pretty`` 这种后置写法；这里预先摘出这两个全局
    标志（不与任何子命令参数重名），再交给 parser 解析剩余部分。
    """
    pretty, config, rest = False, None, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pretty":
            pretty = True
        elif a == "--config" and i + 1 < len(argv):
            config = argv[i + 1]
            i += 1
        elif a.startswith("--config="):
            config = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return pretty, config, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pretty, config, rest = _extract_globals(argv)
    args = build_parser().parse_args(rest)
    args.pretty = pretty              # 全局标志允许出现在任意位置（README 写法）
    if config is not None:
        args.config = config

    from testpilot.config.settings import load_settings
    from testpilot.infra.logsys import setup_logging

    cfg = load_settings(args.config)
    setup_logging(cfg.get("log", {}))

    try:
        return _HANDLERS[args.cmd](cfg, args)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as e:      # CLI 边界：统一错误出口
        log.error(f"命令失败: {e}", exc_info=True)
        print(json.dumps({"error": type(e).__name__, "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

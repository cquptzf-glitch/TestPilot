"""报告生成 — run 级 Markdown 报告（设计 14 章 RunDetail 的 CLI/文件形态）。

报告内容：
1. 概要：状态、时间、通过率、LLM token 计量；
2. 用例明细：状态 / DSL 版本 / 耗时 / 失败签名 / KB 命中 / 归因结论；
3. 步骤轨迹：**用例原文对照** + 驱动 + 定位/动作耗时 + 验证结论 + 双截图 + 设备状态；
4. 归因分布：类别 TOP + 处置建议（M4-5 归因报告的雏形，字段已齐备）；
5. 环境快照。

截图以相对路径内联（Markdown 图片），报告可整目录打包查看。
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("testpilot.reporter")


class Reporter:
    def __init__(self, conn, cfg: dict):
        self.conn = conn
        self.cfg = cfg

    # ---------- 对外 ----------

    def generate(self, run_id: int) -> Path:
        run = self.load(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        out_dir = Path(self.cfg["app"]["report_root"]) / str(run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "report.md"
        path.write_text(self.render(run_id), encoding="utf-8")
        log.info(f"报告已生成: {path}")
        return path

    def load(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM test_run WHERE id=?",
                                (run_id,)).fetchone()
        if row is None:
            return None
        cases = [dict(r) for r in self.conn.execute(
            "SELECT * FROM case_exec WHERE run_id=? ORDER BY id", (run_id,))]
        for c in cases:
            c["root_cause"] = _loads(c.get("root_cause_json"), None)
            c["retry"] = _loads(c.get("retry_json"), None)
            # 复跑用例有多次尝试的轨迹 -> 报告回放最后一次（要看首次失败时显式查 attempt=1）
            c["steps"] = [self._step(r) for r in self.conn.execute(
                "SELECT * FROM step_trace WHERE case_exec_id=? AND attempt="
                "COALESCE((SELECT MAX(attempt) FROM step_trace WHERE"
                " case_exec_id=?), 1) ORDER BY step_no", (c["id"], c["id"]))]
        out = dict(row)
        out["env"] = _loads(row["env_snapshot_json"], {})
        out["stats"] = _loads(row["stats_json"], {})
        out["cases"] = cases
        return out

    def render(self, run_id: int) -> str:
        run = self.load(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        stats = run["stats"]
        report_dir = Path(self.cfg["app"]["report_root"]) / str(run_id)
        stability = self._stability_map(run)     # 跨 run 统计：一次查询供两段复用
        lines: list[str] = []
        lines += self._header(run, stats)
        lines += self._drivers(stats)
        lines += self._cases(run, report_dir, stability)
        lines += self._root_causes(run)
        lines += self._stability(run, stability)
        lines += self._env_compat(run)
        lines += self._env(run)
        return "\n".join(lines) + "\n"

    # ---------- 各段 ----------

    def _header(self, run: dict, stats: dict) -> list[str]:
        total = int(stats.get("total", 0))
        passed = int(stats.get("pass", 0))
        rate = f"{passed / total * 100:.1f}%" if total else "n/a"
        usage = stats.get("llm_usage") or {}
        lines = [
            f"# Run#{run['id']} 执行报告",
            "",
            f"- 任务: #{run['task_id']}",
            f"- 状态: **{run['status']}**",
            f"- 时间: {run['started_at']} ~ {run['finished_at'] or '-'}",
            f"- 结果: 通过 **{passed}** / 失败 {stats.get('fail', 0)}"
            f" / 阻塞 {stats.get('blocked', 0)} / 跳过 {stats.get('skip', 0)}"
            f" / 错误 {stats.get('error', 0)} / 共 {total}（通过率 {rate}）",
        ]
        if usage:
            lines.append(f"- LLM: 调用 {usage.get('calls', 0)} 次，"
                         f"token {usage.get('tokens', 0)}，"
                         f"耗时 {usage.get('ms', 0)}ms，"
                         f"失败 {usage.get('errors', 0)}")
        if stats.get("preload_errors"):
            lines.append(f"- 场景预加载失败: {stats['preload_errors']}")
        if stats.get("kb_hit_cases"):
            lines.append(f"- 失败知识命中: {stats['kb_hit_cases']} 例")
        if stats.get("retry_cases"):
            lines.append(
                f"- 失败复跑: {stats['retry_cases']} 例（flake 过滤），其中 "
                f"**{stats.get('retry_passed', 0)} 例复跑后通过**（疑似偶发，"
                "见用例明细的复跑记录）")
        if stats.get("writeback"):
            lines.append(f"- 禅道回写: {stats['writeback']}")
        return lines + [""]

    def _drivers(self, stats: dict) -> list[str]:
        """驱动与降级统计（设计 10 章指标；数据源为 step_trace 的尝试链）。"""
        d = stats.get("drivers") or {}
        if not d.get("steps"):
            return []
        lines = [
            "## 驱动与降级统计", "",
            f"- 有驱动尝试的步骤: {d.get('steps')}，其中**降级**"
            f"（前序驱动失败、后序成功）{d.get('degraded_steps', 0)} 步"
            f"，降级率 {_pct(d.get('downgrade_rate'))}"
            f"；全部驱动失败 {d.get('failed_steps', 0)} 步",
            "", "| 驱动 | 尝试 | 成功 | 失败 | 成功率 | 平均耗时ms |",
            "|---|---|---|---|---|---|",
        ]
        for name, s in sorted((d.get("by_driver") or {}).items()):
            lines.append(
                f"| {name} | {s.get('attempts', 0)} | {s.get('success', 0)} "
                f"| {s.get('fail', 0)} | {_pct(s.get('success_rate'))} "
                f"| {s.get('avg_ms', 0)} |")
        transitions = d.get("transitions") or []
        if transitions:
            lines += ["", "| 降级路径 | 次数 | 最终成功 |", "|---|---|---|"]
            for t in transitions:
                lines.append(f"| {t.get('from')}→{t.get('to')} "
                             f"| {t.get('count')} | {t.get('resolved', 0)} |")
        return lines + [""]

    def _cases(self, run: dict, report_dir: Path,
               stability: dict | None = None) -> list[str]:
        stability = stability if stability is not None else {}
        lines = ["## 用例明细", ""]
        case_ctx = self._case_ctx_map()
        for c in run["cases"]:
            rc = c.get("root_cause") or {}
            lines += [
                f"### {c['case_id']} — {c['status']}"
                f"（DSL v{c['dsl_version']}，{c['duration_ms']}ms）",
                "",
                f"- 禅道用例: {c.get('zentao_case_id') or '-'}"
                f"　失败签名: `{c.get('failure_signature') or '-'}`"
                f"　KB 命中: {c.get('kb_hit_id') or '-'}",
            ]
            ctx_text = _format_case_ctx(case_ctx.get(c["case_id"]))
            if ctx_text:
                lines.append(f"- 用例上下文（人审维护）: {ctx_text}")
            attempts_text = _format_attempts(c.get("retry"))
            if attempts_text:
                lines.append(f"- 复跑记录: {attempts_text}")
            # 只给"值得追问"的用例补历史：偶发还是必现，决定后续投入方向
            note = _stability_note(stability.get(c["case_id"]))
            if note and c["status"] != "pass":
                lines.append(f"- 历史稳定性: {note}")
            if rc:
                lines.append(
                    f"- 归因: **{rc.get('category', 'unknown')}**"
                    f"（{rc.get('source', '-')}，置信 {rc.get('confidence', '-')}）"
                    f"{'　⚠️ 待人审' if rc.get('needs_review') else ''}")
                if rc.get("evidence"):
                    lines.append(f"  - 依据: {rc['evidence']}")
                if rc.get("suggestion"):
                    lines.append(f"  - 建议: {rc['suggestion']}")
                for ref in rc.get("similar") or []:
                    lines.append(
                        f"  - 相似历史: `{ref.get('signature', '-')}`"
                        f" → {ref.get('category', '-')}"
                        f"（置信 {ref.get('confidence', '-')}，"
                        f"命中 {ref.get('hits', 0)} 次）"
                        f"{'；' + _cell(ref['suggestion']) if ref.get('suggestion') else ''}")
            hist_line = self._history(c.get("failure_signature"))
            if hist_line:
                lines.append(hist_line)
            lines += ["", "| # | 动作 | 用例原文 | 驱动 | 定位ms | 动作ms "
                          "| 验证 | 说明 |", "|---|---|---|---|---|---|---|---|"]
            for s in c["steps"]:
                lines.append(
                    f"| {s['step_no']} | {s['action']} "
                    f"| {_cell(s.get('source_text'))} "
                    f"| {s.get('driver') or '-'} "
                    f"| {_num(s.get('locate_ms'))} | {_num(s.get('act_ms'))} "
                    f"| {s.get('verify_result') or '-'} "
                    f"| {_cell(s.get('error') or s.get('verify_detail'))} |")
            lines += self._receipts(c["steps"])
            lines += self._shots(c["steps"], report_dir)
            lines.append("")
        return lines

    def _shots(self, steps: list[dict], report_dir: Path) -> list[str]:
        lines = []
        with_shots = [s for s in steps if s.get("screenshot_path")
                      or s.get("screenshot_before_path")]
        if not with_shots:
            return []
        shot_root = Path(self.cfg["app"]["screenshot_root"]).resolve()
        lines.append("")
        for s in with_shots:
            # 双截图都可达（设计 8.7）：before 之前只落盘不入库，成了孤儿文件。
            # after 保持既有"截图:"行格式（报告消费者与测试都认它）。
            for key, label in (("screenshot_path", "截图"),
                               ("screenshot_before_path", "截图 before")):
                rel_in_db = s.get(key)
                if not rel_in_db:
                    continue
                rel = _safe_relpath(shot_root / rel_in_db, report_dir)
                lines.append(f"- 步骤 {s['step_no']} {label}: ![]({rel})")
        return lines

    def _root_causes(self, run: dict) -> list[str]:
        dist: dict[str, int] = {}
        suggestions: dict[str, set] = {}
        for c in run["cases"]:
            rc = c.get("root_cause") or {}
            if not rc:
                continue
            cat = str(rc.get("category", "unknown"))
            dist[cat] = dist.get(cat, 0) + 1
            if rc.get("suggestion"):
                suggestions.setdefault(cat, set()).add(str(rc["suggestion"]))
        if not dist:
            return []
        lines = ["## 失败归因分布", "", "| 类别 | 次数 | 处置建议 |", "|---|---|---|"]
        for cat, n in sorted(dist.items(), key=lambda kv: -kv[1]):
            tips = "；".join(sorted(suggestions.get(cat, []))) or "-"
            lines.append(f"| {cat} | {n} | {_cell(tips)} |")
        return lines + [""]

    def _stability_map(self, run: dict) -> dict:
        """按用例取跨 run 稳定性（报告是只读视图，统计失败不阻断出报告）。"""
        from testpilot.engine.stats import case_stability

        case_ids = [c["case_id"] for c in run["cases"]]
        if not case_ids:
            return {}
        try:
            return case_stability(self.conn, case_ids)
        except Exception as e:
            log.warning(f"稳定性统计失败(不阻断): {e}")
            return {}

    def _stability(self, run: dict, stability: dict) -> list[str]:
        """回归稳定性段：把"偶发 or 必现"的判据摆进报告（设计 9.1）。

        只列抖动/必现两类 —— 全部稳定的用例没有信息量，不该占篇幅。
        """
        from testpilot.engine.stats import DEFAULT_WINDOW

        notable = [(cid, s) for cid, s in sorted(stability.items())
                   if s.get("runs", 0) >= 2
                   and s.get("level") in ("flaky", "broken")]
        if not notable:
            return []
        levels: dict[str, int] = {}
        for s in stability.values():
            levels[s.get("level")] = levels.get(s.get("level"), 0) + 1
        lines = [
            "## 回归稳定性（跨 run）", "",
            f"- 窗口: 每个用例最近 {DEFAULT_WINDOW} 次执行；判据: 抖动=有输有赢"
            "或复跑后才通过（疑似偶发），必现失败=窗口内从未通过。",
            f"- 本次 {len(stability)} 条用例: 稳定 {levels.get('stable', 0)}、"
            f"抖动 {levels.get('flaky', 0)}、必现失败 {levels.get('broken', 0)}、"
            f"样本不足 {levels.get('unknown', 0)}。",
            "",
            "| 用例 | 最近执行（新→旧） | 通过 | 翻转 | 结论 |",
            "|---|---|---|---|---|",
        ]
        for cid, s in notable:
            recent = "".join(_stability_symbol(x) for x in s.get("recent", []))
            verdict = _stability_label(s)
            if s.get("retried"):
                # 全通过却判抖动的唯一原因就是这个，不写出来会让人以为算错了
                verdict += f"（复跑通过 {s['retried']} 次）"
            lines.append(f"| {cid} | {recent} | {s.get('pass', 0)}/{s.get('runs', 0)}"
                         f" | {s.get('flips', 0)} | {verdict} |")
        return lines + [""]

    def _case_ctx_map(self) -> dict[str, dict]:
        """人审维护的用例上下文（case_ctx）按本地 case_id 索引。"""
        out: dict[str, dict] = {}
        try:
            rows = self.conn.execute(
                "SELECT signature, payload_json FROM kb_entry"
                " WHERE type='case_ctx' AND status='active'")
        except Exception:
            return out
        for row in rows:
            out[row["signature"]] = _loads(row["payload_json"], {})
        return out

    def _env_compat(self, run: dict) -> list[str]:
        """历史环境通过率矩阵（env_compat）：同一方案 / 用例集下的各被测版本对照。"""
        from testpilot.knowledge.service import KnowledgeService

        env = run.get("env") or {}
        version = env.get("test_app_version")
        suite_id = env.get("suite_id")
        if not version:
            return []
        try:
            entries = KnowledgeService(self.conn, {}).env_compat_matrix(
                suite_id=suite_id, case_ids=env.get("case_ids"))
        except Exception as e:      # 矩阵是增强信息，失败不阻断报告
            log.warning(f"env_compat 查询失败(不阻断): {e}")
            return []
        if not entries:
            return []
        versions = [str(e["payload"].get("test_app_version", "?"))
                    for e in entries]
        case_ids = list(env.get("case_ids")
                        or (entries[0]["payload"].get("case_ids") or []))
        lines = ["## 历史环境通过率（env_compat 矩阵）", "",
                 "| 用例 | " + " | ".join(versions) + " |",
                 "|" + "---|" * (len(versions) + 1)]
        for cid in case_ids:
            cells = [_cell_ratio((e["payload"].get("matrix") or {}).get(cid))
                     for e in entries]
            lines.append(f"| {cid} | " + " | ".join(cells) + " |")
        lines += ["", f"> 单元格 = 通过数/总次数；覆盖最近 {len(entries)} 个版本，"
                      f"本次为 {version}。", ""]
        return lines

    def _env(self, run: dict) -> list[str]:
        return ["## 环境快照", "", "```json",
                json.dumps(run["env"], ensure_ascii=False, indent=2), "```", ""]

    # ---------- 内部 ----------

    def _step(self, row) -> dict:
        d = dict(row)
        d["error"] = _loads(d.get("error_json"), None)
        d["device_state"] = _loads(d.get("device_state_json"), None)
        d["receipt"] = _loads(d.get("receipt_json"), None)
        d["target"] = _loads(d.get("target_json"), None)
        return d

    def _history(self, signature) -> str | None:
        """快速定位体验（设计 9.4）：该签名历史次数 / 根因分布 / 最近一次 / 建议。"""
        if not signature:
            return None
        from testpilot.engine.stats import signature_history

        try:
            hist = signature_history(self.conn, str(signature))
        except Exception as e:              # 统计失败不该阻断出报告
            log.warning(f"签名历史统计失败(不阻断): {e}")
            return None
        if not hist:
            return None
        dist = " / ".join(f"{c['category']} {c['count']}"
                          for c in hist["categories"]) or "-"
        last = hist["last"]
        where = f"run#{last.get('run_id')} / {last.get('case_id')}"
        if last.get("step_no"):
            where += f" / step#{last['step_no']}"
        if last.get("at"):
            where += f"（{last['at']}）"
        text = (f"- 快速定位: 该签名历史出现 **{hist['count']}** 次；"
                f"根因分布: {dist}；最近一次: {where}")
        if hist.get("suggestion"):
            text += f"；建议: {hist['suggestion']}"
        return text

    def _receipts(self, steps: list[dict]) -> list[str]:
        """设备指令回执（设计 14.3）：失败的 device 步骤也保留"发过什么指令"。"""
        out = []
        for s in steps:
            r = s.get("receipt")
            if not r:
                continue
            ms = r.get("ms")
            cost = f"（{ms}ms）" if isinstance(ms, (int, float)) else ""
            err = f" · {r['error']}" if r.get("error") else ""
            status = "ok" if r.get("ok") else "失败"
            out.append(f"- 指令回执 步骤{s['step_no']}: `{r.get('cmd', '-')}`"
                       f" → {status}{cost}{err}")
        return out


def _safe_relpath(target: Path, start: Path) -> str:
    """报告内的截图相对路径；跨盘符（Windows relpath 会抛 ValueError）降级为绝对路径。"""
    try:
        return os.path.relpath(target, start).replace("\\", "/")
    except ValueError:
        return target.resolve().as_posix()


def _loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _num(value) -> str:
    return "-" if value is None else str(value)


def _cell(value) -> str:
    """表格单元：压掉换行与竖线，避免破表。"""
    if value is None:
        return "-"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text[:120] if text else "-"


def _pct(value) -> str:
    """比率 -> 百分比文案；None（历史数据无法判定）显示 ``-``。"""
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _cell_ratio(cell) -> str:
    """env_compat 矩阵单元：``pass/total (rate)``。"""
    if not cell:
        return "-"
    total = int(cell.get("total", 0))
    passed = int(cell.get("pass", 0))
    if total <= 0:
        return "-"
    return f"{passed}/{total} ({passed / total * 100:.0f}%)"


def _format_case_ctx(entry) -> str:
    """用例上下文的人读摘要（场景/已知坑/备注）。"""
    if not entry:
        return ""
    parts = []
    if entry.get("scene"):
        parts.append(f"场景 {entry['scene']}")
    pitfalls = entry.get("pitfalls") or []
    if pitfalls:
        parts.append("已知坑：" + "；".join(str(p) for p in pitfalls))
    if entry.get("notes"):
        parts.append(f"备注：{entry['notes']}")
    return "　".join(parts)


#: 稳定性结论文案（引擎只给 level 枚举，中文只在展示层出现）
_STABILITY_LABELS = {
    "stable": "稳定",
    "flaky": "抖动",
    "broken": "必现失败",
    "unknown": "样本不足",
}

_STABILITY_SYMBOLS = {"pass": "✓", "fail": "✗", "blocked": "⊘", "skip": "–"}


def _stability_label(entry: dict) -> str:
    return _STABILITY_LABELS.get(str(entry.get("level")), "")


def _stability_symbol(status) -> str:
    return _STABILITY_SYMBOLS.get(str(status), "?")


def _stability_note(entry) -> str:
    """用例明细里的一行历史（仅非通过用例展示，稳定用例不占篇幅）。"""
    if not entry or not entry.get("runs"):
        return ""
    text = f"最近 {entry['runs']} 次执行中 {entry.get('pass', 0)} 次通过"
    if entry.get("retried"):
        text += f"，其中 {entry['retried']} 次靠复跑才通过"
    if entry.get("flips"):
        text += f"，状态翻转 {entry['flips']} 次"
    label = _stability_label(entry)
    return f"{text}（{label}）" if label else text


def _format_attempts(attempts) -> str:
    """复跑记录：每次尝试的状态/耗时/签名 + 结论（疑似偶发 or 必现）。"""
    if not attempts or len(attempts) < 2:
        return ""
    pieces = []
    for item in attempts:
        piece = f"第{item.get('attempt')}次 {item.get('status')}"
        if item.get("duration_ms"):
            piece += f"({item['duration_ms']}ms)"
        if item.get("failure_signature"):
            piece += f"[{_cell(item['failure_signature'])[:24]}]"
        pieces.append(piece)

    last = str(attempts[-1].get("status") or "")
    if last == "pass":
        verdict = "复跑后通过（疑似偶发）"
        first_cause = (attempts[0].get("root_cause") or {}).get("category")
        if first_cause:
            verdict += f"，首次归因 {first_cause}"
    elif last in ("skip", "blocked"):
        # 被中止/被阻塞不是"复跑后仍失败"：标"必现"会误导排查方向
        verdict = f"复跑未完成（末次 {last}，见运行日志）"
    else:
        verdict = "复跑后仍未通过（必现）"
    return " → ".join(pieces) + f"；结论: {verdict}"

"""CLI 单测 — 命令表解析、参数助手、端到端命令出口（M1 命令链）。"""
import json

import pytest
import yaml

from testpilot.app.cli import _case_ids, _coerce, _ids, _kv, build_parser, main


@pytest.fixture()
def cli_cfg(tmp_path):
    """落一份独立的临时配置（路径全在 tmp 下），供 CLI 端到端调用。"""
    from testpilot.config.settings import load_settings

    c = load_settings()
    c["app"]["db"] = str(tmp_path / "cli.db")
    c["app"]["screenshot_root"] = str(tmp_path / "shots")
    c["app"]["report_root"] = str(tmp_path / "reports")
    c["vision"]["template_dir"] = str(tmp_path / "templates")
    c["log"]["per_run_file"] = False
    c["log"]["level"] = "CRITICAL"        # 测试里关掉日志，stdout/stderr 只留命令输出
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")
    return str(path)


# ---------- 参数解析 ----------

def test_parser_accepts_core_commands():
    parser = build_parser()
    args = parser.parse_args(["case", "pull", "--ids", "1,2", "--parse"])
    assert args.cmd == "case" and args.action == "pull"
    assert args.ids == "1,2" and args.parse is True

    args = parser.parse_args(["task", "create", "--name", "n", "--suite", "3",
                              "--remote", "--force"])
    assert args.suite == "3" and args.remote is True and args.force is True

    args = parser.parse_args(["case", "list", "--status", "active"])
    assert args.status == "active"

    args = parser.parse_args(["schedule", "--task", "1", "--task", "2",
                              "--every-min", "30", "--once"])
    assert args.task == ["1", "2"] and args.every_min == 30 and args.once is True

    archive = parser.parse_args(["run", "archive", "--before",
                                 "2026-01-01T00:00:00", "--delete"])
    assert archive.before == "2026-01-01T00:00:00"
    assert archive.delete is True

    compare = parser.parse_args(["run", "compare", "--base", "1", "--target", "2"])
    assert compare.base == 1 and compare.target == 2

    run_task = parser.parse_args(["task", "run", "3", "--retry", "1"])
    assert run_task.retry == 1


def test_parser_accepts_kb_ctx_and_explore_commands():
    parser = build_parser()
    args = parser.parse_args(["kb", "ctx-set", "--case", "TC-1",
                              "--scene", "s", "--pitfalls", "a;b",
                              "--notes", "n"])
    assert args.action == "ctx-set" and args.case == "TC-1"
    assert args.pitfalls == "a;b"

    args = parser.parse_args(["kb", "env-matrix", "--suite", "3", "--limit", "5"])
    assert args.suite == 3 and args.limit == 5

    args = parser.parse_args(["explore", "export", "--file", "s.json",
                              "--format", "yaml", "--case-id", "TC-9"])
    assert args.action == "export" and args.case_id == "TC-9"

    args = parser.parse_args(["explore", "import", "--file", "s.json"])
    assert args.action == "import"


def test_parser_rejects_unknown_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nope"])


def test_argument_helpers():
    assert _ids("1, 2,3") == [1, 2, 3]
    assert _ids("1，2") == [1, 2]
    assert _case_ids("TC-1, TC-2") == ["TC-1", "TC-2"]
    assert _kv("mode=uia_only,retry=true,n=3,ratio=0.5") == {
        "mode": "uia_only", "retry": True, "n": 3, "ratio": 0.5}
    assert _coerce("false") is False
    assert _coerce("abc") == "abc"


def test_kv_rejects_malformed_pair():
    with pytest.raises(SystemExit):
        _kv("mode")


# ---------- 端到端命令 ----------

def test_db_init_creates_tables(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "db-init"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {"dsl_case", "test_suite", "test_task", "test_run", "case_exec",
            "step_trace", "kb_entry", "router_stats"} <= set(out["tables"])


def test_case_list_empty(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "case", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_doctor_reports_health(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "doctor"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["db"]["ok"] is True
    assert "drivers" in out


def test_kb_case_ctx_cli_end_to_end(cli_cfg, capsys):
    """人审维护链路在 CLI 侧可用：ctx-set -> ctx-list -> ctx-rm。"""
    from testpilot.config.settings import load_settings
    from testpilot.storage import repos
    from testpilot.storage.db import connect

    cfg = load_settings(cli_cfg)
    conn = connect(cfg["app"]["db"])
    repos.create_case(conn, case_id="TC-7", zentao_case_id=7, title="用例七",
                      raw=None, zentao_updated_at="t")
    conn.close()

    assert main(["--config", cli_cfg, "kb", "ctx-set", "--case", "TC-7",
                 "--scene", "s1", "--pitfalls", "a;b", "--notes", "n"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["payload"]["pitfalls"] == ["a", "b"]

    assert main(["--config", cli_cfg, "kb", "ctx-list", "--case", "7"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["entries"][0]["case_id"] == "TC-7"

    assert main(["--config", cli_cfg, "kb", "ctx-rm", "TC-7"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1


def test_explore_export_cli(cli_cfg, capsys, tmp_path):
    """探索会话 JSON -> DSL 草稿文本（固化链路的第一步）。"""
    session = {
        "case_id": "TC-500", "title": "探索导出",
        "steps": [
            {"index": 1, "tool": "app.launch", "args": {"exe": "target.exe"},
             "ok": True, "ms": 1200},
            {"index": 2, "tool": "ui.click",
             "args": {"key": "btn.start", "name": "开始"}, "ok": True, "ms": 30},
            {"index": 3, "tool": "screen.grab", "args": {}, "ok": True, "ms": 80},
        ],
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")

    assert main(["--config", cli_cfg, "explore", "export",
                 "--file", str(path), "--format", "json"]) == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft["case_id"] == "TC-500"
    assert [s["action"] for s in draft["steps"]] == ["app.launch", "ui.click"]
    assert "跳过 1 步" in draft["notes"]


def test_suite_lifecycle(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "suite", "create", "--name", "S1",
                 "--cases", "TC-1,TC-2", "--driver", "mode=uia_only"]) == 0
    suite = json.loads(capsys.readouterr().out)
    assert suite["name"] == "S1" and suite["driver_config"]["mode"] == "uia_only"
    # 用例尚未入库 -> 明确暴露不可执行
    assert [b["case_id"] for b in suite["not_runnable"]] == ["TC-1", "TC-2"]

    assert main(["--config", cli_cfg, "suite", "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)["suites"]) == 1

    assert main(["--config", cli_cfg, "suite", "show", str(suite["id"])]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == suite["id"]

    assert main(["--config", cli_cfg, "suite", "delete", str(suite["id"])]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == suite["id"]


def test_task_create_gate_blocks_unrunnable(cli_cfg, capsys):
    rc = main(["--config", cli_cfg, "task", "create", "--name", "T1",
               "--cases", "TC-1"])
    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"] == "TestPilotError"
    assert "不可执行" in err["message"]


def test_case_show_missing_returns_error(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "case", "show", "TC-404"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "TC-404" in err["message"]


def test_task_list_and_run_list_empty(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "task", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == []
    assert main(["--config", cli_cfg, "run", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == []


def test_kb_pending_empty(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "kb", "pending"]) == 0
    assert json.loads(capsys.readouterr().out) == {"count": 0, "entries": []}


def test_run_compare_cli_with_seeded_runs(cli_cfg, capsys):
    """两次 run 对比在 CLI 侧可用（直接种数据，不依赖 GUI 驱动执行）。"""
    from testpilot.config.settings import load_settings
    from testpilot.storage.db import connect

    cfg = load_settings(cli_cfg)
    conn = connect(cfg["app"]["db"])
    conn.execute("INSERT INTO dsl_case(case_id, title, version, steps_json,"
                 " updated_at) VALUES('TC-1','用例一',1,'[]','t')")
    conn.execute("INSERT INTO test_task(id, name, case_ids_json, created_at)"
                 " VALUES(1,'t','[\"TC-1\"]','t')")
    for run_id, status in ((1, "pass"), (2, "fail")):
        conn.execute("INSERT INTO test_run(id, task_id, started_at, finished_at,"
                     " status, env_snapshot_json, stats_json)"
                     " VALUES(?,1,'t','t','done','{}','{}')", (run_id,))
        conn.execute("INSERT INTO case_exec(run_id, case_id, dsl_version, status,"
                     " duration_ms, started_at) VALUES(?,'TC-1',1,?,10,'t')",
                     (run_id, status))
    conn.commit()
    conn.close()

    assert main(["--config", cli_cfg, "run", "compare",
                 "--base", "1", "--target", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [c["case_id"] for c in out["new_failures"]] == ["TC-1"]
    assert out["counts"]["new_failures"] == 1

    # 非法参数走统一错误出口（stderr + 退出码 1），不静默返回空结果
    assert main(["--config", cli_cfg, "run", "compare",
                 "--base", "1", "--target", "1"]) == 1
    assert "同一个" in json.loads(capsys.readouterr().err)["message"]


def test_case_export_unknown_returns_error(cli_cfg, capsys):
    assert main(["--config", cli_cfg, "case", "export", "TC-404"]) == 1
    assert "TC-404" in json.loads(capsys.readouterr().err)["message"]


def test_case_import_creates_local_draft(cli_cfg, tmp_path, capsys):
    payload = {"case_id": "TC-501", "title": "手工", "steps": [
        {"id": "s1", "action": "sleep", "args": {"ms": 1}}]}
    src = tmp_path / "case.json"
    src.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--config", cli_cfg, "case", "import", "--file", str(src)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["case_id"] == "TC-501" and out["step_count"] == 1

    assert main(["--config", cli_cfg, "case", "accept", "TC-501"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1

    assert main(["--config", cli_cfg, "case", "export", "TC-501"]) == 0
    assert "TC-501" in capsys.readouterr().out


def test_yaml_import_format_detected_from_suffix(cli_cfg, tmp_path, capsys):
    payload = {"case_id": "TC-502", "steps": [{"id": "s1", "action": "sleep"}]}
    src = tmp_path / "case.yaml"
    src.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    assert main(["--config", cli_cfg, "case", "import", "--file", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["case_id"] == "TC-502"

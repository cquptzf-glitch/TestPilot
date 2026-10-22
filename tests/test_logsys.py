"""结构化日志单测 — 上下文注入 / extra 白名单 / stderr 分离 / run 日志抽取（设计 7.5）。"""
import json
import logging

import pytest

from testpilot.infra.logsys import (JsonFormatter, bind_case, bind_run, bind_step,
                                    extract_run_log, setup_logging)


@pytest.fixture()
def clean_logger():
    """保存并恢复 testpilot logger 的 handler（避免污染其他测试）。"""
    logger = logging.getLogger("testpilot")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        if h not in saved_handlers:
            try:
                h.close()
            except Exception:
                pass
    for h in saved_handlers:
        logger.addHandler(h)
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate


def _record(msg="hello", **extra):
    record = logging.LogRecord(name="testpilot.x", level=logging.INFO, pathname=__file__,
                               lineno=1, msg=msg, args=(), exc_info=None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_includes_context_and_whitelisted_extras():
    bind_run(7)
    bind_case("TC-1")
    bind_step("s2")
    payload = json.loads(JsonFormatter().format(_record(
        "定位成功", driver="uia", target_key="btn.start", locate_ms=12,
        无关字段="不该出现")))

    assert payload["msg"] == "定位成功"
    assert payload["level"] == "INFO"
    assert payload["run_id"] == 7
    assert payload["case_id"] == "TC-1"
    assert payload["step_id"] == "s2"
    assert payload["driver"] == "uia"
    assert payload["target_key"] == "btn.start"
    assert payload["locate_ms"] == 12
    assert "无关字段" not in payload
    assert "ts" in payload


def test_formatter_includes_exception_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _record("失败")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]


def test_formatter_emits_valid_json_with_unicode():
    payload = JsonFormatter().format(_record("中文消息 ✓"))
    assert json.loads(payload)["msg"] == "中文消息 ✓"


def test_setup_logging_writes_to_stderr_not_stdout(clean_logger, capsys):
    """CLI 的 stdout 必须只有命令结果，日志一律走 stderr。"""
    setup_logging({"level": "INFO", "per_run_file": False})
    logging.getLogger("testpilot.infra.logsys").info("仅日志")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "仅日志" in captured.err


def test_setup_logging_is_idempotent(clean_logger, tmp_path):
    cfg = {"level": "WARNING", "per_run_file": True, "dir": str(tmp_path)}
    setup_logging(cfg)
    first = len(clean_logger.handlers)
    setup_logging(cfg)
    assert len(clean_logger.handlers) == first        # 不叠加 handler
    assert clean_logger.level == logging.WARNING
    assert (tmp_path / "testpilot.log").exists()


def test_setup_logging_creates_log_dir(clean_logger, tmp_path):
    target = tmp_path / "deep" / "logs"
    setup_logging({"per_run_file": True, "dir": str(target)})
    assert target.is_dir()


def test_setup_logging_unknown_level_falls_back(clean_logger):
    setup_logging({"level": "NOT_A_LEVEL", "per_run_file": False})
    assert clean_logger.level == logging.INFO


# ---------------------------------------------------------------- run 日志抽取

def test_extract_run_log_filters_by_run_id(tmp_path):
    src = tmp_path / "testpilot.log"
    src.write_text("\n".join([
        json.dumps({"run_id": 1, "msg": "a"}),
        "不是 JSON 的脏行",
        "",
        json.dumps({"run_id": 2, "msg": "b"}),
        json.dumps({"run_id": 1, "msg": "c"}),
    ]), encoding="utf-8")

    out = tmp_path / "reports" / "1" / "run.log"
    assert extract_run_log(src, 1, out) == 2
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(x)["msg"] for x in lines] == ["a", "c"]


def test_extract_run_log_missing_source_returns_zero(tmp_path):
    assert extract_run_log(tmp_path / "nope.log", 1, tmp_path / "o.log") == 0


def test_extract_run_log_no_match_creates_empty_file(tmp_path):
    src = tmp_path / "testpilot.log"
    src.write_text(json.dumps({"run_id": 9}), encoding="utf-8")
    out = tmp_path / "run.log"
    assert extract_run_log(src, 1, out) == 0
    assert out.read_text(encoding="utf-8") == ""


def test_bind_run_is_isolated_per_context():
    """contextvars 在拷贝上下文里互不干扰（多 run 串/并发不串号的前提）。"""
    import contextvars

    bind_run(1)
    seen = {}

    def child():
        bind_run(2)
        seen["child"] = json.loads(JsonFormatter().format(_record()))["run_id"]

    contextvars.copy_context().run(child)
    assert seen["child"] == 2
    assert json.loads(JsonFormatter().format(_record()))["run_id"] == 1

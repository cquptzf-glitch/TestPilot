"""结构化日志 — run/case/step 三级上下文自动注入（设计 7.5）。

约定：
- 用 ``bind_run`` / ``bind_case`` / ``bind_step`` 绑定上下文（contextvars，线程安全）；
- 业务字段通过 ``extra={...}`` 传入，命中白名单即进 JSON 载荷；
- run 结束后用 ``extract_run_log`` 把 ``run_id`` 匹配行抽成
  ``data/reports/{run_id}/run.log``（JSONL，便于检索）。
"""
import contextvars
import json
import logging
import sys
import time
from pathlib import Path

_run_id = contextvars.ContextVar("run_id", default=None)
_case_id = contextvars.ContextVar("case_id", default=None)
_step_id = contextvars.ContextVar("step_id", default=None)

# 允许进 JSON 载荷的业务字段（防止 extra 乱塞导致日志膨胀）
_EXTRA_KEYS = (
    "driver", "target_key", "step_no", "action", "cmd", "ms", "locate_ms",
    "act_ms", "status", "llm_model", "llm_tokens", "llm_ms", "prompt_version",
    "sig", "kb_hit", "case_exec_id", "run_id_", "task_id", "suite_id",
    "confidence", "phase", "count",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": _run_id.get(),
            "case_id": _case_id.get(),
            "step_id": _step_id.get(),
        }
        for k in _EXTRA_KEYS:
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(cfg: dict) -> logging.Logger:
    root = logging.getLogger("testpilot")
    root.setLevel(getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO))
    for h in list(root.handlers):   # 幂等：重复调用不叠加 handler（并释放文件句柄）
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.propagate = False

    # 日志走 stderr，stdout 留给命令结果（CLI 输出必须是可解析的纯 JSON）
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    if cfg.get("per_run_file"):
        log_dir = Path(cfg.get("dir", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "testpilot.log", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    return root


def bind_run(run_id: int) -> None:
    _run_id.set(run_id)


def bind_case(case_id: str) -> None:
    _case_id.set(case_id)


def bind_step(step_id: str) -> None:
    _step_id.set(step_id)


def extract_run_log(log_path: str | Path, run_id: int, out_path: str | Path) -> int:
    """把 ``log_path`` 中 run_id 匹配的 JSONL 行抽到 ``out_path``。

    返回抽取行数；日志文件不存在时返回 0（不抛异常，不阻断收尾）。
    """
    src = Path(log_path)
    if not src.exists():
        return 0
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with src.open("r", encoding="utf-8") as fin, \
            out.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("run_id") != run_id:
                    continue
            except ValueError:
                continue
            fout.write(line + "\n")
            n += 1
    return n

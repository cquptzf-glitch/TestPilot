"""截图三级目录存取 — ``data/shots/{run}/{case_exec}/{step}_{phase}.png``。

单一路径权威：``Recorder.save_screenshot`` 委托本类，避免路径规则散落两处。
``phase`` 取 ``before|after|error``（设计 3.4）。

**attempt 段**：失败复跑复用同一个 ``case_exec`` 行（一条用例一票），同一步骤
会被执行多次 —— 路径若不含 attempt，第二次执行会**静默覆盖**第一次的取证截图。
第 1 次尝试保持旧命名（兼容既有库内引用），第 2 次起加 ``a{attempt}_`` 前缀。
"""
import logging
from pathlib import Path

log = logging.getLogger("testpilot.shotstore")

PHASES = ("before", "after", "error")


def rel_path(run_id: int, case_exec_id: int, step_no: int, phase: str,
             attempt: int = 1) -> str:
    """截图相对路径的唯一权威实现（写盘与消费方都必须走这里）。"""
    step_part = f"{step_no}_{phase}" if int(attempt or 1) <= 1 \
        else f"a{int(attempt)}_{step_no}_{phase}"
    return f"{run_id}/{case_exec_id}/{step_part}.png"


class ShotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.errors = 0

    def path(self, run_id: int, case_exec_id: int, step_no: int, phase: str,
             attempt: int = 1) -> Path:
        return self.root / rel_path(run_id, case_exec_id, step_no, phase, attempt)

    def resolve(self, rel: str) -> Path:
        return self.root / rel

    def write(self, run_id: int, case_exec_id: int, step_no: int, phase: str,
              img: bytes, attempt: int = 1) -> str:
        """写盘并返回相对路径；失败只记日志（不阻断执行）。"""
        if phase not in PHASES:
            log.warning(f"非标准截图 phase={phase}（仍按原样存储）")
        rel = rel_path(run_id, case_exec_id, step_no, phase, attempt)
        path = self.root / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(img)
        except OSError as e:
            self.errors += 1
            log.error(f"截图写盘失败(不阻断): {path} {e}")
        return rel

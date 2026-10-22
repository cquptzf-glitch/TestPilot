"""领域契约 — 引擎只依赖本文件，不依赖具体驱动（设计文档 7.2）。

v1.3 增 device_state / source_text；本轮（M1）增：
- ``NullAbort``：空对象中止信号，``ExecContext.abort`` 绝不出现 ``None``（core 不允许依赖 infra）；
- ``StepResult.target_json`` / ``verify_detail`` / ``kb_hit``：轨迹落库所需（设计"轨迹是一切地基"）。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from testpilot.core.dsl import Locator, TestCase

Rect = tuple[int, int, int, int]  # x, y, w, h（物理像素）


class DriverKind(str, Enum):
    UIA = "uia"
    VISION = "vision"
    TEMPLATE = "template"
    EMULATOR = "emulator"


@dataclass
class DriverManifest:
    """驱动自描述清单（设计 2.4 / 7.3）。

    定位：**值对象**，与 ``Driver`` 协议同属契约层，因此定义在 contracts；
    ``core.registry`` 从本模块导入并再导出（保持 ``registry.DriverManifest`` 可用）。
    """
    kind: str                      # interaction | device | external
    name: str                      # uia | vision | emulator | ...
    version: str
    capabilities: frozenset[str]   # click/input/select/grab/wait_ready...
    config_schema: dict = field(default_factory=dict)  # JSON Schema
    entry: str = ""                # "pkg.module:create"
    priority: int = 100


# ---------- 协作取消空对象 ----------

class NullAbort:
    """默认中止信号：永不请求取消。真正的信号见 ``infra.abort.AbortSignal``。

    core 层不允许依赖 infra，因此这里给出满足 ``check()`` 语义的空对象，
    保证驱动里 ``ctx.abort.check()`` 永远不会是空值解引用。
    """

    __slots__ = ()

    def request(self) -> None:  # pragma: no cover - 语义化空实现
        return None

    @property
    def requested(self) -> bool:
        return False

    def check(self) -> None:
        return None


# ---------- 值对象 ----------

@dataclass
class ExecContext:
    """单用例执行上下文（贯穿所有驱动调用，用于日志关联与窗口解析）。"""
    run_id: int
    case_exec_id: int
    case: TestCase | None = None
    window_rect: Rect | None = None       # 当前被测窗口矩形（视觉驱动换算基准）
    app_handle: Any = None                # UIA 驱动的窗口句柄缓存
    abort: Any = field(default_factory=NullAbort)   # 协作取消信号
    extra: dict[str, Any] = field(default_factory=dict)
    #: 第几次执行（失败复跑时递增；轨迹按此区分，见设计 6.2 的"偶发/必现"判据）
    attempt: int = 1

    def check_abort(self) -> None:
        """检查点统一入口（驱动/执行器共用）。"""
        self.abort.check()


@dataclass
class LocatedElement:
    driver: DriverKind
    handle: Any            # UIA wrapper 或 bbox
    rect: Rect
    confidence: float = 1.0


@dataclass
class Screenshot:
    png: bytes
    rect: Rect | None = None


@dataclass
class HealthReport:
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    status: str            # pass | fail | unknown
    detail: str = ""
    confidence: float | None = None


@dataclass
class StepResult:
    step_no: int
    step_id: str
    action: str
    status: str            # pass | fail | blocked | skipped
    driver: str | None = None
    locate_ms: int = 0
    act_ms: int = 0
    # 驱动尝试链（设计 10 章"驱动降级率"的数据地基）：
    # [{"driver","ok","ms","error"}, ...]，按尝试顺序；降级 = 前序失败 + 末位成功
    driver_chain: list[dict] | None = None
    verify: VerifyResult | None = None
    screenshot_before: str | None = None
    screenshot_after: str | None = None
    device_state: Any = None          # device 步骤后模拟器状态快照摘要
    #: 设备指令回执（设计 14.3）：{step_id, cmd, ok, ms, error?}，落 step_trace.receipt_json
    device_receipt: dict | None = None
    source_text: str | None = None    # 来源用例原文步骤（轨迹对照）
    target_json: dict | None = None   # 步骤 target 描述符快照（回放/归因用）
    error: dict | None = None
    kb_hit: int | None = None


@dataclass
class CaseExecResult:
    case_id: str
    status: str            # pass | fail | blocked | skip | error
    duration_ms: int = 0   # 由执行引擎在收尾时回填；构造阶段允许缺省
    steps: list[StepResult] = field(default_factory=list)
    failure_signature: str | None = None
    kb_hit: int | None = None
    root_cause: dict | None = None    # 归因报告摘要（judge 产出，报告/人审用）


# ---------- 端口协议 ----------

@runtime_checkable
class Driver(Protocol):
    manifest: "DriverManifest"

    def health(self) -> HealthReport: ...
    def locate(self, loc: Locator, ctx: ExecContext,
               timeout_ms: int | None = None) -> LocatedElement: ...
    def click(self, el: LocatedElement, ctx: ExecContext) -> None: ...
    def input_text(self, el: LocatedElement, text: str, ctx: ExecContext) -> None: ...
    def select(self, el: LocatedElement, value: str, ctx: ExecContext) -> None: ...
    def grab(self, rect: Rect | None, ctx: ExecContext) -> Screenshot: ...
    def wait_ready(self, ctx: ExecContext, timeout_ms: int) -> bool: ...
    def wait_gone(self, el: LocatedElement, ctx: ExecContext,
                  timeout_ms: int) -> bool: ...


@runtime_checkable
class EmulatorPort(Protocol):
    def call(self, cmd: str, args: dict, timeout_s: float = 10.0) -> dict: ...
    def load_scene(self, scene_id: str) -> None: ...
    def query_state(self, path: str) -> Any: ...
    def history(self, limit: int = 100) -> list[dict]: ...


@runtime_checkable
class KnowledgePort(Protocol):
    def prefetch(self, case_ids: list[str]) -> Any: ...
    def query_element(self, key: str) -> Any: ...
    def match_failure(self, sig: Any) -> Any: ...
    def save_failure(self, fk: Any) -> None: ...
    def feedback_router(self, key: str, driver: str, ok: bool, ms: int) -> None: ...
    def load_router_stats(self, target_key: str) -> dict: ...


@runtime_checkable
class RecorderPort(Protocol):
    def begin_run(self, env: dict) -> int: ...
    def begin_case(self, run_id: int, case: TestCase) -> int: ...
    def record_step(self, case_exec_id: int, res: StepResult,
                    attempt: int = 1) -> None: ...
    def record_attempts(self, case_exec_id: int, attempts: list[dict]) -> None: ...
    def finish_case(self, case_exec_id: int, status: str, sig: str | None,
                    kb_hit: int | None, duration_ms: int = 0,
                    root_cause: dict | None = None) -> None: ...
    def finish_run(self, run_id: int, stats: dict) -> None: ...
    def save_screenshot(self, run_id: int, case_exec_id: int, step_no: int,
                        phase: str, img: bytes) -> str: ...


@runtime_checkable
class LlmPort(Protocol):
    def parse_case(self, raw: dict) -> dict: ...
    def ground(self, image_b64: str, desc: str, region: Rect | None) -> dict: ...
    def verify_vision(self, image_b64: str, expect: str) -> dict: ...
    def judge_failure(self, ctx: dict) -> dict: ...


@runtime_checkable
class ZentaoPort(Protocol):
    def fetch_cases(self, product_id: int, since: Any = None) -> list: ...
    def create_testtask(self, spec: dict) -> int: ...
    def update_task_status(self, task_id: int, status: str, summary: dict) -> None: ...

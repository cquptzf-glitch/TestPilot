"""测试公共夹具 — 全部依赖用假实现替身，单测不触碰 GUI/网络/模拟器。

设计原则：
- ``cfg`` 夹具直接基于打包内 ``default.yaml`` 覆盖路径 -> 顺带校验配置项齐备；
- 数据库用真实 ``connect()``（临时文件）以覆盖 DDL/迁移/仓储真实行为；
- 驱动/模拟器/LLM/禅道用轻量替身，行为可控（可注入失败）以覆盖分支。
"""
import pytest
from testpilot.core.contracts import (DriverKind, HealthReport, LocatedElement,
                                      Screenshot)
from testpilot.core.dsl import Step, TestCase, TargetDesc, Locator  # noqa: F401
from testpilot.core.errors import ActionError, DeviceError, LocateError
from testpilot.core.registry import DriverManifest

# Valid PNGs: screenshot verification must exercise pixels, never malformed data.
def _pixel_png(value):
    import struct
    import zlib
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes([0, value]))) + chunk(b"IEND", b""))


PNG_1PX = _pixel_png(0)


# ---------- 环境隔离 ----------

@pytest.fixture(autouse=True)
def _neutralize_host_dpi(monkeypatch):
    """单测结果不得依赖**宿主显示器**：DPI 一律按"已感知 + 100%"处理。

    否则同一份代码在 100% 和 150% 的机器上跑出不同结果（坐标换算被真机缩放影响）。
    DPI 自身的行为由 ``tests/test_dpi.py`` 用假 windll 完整覆盖；
    需要验证坐标换算的用例在自己的测试里 monkeypatch ``dpi``。
    """
    from testpilot.infra import dpi

    monkeypatch.setattr(dpi, "_aware", True)
    monkeypatch.setattr(dpi, "_scale_cache", 1.0)
    yield
    dpi.reset_cache()


# ---------- 配置 ----------

@pytest.fixture()
def cfg(tmp_path):
    """基于打包 default.yaml，路径全部落到 tmp_path（顺带校验配置键齐备）。"""
    from testpilot.config.settings import load_settings

    c = load_settings()
    c["app"]["db"] = str(tmp_path / "testpilot.db")
    c["app"]["screenshot_root"] = str(tmp_path / "shots")
    c["app"]["report_root"] = str(tmp_path / "reports")
    c["vision"]["template_dir"] = str(tmp_path / "templates")
    c["log"]["per_run_file"] = False
    c["llm"]["api_base"] = ""
    c["vision"]["vlm"]["api_base"] = ""
    c["emulator"]["connect_timeout_s"] = 0.1
    return c


@pytest.fixture()
def db_conn(cfg):
    from testpilot.storage.db import connect
    conn = connect(cfg["app"]["db"])
    yield conn
    conn.close()


@pytest.fixture()
def mem_conn():
    """内存库 + 建表（每测试独立）。"""
    from testpilot.storage.db import migrate
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


# ---------- 假驱动 ----------

class FakeDriver:
    """满足 Driver 协议的最小实现；可注入定位/动作失败与文本读取结果。"""

    def __init__(self, name="uia", capabilities=None, fail_locate=False,
                 fail_action=False, text="OK", manifest=None):
        self.manifest = manifest or DriverManifest(
            kind="interaction", name=name, version="test",
            capabilities=frozenset(capabilities or {
                "click", "input", "select", "grab", "wait_ready", "wait_gone",
                "launch", "attach", "close", "read_text", "toggle", "keypress",
                "list_windows"}),
            entry="")
        self.fail_locate = fail_locate
        self.fail_action = fail_action
        self.text = text
        self.calls = []
        self.locate_calls = 0
        self.launched = None
        self.closed = None
        self.waited_gone = []

    def health(self):
        return HealthReport(ok=not self.fail_locate, detail=f"fake:{self.manifest.name}")

    def locate(self, loc, ctx, timeout_ms=None):
        ctx.abort.check()
        self.locate_calls += 1
        if self.fail_locate:
            raise LocateError(f"fake locate fail: {loc.type}", searched=str(loc))
        return LocatedElement(driver=DriverKind.UIA, handle=f"h:{loc.type}",
                              rect=(10, 20, 100, 30), confidence=1.0)

    def click(self, el, ctx):
        ctx.abort.check()
        self._act("click", el)

    def input_text(self, el, text, ctx):
        ctx.abort.check()
        self._act("input", el, text)

    def select(self, el, value, ctx):
        ctx.abort.check()
        self._act("select", el, value)

    def select_all_and_delete(self, el, ctx):
        """UIA 专属能力（clear 输入）；视觉驱动替身刻意不实现，用于能力门禁测试。"""
        ctx.abort.check()
        self._act("clear", el)

    def grab(self, rect, ctx):
        self.calls.append(("grab", rect))
        count = sum(1 for call in self.calls if call[0] == "grab")
        return Screenshot(png=_pixel_png(255 if count % 2 == 0 else 0), rect=rect)

    def wait_ready(self, ctx, timeout_ms):
        ctx.abort.check()
        self.calls.append(("wait_ready", timeout_ms))
        return True

    def wait_gone(self, el, ctx, timeout_ms):
        ctx.abort.check()
        self.calls.append(("wait_gone", timeout_ms))
        self.waited_gone.append(el)
        return True

    def read_text(self, el):
        return self.text

    def launch(self, spec, timeout_s=30):
        self.calls.append(("launch", spec))
        self.launched = spec
        return (0, 0, 800, 600)

    def attach(self, title_re, timeout_s=10):
        self.calls.append(("attach", title_re))
        self.launched = {"title_re": title_re}
        return (0, 0, 800, 600)

    def list_windows(self, limit=100):
        return [{"title": "Fake App", "handle": 1, "pid": 123,
                 "rect": [0, 0, 800, 600]}][:limit]

    def toggle(self, el, checked, ctx):
        ctx.abort.check()
        self._act("toggle", el, bool(checked))

    def keypress(self, el, keys, ctx):
        ctx.abort.check()
        self._act("keypress", el, keys)

    def close(self, mode="graceful"):
        self.calls.append(("close", mode))
        self.closed = mode

    def _act(self, kind, el, extra=None):
        if self.fail_action:
            raise ActionError(f"fake {kind} fail")
        self.calls.append((kind, extra))


class FlakyDriver(FakeDriver):
    """前 N 次定位失败、之后成功（覆盖重试/路由学习路径）。"""

    def __init__(self, fail_times=1, **kw):
        kw.setdefault("name", "uia")
        super().__init__(**kw)
        self.remaining = fail_times

    def locate(self, loc, ctx, timeout_ms=None):
        if self.remaining > 0:
            self.remaining -= 1
            raise LocateError(f"flake: 第 {self.remaining + 1} 次预置失败")
        return super().locate(loc, ctx, timeout_ms)


class FakeVisionDriver(FakeDriver):
    """不带 wait_gone/select/read_text/clear 能力的视觉驱动替身（能力门禁测试用）。"""

    # 视觉驱动没有"清空输入"能力（真实 VisionDriver 里完全没有该方法）
    select_all_and_delete = None

    def __init__(self, **kw):
        kw.setdefault("name", "vision")
        kw.setdefault("capabilities", {"click", "input", "grab"})
        super().__init__(**kw)


@pytest.fixture()
def fake_drivers():
    return {"uia": FakeDriver("uia"), "vision": FakeDriver("vision")}


# ---------- 假模拟器 ----------

class FakeEmulator:
    """模拟器替身：状态树支持 JSON 路径查询（与 A.3 契约一致）。"""

    def __init__(self, fail=False, state=None, caps=None):
        self.calls = []
        self.scenes = []
        self.fail = fail
        self.state = state if state is not None else {
            "state": {"run": "completed",
                      "summary": {"run": "completed", "grid": "a"}}}
        self._caps = caps if caps is not None else {
            "protocol": "1.0", "commands": ["load_scene", "query_state"],
            "scenes": ["normal_sample_a"], "terms": {}}
        self.connected = True

    def call(self, cmd, args, timeout_s=None):
        self.calls.append((cmd, args))
        if self.fail:
            raise DeviceError(f"fake device error: {cmd}")
        return {"ok": True, "data": {"cmd": cmd, "args": args}, "elapsed_ms": 7}

    def load_scene(self, scene_id):
        self.calls.append(("load_scene", {"scene_id": scene_id}))
        if self.fail:
            raise DeviceError(f"fake scene error: {scene_id}")
        self.scenes.append(scene_id)

    def query_state(self, path):
        # 记录调用：轨迹里"传给模拟器的 path 是否被加工"必须可断言（A.3 契约）
        self.calls.append(("query_state", {"path": path}))
        if self.fail:
            raise DeviceError("fake query_state error")
        node = self.state
        for part in [p for p in str(path).split("/") if p]:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    def history(self, limit=100):
        return self.calls[:limit]

    @property
    def capabilities(self):
        return self._caps

    def protocol_version(self):
        return self._caps.get("protocol", "1.0")

    def close(self):
        self.connected = False


@pytest.fixture()
def fake_emulator():
    return FakeEmulator()


# ---------- 假 LLM ----------

class FakeLlm:
    """可控的 LlmPort 替身：解析草稿 / 归因 / 视觉断言。"""

    def __init__(self, draft=None, judge=None, ground=None, vision=None,
                 fail_parse=False):
        self.draft = draft
        self.judge_result = judge
        self.ground_result = ground
        self.vision_result = vision
        self.fail_parse = fail_parse
        self.emulator_capabilities = None
        self.parse_calls = []
        self.stats = {"calls": 0, "tokens": 0, "ms": 0, "errors": 0}

    def parse_case(self, raw):
        self.parse_calls.append(raw)
        if self.fail_parse:
            raise RuntimeError("fake parse failure")
        if self.draft is not None:
            return self.draft
        return {
            "case_id": raw.get("case_id"),
            "title": raw.get("title", ""),
            "preconditions": ["模拟器场景: normal_sample_a"],
            "notes": "",
            "steps": [
                {"id": "s1", "action": "app.launch",
                 "source_text": "启动被测程序",
                 "args": {"exe": "target.exe", "title_re": ".*主窗口.*"}},
                {"id": "s2", "action": "ui.click",
                 "source_text": "点击开始检测",
                 "target": {"key": "btn.start_detect",
                            "primary": {"type": "uia", "name": "开始检测",
                                        "control_type": "Button"}},
                 "verify": {"type": "screenshot_diff"}},
            ],
            "prompt_version": "parse_case_v1",
            "uncertain_count": 0,
        }

    def ground(self, image_b64, desc, region):
        return self.ground_result or {"bbox": [10, 10, 40, 20], "confidence": 0.9}

    def verify_vision(self, image_b64, expect):
        return self.vision_result or {"pass": True, "confidence": 0.95,
                                      "reason": "ok"}

    def judge_failure(self, ctx):
        return self.judge_result or {"category": "uia_change", "confidence": 0.7,
                                     "evidence": "定位失败", "suggestion": "补 fallback"}

    def usage_snapshot(self):
        return dict(self.stats)


@pytest.fixture()
def fake_llm():
    return FakeLlm()


# ---------- 假禅道 ----------

class FakeZentao:
    def __init__(self, cases=None, configured=False, fail_create=False,
                 fail_writeback=False):
        self._cases = cases if cases is not None else {
            1: _raw_case(1, "常规样本A检测全流程", "2026-01-01T00:00:00"),
            2: _raw_case(2, "参数修改用例", "2026-01-02T00:00:00"),
        }
        self.configured = configured
        self.fail_create = fail_create
        self.fail_writeback = fail_writeback
        self.created = []
        self.writebacks = []

    def is_configured(self):
        return self.configured

    def fetch_case(self, zentao_case_id):
        from testpilot.core.errors import ZentaoError
        if int(zentao_case_id) not in self._cases:
            raise ZentaoError(f"用例不存在: {zentao_case_id}")
        return dict(self._cases[int(zentao_case_id)])

    def fetch_cases(self, product_id=None, since=None):
        out = []
        for raw in self._cases.values():
            if since and str(raw.get("updated_at", "")) <= str(since):
                continue
            out.append(dict(raw))
        return out

    def create_testtask(self, spec):
        from testpilot.core.errors import ZentaoError
        if self.fail_create:
            raise ZentaoError("fake create_testtask fail")
        self.created.append(spec)
        return 9000 + len(self.created)

    def update_task_status(self, task_id, status, summary, retries=3):
        from testpilot.core.errors import ZentaoError
        if self.fail_writeback:
            raise ZentaoError("fake writeback fail")
        self.writebacks.append({"task_id": task_id, "status": status,
                                "summary": dict(summary)})


def _raw_case(zid, title, updated):
    return {
        "case_id": f"TC-{zid}", "zentao_case_id": zid, "title": title,
        "steps": [{"desc": "启动被测程序", "expect": ""},
                  {"desc": "点击开始检测按钮", "expect": "出现结果表格"}],
        "steps_text": "1. 启动被测程序\n2. 点击开始检测按钮  [期望: 出现结果表格]",
        "product": 1, "module": 0, "updated_at": updated,
    }


@pytest.fixture()
def fake_zentao():
    return FakeZentao()


# ---------- DSL 构造助手 ----------

def make_step(key="btn.start_detect", action="ui.click", verify=None,
              source_text="点击开始检测", **kw):
    step = {
        "id": kw.pop("id", "s1"), "action": action,
        "source_text": source_text,
        "target": {"key": key,
                   "primary": {"type": "uia", "name": "开始检测",
                               "control_type": "Button"},
                   "fallback": {"type": "visual", "desc": "开始检测按钮"}},
    }
    if verify:
        step["verify"] = verify
    step.update(kw)
    return Step.model_validate(step)


def make_case(case_id="TC-1", steps=None, version=1, preconditions=None):
    return TestCase(case_id=case_id, zentao_case_id=int(case_id.split("-")[-1])
                    if case_id.split("-")[-1].isdigit() else None,
                    title=f"用例 {case_id}", version=version,
                    preconditions=preconditions or [],
                    steps=steps or [
                        make_step(action="app.launch", key="app.window",
                                  args={"exe": "target.exe"},
                                  source_text="启动被测程序"),
                        make_step(),
                    ])


@pytest.fixture()
def active_case_factory(db_conn):
    """把用例写入本地库并置为 active，返回 case_id。"""
    from testpilot.storage import repos

    def _make(case_id="TC-1", steps=None, preconditions=None):
        repos.create_case(db_conn, case_id=case_id,
                          zentao_case_id=int(case_id.split("-")[-1]),
                          title=f"用例 {case_id}", raw={"steps_text": "x"},
                          zentao_updated_at="2026-01-01T00:00:00")
        case = make_case(case_id, steps=steps, preconditions=preconditions)
        repos.save_case_steps(db_conn, case_id,
                              steps=[s.model_dump(mode="json") for s in case.steps],
                              preconditions=case.preconditions)
        repos.activate_case(db_conn, case_id)
        return case_id

    return _make


@pytest.fixture()
def registry():
    from testpilot.app.cli import build_registry
    return build_registry()


@pytest.fixture()
def orch_factory(cfg, db_conn, tmp_path, registry):
    """构造 TaskOrchestrator（全部依赖替身），供编排/报告/RPC 测试复用。"""

    def _make(*, zentao=None, drivers=None, emulator=None, llm=None, bus=None,
              recorder=None):
        from testpilot.engine.orchestrator import TaskOrchestrator
        from testpilot.storage.recorder import Recorder

        return TaskOrchestrator(
            cfg, registry, conn=db_conn,
            recorder=recorder or Recorder(db_conn, tmp_path / "shots"),
            zentao=zentao or FakeZentao(), llm=llm or FakeLlm(),
            emulator=emulator or FakeEmulator(),
            drivers=drivers if drivers is not None else {"uia": FakeDriver("uia")},
            bus=bus)

    return _make


def prepare_case(orch, zentao_ids=(1,)):
    """拉取 + 解析 + 人审通过，返回可执行的 case_id 列表。"""
    out = orch.cases.pull(list(zentao_ids))
    ids = [c["case_id"] for c in out["cases"]]
    for cid in ids:
        orch.cases.parse(cid)
        orch.cases.accept(cid)
    return ids

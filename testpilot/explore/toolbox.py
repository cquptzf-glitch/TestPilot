"""探索工具层 — 把既有驱动与模拟器包装成 agent 可调用工具。

原则：
1. 业务能力不重写：动作仍复用 uia / vision / emulator；
2. 主回归链路不依赖本模块：确定性 DSL Executor 保持原样；
3. 工具白名单：只暴露当前依赖真实可用的能力；
4. Agent 观察与回归定位分离：element_index 只在 Explore/MCP 会话内短期有效，
   最终固化回 DSL 时仍应使用稳定 locator。
"""
import base64
import time
from dataclasses import dataclass, field

from testpilot.core.contracts import ExecContext
from testpilot.core.dsl import Locator, LocatorType
from testpilot.core.errors import ActionError, TestPilotError
from testpilot.explore.computer_use import ComputerUseSession


class ToolError(TestPilotError):
    """探索工具调用失败（参数非法 / 依赖未装配 / 定位或动作失败）。"""


@dataclass
class ToolSpec:
    """一个工具声明；to_dict() 直接生成 MCP tools/list 条目。"""
    name: str
    description: str
    params: dict = field(default_factory=dict)
    needs: str = ""            # "" | "uia" | "driver" | "emulator"
    capability: str = ""       # 非空时按 DriverManifest.capabilities 精确裁剪

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.params}


_LOCATE_PROPS = {
    "key": {"type": "string",
            "description": "可选稳定语义键，供会话记录/固化回 DSL 使用"},
    "driver": {"type": "string", "enum": ["uia", "vision"]},
    "element_index": {
        "type": "integer",
        "minimum": 1,
        "description": "ui.observe/ui.query 返回的会话内短期元素索引；提供后无需 locator 字段",
    },
    "snapshot_id": {
        "type": "integer",
        "minimum": 1,
        "description": "可选：与 element_index 同时校验，防止跨快照误操作",
    },
    "name": {"type": "string", "description": "UIA accessibleName / 控件文本"},
    "automation_id": {"type": "string"},
    "control_type": {"type": "string", "description": "Button/Edit/Table..."},
    "desc": {"type": "string", "description": "视觉语义描述（vision 驱动）"},
    "template": {"type": "string", "description": "模板图相对路径（template）"},
    "index": {"type": "integer", "description": "多匹配时取第几个"},
    "timeout_ms": {"type": "integer"},
}


def _locate_schema(extra: dict | None = None, required: tuple = ()) -> dict:
    props = dict(_LOCATE_PROPS)
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(required)}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("app.list", "列出当前桌面可见顶层窗口", {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
    }, needs="uia", capability="list_windows"),
    ToolSpec("app.launch", "启动被测程序；也允许仅 title_re 附加到已运行窗口", {
        "type": "object",
        "properties": {
            "exe": {"type": "string", "description": "被测程序路径；与 title_re 至少一个"},
            "args": {"type": "array", "items": {"type": "string"}},
            "title_re": {"type": "string", "description": "主窗口标题正则"},
            "timeout_s": {"type": "number"},
        },
    }, needs="uia", capability="launch"),
    ToolSpec("app.attach", "按窗口标题正则附加到已运行程序", {
        "type": "object",
        "properties": {
            "title_re": {"type": "string"},
            "timeout_s": {"type": "number", "default": 10},
        },
        "required": ["title_re"],
    }, needs="uia", capability="attach"),
    ToolSpec("app.wait_ready", "等待被测窗口就绪", {
        "type": "object", "properties": {"timeout_ms": {"type": "integer"}},
    }, needs="uia", capability="wait_ready"),
    ToolSpec("app.close", "关闭被测程序", {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["graceful", "force"]}},
    }, needs="uia", capability="close"),

    ToolSpec("ui.observe", "读取当前 UI 状态；AX 与截图可按需选择", {
        "type": "object",
        "properties": {
            "include_ax": {"type": "boolean", "default": True},
            "include_screenshot": {"type": "boolean", "default": False},
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 5000,
                          "default": 300},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 64,
                          "default": 12},
        },
    }, needs="uia"),
    ToolSpec("ui.query", "按文本/AutomationId/控件类型定向查找 UIA 元素，不截图", {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "automation_id": {"type": "string"},
            "control_type": {"type": "string"},
            "exact": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                      "default": 20},
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 5000,
                          "default": 500},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 64,
                          "default": 16},
        },
    }, needs="uia"),

    ToolSpec("ui.locate", "定位控件（返回矩形、命中驱动与耗时）",
             _locate_schema(), needs="driver"),
    ToolSpec("ui.read", "读取控件当前文本/值",
             _locate_schema(), needs="uia", capability="read_text"),
    ToolSpec("ui.click", "定位/解析元素并点击控件",
             _locate_schema(), needs="driver"),
    ToolSpec("ui.input", "定位/解析元素并输入文本",
             _locate_schema({"text": {"type": "string"},
                             "clear": {"type": "boolean"}},
                            required=("text",)), needs="driver"),
    ToolSpec("ui.set_value", "通过 UIA 可访问值写入并写后回读校验",
             _locate_schema({
                 "value": {"type": "string"},
                 "verify_timeout_ms": {"type": "integer", "default": 800},
             }, required=("value",)), needs="uia"),
    ToolSpec("ui.select", "定位/解析元素并选择下拉项",
             _locate_schema({"value": {"type": "string"}},
                            required=("value",)), needs="uia", capability="select"),
    ToolSpec("ui.toggle", "设置复选/开关控件状态并验证最终状态",
             _locate_schema({"checked": {"type": "boolean"}},
                            required=("checked",)), needs="uia", capability="toggle"),
    ToolSpec("ui.keypress", "向目标控件发送 pywinauto 键序列",
             _locate_schema({"keys": {"type": "string"}},
                            required=("keys",)), needs="uia", capability="keypress"),

    ToolSpec("device.scene", "加载模拟器场景（前置依赖）", {
        "type": "object", "properties": {"scene_id": {"type": "string"}},
        "required": ["scene_id"],
    }, needs="emulator"),
    ToolSpec("device.call", "调用模拟器指令并返回回执", {
        "type": "object",
        "properties": {"cmd": {"type": "string"}, "args": {"type": "object"},
                       "timeout_s": {"type": "number"}},
        "required": ["cmd"],
    }, needs="emulator"),
    ToolSpec("device.state", "查询模拟器状态（JSON 路径）", {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }, needs="emulator"),
    ToolSpec("screen.grab", "截取当前画面（MCP 下会作为 image block 返回）", {
        "type": "object", "properties": {},
    }, needs="driver"),
)


class ToolBox:
    """工具容器：驱动 + 模拟器 + ComputerUse 会话。"""

    def __init__(self, drivers: dict | None = None, emulator=None,
                 ctx: ExecContext | None = None):
        self.drivers = dict(drivers or {})
        self.emulator = emulator
        self.ctx = ctx or ExecContext(run_id=0, case_exec_id=0)
        self.computer = ComputerUseSession(self.drivers, self.ctx)

    def available(self, spec: ToolSpec) -> bool:
        if spec.needs == "emulator":
            return self.emulator is not None
        if spec.needs == "uia":
            drv = self.drivers.get("uia")
            if drv is None:
                return False
            if spec.capability:
                caps = getattr(getattr(drv, "manifest", None), "capabilities", frozenset())
                return spec.capability in caps
            return True
        if spec.needs == "driver":
            candidates = [self.drivers.get(n) for n in ("uia", "vision")]
            if not spec.capability:
                return any(d is not None for d in candidates)
            return any(spec.capability in getattr(getattr(d, "manifest", None),
                                                  "capabilities", frozenset())
                       for d in candidates if d is not None)
        return True

    def list_tools(self) -> list[dict]:
        return [s.to_dict() for s in TOOL_SPECS if self.available(s)]

    def invoke(self, name: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        spec = next((s for s in TOOL_SPECS if s.name == name), None)
        if spec is None:
            raise ToolError(f"未知工具: {name}")
        if not self.available(spec):
            raise ToolError(f"工具 {name} 依赖未装配（needs={spec.needs}）")
        handler = getattr(self, "_" + name.replace(".", "_"), None)
        if not callable(handler):
            raise ToolError(f"工具 {name} 未实现")
        return handler(args)

    def reset_session(self) -> None:
        self.computer.reset()

    # ---------- app.* ----------

    def _app_list(self, args) -> dict:
        drv = self._driver(args, default="uia")
        fn = getattr(drv, "list_windows", None)
        if not callable(fn):
            raise ToolError("UIA 驱动不支持窗口枚举")
        windows = fn(int(args.get("limit", 100)))
        return {"windows": windows, "count": len(windows)}

    def _app_launch(self, args) -> dict:
        drv = self._driver(args, default="uia")
        exe = str(args.get("exe") or "").strip()
        title_re = str(args.get("title_re") or "").strip()
        if not exe and not title_re:
            raise ToolError("app.launch 至少需要 exe 或 title_re")
        spec = {}
        if exe:
            spec["exe"] = exe
        if args.get("args"):
            spec["args"] = [str(a) for a in args["args"]]
        if title_re:
            spec["title_re"] = title_re
        rect = drv.launch(spec, timeout_s=float(args.get("timeout_s", 30)))
        self.ctx.window_rect = tuple(rect) if rect else None
        self.computer.reset()
        return {"window_rect": list(rect) if rect else None,
                "mode": "launch" if exe else "attach"}

    def _app_attach(self, args) -> dict:
        drv = self._driver(args, default="uia")
        attach = getattr(drv, "attach", None)
        if not callable(attach):
            raise ToolError("UIA 驱动不支持 attach")
        title_re = str(_req(args, "title_re"))
        rect = attach(title_re, timeout_s=float(args.get("timeout_s", 10)))
        self.ctx.window_rect = tuple(rect) if rect else None
        self.computer.reset()
        return {"window_rect": list(rect) if rect else None, "mode": "attach",
                "title_re": title_re}

    def _app_wait_ready(self, args) -> dict:
        drv = self._driver(args, default="uia")
        timeout = int(args.get("timeout_ms", 10000))
        if not drv.wait_ready(self.ctx, timeout):
            raise ToolError("被测程序就绪超时")
        return {"ready": True, "timeout_ms": timeout}

    def _app_close(self, args) -> dict:
        drv = self._driver(args, default="uia")
        mode = str(args.get("mode", "graceful"))
        drv.close(mode)
        self.ctx.window_rect = None
        self.computer.reset()
        return {"closed": mode}

    # ---------- observation ----------

    def _ui_observe(self, args) -> dict:
        return self.computer.observe(
            include_ax=bool(args.get("include_ax", True)),
            include_screenshot=bool(args.get("include_screenshot", False)),
            max_nodes=int(args.get("max_nodes", 300)),
            max_depth=int(args.get("max_depth", 12)),
        )

    def _ui_query(self, args) -> dict:
        return self.computer.query(
            text=args.get("text"),
            automation_id=args.get("automation_id"),
            control_type=args.get("control_type"),
            exact=bool(args.get("exact", False)),
            limit=int(args.get("limit", 20)),
            max_nodes=int(args.get("max_nodes", 500)),
            max_depth=int(args.get("max_depth", 16)),
        )

    # ---------- ui.* ----------

    def _ui_locate(self, args) -> dict:
        t0 = time.monotonic()
        drv, el, meta = self._resolve(args)
        return {"rect": list(el.rect), "driver": drv.manifest.name,
                "confidence": el.confidence, "target": meta,
                "ms": int((time.monotonic() - t0) * 1000)}

    def _ui_read(self, args) -> dict:
        drv, el, meta = self._resolve(args, force_driver="uia")
        read = getattr(drv, "read_text", None)
        if not callable(read):
            raise ToolError("UIA 驱动不支持 read_text")
        value = read(el)
        return {"rect": list(el.rect), "driver": "uia", "target": meta,
                "value": value, **_session_ref(args)}

    def _ui_click(self, args) -> dict:
        drv, el, meta = self._resolve(args)
        drv.click(el, self.ctx)
        return {"rect": list(el.rect), "driver": drv.manifest.name,
                "target": meta, **_session_ref(args)}

    def _ui_input(self, args) -> dict:
        drv, el, meta = self._resolve(args)
        if args.get("clear"):
            clear = getattr(drv, "select_all_and_delete", None)
            if not callable(clear):
                raise ToolError(f"驱动 {drv.manifest.name} 不支持 clear 输入")
            clear(el, self.ctx)
        drv.input_text(el, str(_req(args, "text")), self.ctx)
        out = {"rect": list(el.rect), "driver": drv.manifest.name,
               "target": meta, **_session_ref(args)}
        if drv.manifest.name == "uia":
            out["read_back"] = _try_read(drv, el)
        return out

    def _ui_set_value(self, args) -> dict:
        drv, el, meta = self._resolve(args, force_driver="uia")
        if drv.manifest.name != "uia":
            raise ToolError("ui.set_value 仅支持 UIA 驱动")
        out = self.computer.set_value(
            el,
            str(_req(args, "value")),
            timeout_ms=int(args.get("verify_timeout_ms", 800)),
        )
        return {"rect": list(el.rect), "driver": "uia", "target": meta,
                **_session_ref(args), **out}

    def _ui_select(self, args) -> dict:
        drv, el, meta = self._resolve(args, force_driver="uia")
        drv.select(el, str(_req(args, "value")), self.ctx)
        return {"rect": list(el.rect), "driver": drv.manifest.name,
                "target": meta, "read_back": _try_read(drv, el),
                **_session_ref(args)}

    def _ui_toggle(self, args) -> dict:
        drv, el, meta = self._resolve(args, force_driver="uia")
        toggle = getattr(drv, "toggle", None)
        if not callable(toggle):
            raise ToolError("UIA 驱动不支持 toggle")
        checked = bool(_req_bool(args, "checked"))
        toggle(el, checked, self.ctx)
        return {"rect": list(el.rect), "driver": "uia", "target": meta,
                "checked": checked, "read_back": _try_read(drv, el),
                **_session_ref(args)}

    def _ui_keypress(self, args) -> dict:
        drv, el, meta = self._resolve(args, force_driver="uia")
        keypress = getattr(drv, "keypress", None)
        if not callable(keypress):
            raise ToolError("UIA 驱动不支持 keypress")
        keys = str(_req(args, "keys"))
        keypress(el, keys, self.ctx)
        return {"rect": list(el.rect), "driver": "uia", "target": meta,
                "keys": keys, **_session_ref(args)}

    # ---------- device.* / screen.* ----------

    def _device_scene(self, args) -> dict:
        self._require_emulator()
        scene = str(_req(args, "scene_id"))
        self.emulator.load_scene(scene)
        return {"scene_id": scene, "ok": True}

    def _device_call(self, args) -> dict:
        self._require_emulator()
        resp = self.emulator.call(str(_req(args, "cmd")),
                                  dict(args.get("args") or {}),
                                  args.get("timeout_s"))
        return {"ok": bool(resp.get("ok")), "data": resp.get("data"),
                "elapsed_ms": resp.get("elapsed_ms")}

    def _device_state(self, args) -> dict:
        self._require_emulator()
        path = str(_req(args, "path"))
        return {"path": path, "state": self.emulator.query_state(path)}

    def _screen_grab(self, args) -> dict:
        if args.get("driver"):
            drv = self._driver(args, default="vision")
        else:
            drv = next(
                (d for d in self.drivers.values()
                 if "grab" in getattr(getattr(d, "manifest", None),
                                      "capabilities", frozenset())),
                None
            )
            if drv is None:
                raise ToolError("没有具备截图能力（grab）的驱动")
        shot = drv.grab(self.ctx.window_rect, self.ctx)
        return {"png_base64": base64.b64encode(shot.png).decode("ascii"),
                "bytes": len(shot.png),
                "rect": list(shot.rect) if shot.rect is not None else None}

    # ---------- internal ----------

    def _driver(self, args, default: str):
        name = str(args.get("driver") or default)
        drv = self.drivers.get(name)
        if drv is None:
            raise ToolError(f"驱动未装配: {name}")
        return drv

    def _require_emulator(self):
        if self.emulator is None:
            raise ToolError("模拟器端口未装配")

    def _resolve(self, args: dict, force_driver: str | None = None):
        if args.get("element_index") is not None:
            if force_driver and force_driver != "uia":
                raise ToolError("element_index 仅支持 UIA 会话元素")
            if args.get("driver") not in (None, "", "uia"):
                raise ToolError("element_index 只能与 UIA 驱动一起使用")
            drv = self._driver({"driver": "uia"}, default="uia")
            el, record = self.computer.resolve(
                int(args["element_index"]),
                snapshot_id=int(args["snapshot_id"])
                if args.get("snapshot_id") is not None else None,
            )
            return drv, el, _stable_meta(record)

        drv, loc = self._locator(args, force_driver=force_driver)
        el = drv.locate(
            loc, self.ctx,
            timeout_ms=int(args.get("timeout_ms", 10000)))
        meta = _stable_meta({
            "name": loc.name,
            "automation_id": loc.automation_id,
            "control_type": loc.control_type,
            "desc": loc.desc,
            "template": loc.template,
        })
        return drv, el, meta

    def _locator(self, args, force_driver: str | None = None) -> tuple:
        driver_name = force_driver or args.get("driver")
        if not driver_name:
            driver_name = "uia" if "uia" in self.drivers else "vision"
        drv = self.drivers.get(driver_name)
        if drv is None:
            raise ToolError(f"驱动未装配: {driver_name}")

        raw_type = args.get("type")
        if not raw_type:
            if driver_name == "uia":
                raw_type = "uia"
            elif args.get("template"):
                raw_type = "template"
            else:
                raw_type = "visual"
        try:
            ltype = LocatorType(raw_type)
        except ValueError:
            raise ToolError(f"未知定位类型: {raw_type}") from None

        if ltype == LocatorType.UIA and not (
                args.get("name") or args.get("automation_id")
                or args.get("control_type")):
            raise ToolError(
                "uia 定位需要 name / automation_id / control_type，"
                "或直接提供 element_index")
        if ltype == LocatorType.VISUAL and not args.get("desc"):
            raise ToolError("visual 定位需要 desc")
        if ltype == LocatorType.TEMPLATE and not args.get("template"):
            raise ToolError("template 定位需要 template")

        loc = Locator(type=ltype, name=args.get("name"),
                      automation_id=args.get("automation_id"),
                      control_type=args.get("control_type"),
                      desc=args.get("desc"), template=args.get("template"),
                      index=int(args.get("index", 0) or 0))
        return drv, loc


def _stable_meta(data: dict | None) -> dict:
    data = data or {}
    out = {}
    for key in ("name", "automation_id", "control_type", "desc", "template",
                "stable_key", "selector"):
        value = data.get(key)
        if value not in (None, ""):
            out[key] = value
    if "selector" not in out:
        selector = _selector_from_meta(out)
        if selector:
            out["selector"] = selector
    return out


def _selector_from_meta(data: dict) -> dict | None:
    if data.get("automation_id"):
        out = {"type": "uia", "automation_id": data["automation_id"]}
        if data.get("control_type"):
            out["control_type"] = data["control_type"]
        return out
    if data.get("name"):
        out = {"type": "uia", "name": data["name"]}
        if data.get("control_type"):
            out["control_type"] = data["control_type"]
        return out
    if data.get("template"):
        return {"type": "template", "template": data["template"]}
    if data.get("desc"):
        return {"type": "visual", "desc": data["desc"]}
    return None


def _session_ref(args: dict) -> dict:
    return {"element_index": args.get("element_index"),
            "snapshot_id": args.get("snapshot_id")}


def _try_read(drv, el):
    read = getattr(drv, "read_text", None)
    if not callable(read):
        return None
    try:
        return read(el)
    except (ActionError, Exception):
        return None


def _req(args: dict, key: str):
    value = args.get(key)
    if value is None or value == "":
        raise ToolError(f"缺少必填参数: {key}")
    return value


def _req_bool(args: dict, key: str) -> bool:
    if key not in args or not isinstance(args[key], bool):
        raise ToolError(f"参数 {key} 必须是 boolean")
    return args[key]

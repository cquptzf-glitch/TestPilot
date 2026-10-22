"""探索工具层（M5+ piagent）— 把既有驱动与模拟器包装成 agent 可调用工具。

设计 11.1 的三条约束在这里落地：
1. **业务能力零重写**：工具只提供"名字 + JSON Schema + 分发"，执行仍走
   uia / vision / emulator 的既有实现；
2. **主链路不依赖**：确定性回归仍由 DSL 引擎负责，本模块是可选的探索上层；
3. **能力白名单**：``list_tools()`` 即 agent 可见的全部动作（防模型幻觉出不存在的工具）。

工具结果统一为 JSON 可序列化 dict；失败抛 ``ToolError``（agent 可据此重规划）。
"""
import base64
import time
from dataclasses import dataclass, field

from testpilot.core.contracts import ExecContext
from testpilot.core.dsl import Locator, LocatorType
from testpilot.core.errors import TestPilotError


class ToolError(TestPilotError):
    """探索工具调用失败（参数非法 / 依赖未装配 / 定位或动作失败）。"""


@dataclass
class ToolSpec:
    """一个工具的声明（``to_dict`` 即 MCP ``tools/list`` 的条目形态）。"""
    name: str
    description: str
    params: dict = field(default_factory=dict)
    needs: str = ""            # "" | "uia" | "driver" | "emulator"

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.params}


_LOCATE_PROPS = {
    "key": {"type": "string",
            "description": "可选：稳定语义键（供会话记录与固化回 DSL 的 target.key，"
                           "定位本身不消费）"},
    "driver": {"type": "string", "enum": ["uia", "vision"]},
    "name": {"type": "string", "description": "UIA accessibleName / 控件文本"},
    "automation_id": {"type": "string"},
    "control_type": {"type": "string", "description": "Button/Edit/Table..."},
    "desc": {"type": "string", "description": "视觉语义描述（vision 驱动）"},
    "template": {"type": "string", "description": "模板图相对路径（template）"},
    "index": {"type": "integer", "description": "多匹配时取第几个"},
    "timeout_ms": {"type": "integer"},
}


def _locate_schema(action_desc: str, extra: dict | None = None,
                   required: tuple = ()) -> dict:
    props = dict(_LOCATE_PROPS)
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(required)}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("app.launch", "启动被测程序并返回窗口矩形", {
        "type": "object",
        "properties": {
            "exe": {"type": "string", "description": "被测程序路径"},
            "args": {"type": "array", "items": {"type": "string"}},
            "title_re": {"type": "string", "description": "主窗口标题正则"},
            "timeout_s": {"type": "number"},
        },
        "required": ["exe"],
    }, needs="uia"),
    ToolSpec("app.wait_ready", "等待被测窗口就绪", {
        "type": "object", "properties": {"timeout_ms": {"type": "integer"}},
    }, needs="uia"),
    ToolSpec("app.close", "关闭被测程序", {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["graceful", "force"]}},
    }, needs="uia"),
    ToolSpec("ui.locate", "定位控件（返回矩形、命中驱动与耗时）",
             _locate_schema("定位"), needs="driver"),
    ToolSpec("ui.click", "定位并点击控件",
             _locate_schema("点击"), needs="driver"),
    ToolSpec("ui.input", "定位并输入文本",
             _locate_schema("输入", {"text": {"type": "string"},
                                     "clear": {"type": "boolean"}},
                            required=("text",)), needs="driver"),
    ToolSpec("ui.select", "定位并选择下拉项",
             _locate_schema("选择", {"value": {"type": "string"}},
                            required=("value",)), needs="driver"),
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
    ToolSpec("screen.grab", "截取当前画面（返回 PNG base64）", {
        "type": "object", "properties": {},
    }, needs="driver"),
)


class ToolBox:
    """工具容器：驱动 + 模拟器 + 执行上下文（取消语义与主链路一致）。"""

    def __init__(self, drivers: dict | None = None, emulator=None,
                 ctx: ExecContext | None = None):
        self.drivers = dict(drivers or {})
        self.emulator = emulator
        self.ctx = ctx or ExecContext(run_id=0, case_exec_id=0)

    # ---------- 对外 ----------

    def available(self, spec: ToolSpec) -> bool:
        if spec.needs == "emulator":
            return self.emulator is not None
        if spec.needs == "uia":
            return "uia" in self.drivers
        if spec.needs == "driver":
            return any(n in self.drivers for n in ("uia", "vision"))
        return True

    def list_tools(self) -> list[dict]:
        """可用工具清单（依赖未装配的工具不暴露，避免 agent 调用必然失败）。"""
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

    # ---------- app.* ----------

    def _app_launch(self, args) -> dict:
        drv = self._driver(args, default="uia")
        spec = {"exe": _req(args, "exe")}
        if args.get("args"):
            spec["args"] = [str(a) for a in args["args"]]
        if args.get("title_re"):
            spec["title_re"] = str(args["title_re"])
        rect = drv.launch(spec, timeout_s=float(args.get("timeout_s", 30)))
        self.ctx.window_rect = tuple(rect) if rect else None
        return {"window_rect": list(rect) if rect else None}

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
        return {"closed": mode}

    # ---------- ui.* ----------

    def _ui_locate(self, args) -> dict:
        drv, loc = self._locator(args)
        t0 = time.monotonic()
        el = drv.locate(loc, self.ctx,
                        timeout_ms=int(args.get("timeout_ms", 10000)))
        return {"rect": list(el.rect), "driver": drv.manifest.name,
                "confidence": el.confidence,
                "ms": int((time.monotonic() - t0) * 1000)}

    def _ui_click(self, args) -> dict:
        drv, loc = self._locator(args)
        el = drv.locate(loc, self.ctx,
                        timeout_ms=int(args.get("timeout_ms", 10000)))
        drv.click(el, self.ctx)
        return {"rect": list(el.rect), "driver": drv.manifest.name}

    def _ui_input(self, args) -> dict:
        drv, loc = self._locator(args)
        el = drv.locate(loc, self.ctx,
                        timeout_ms=int(args.get("timeout_ms", 10000)))
        if args.get("clear"):
            clear = getattr(drv, "select_all_and_delete", None)
            if not callable(clear):
                raise ToolError(f"驱动 {drv.manifest.name} 不支持 clear 输入")
            clear(el, self.ctx)
        drv.input_text(el, str(_req(args, "text")), self.ctx)
        return {"rect": list(el.rect), "driver": drv.manifest.name}

    def _ui_select(self, args) -> dict:
        drv, loc = self._locator(args)
        el = drv.locate(loc, self.ctx,
                        timeout_ms=int(args.get("timeout_ms", 10000)))
        drv.select(el, str(_req(args, "value")), self.ctx)
        return {"rect": list(el.rect), "driver": drv.manifest.name}

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
            # 默认驱动按"谁真的具备 grab 能力"挑：只装配 uia（无 cv2 环境）时
            # 硬点 vision 必然 ToolError，与 available() 的判定自相矛盾
            drv = next(
                (d for n, d in self.drivers.items()
                 if "grab" in getattr(getattr(d, "manifest", None),
                                      "capabilities", frozenset())),
                None)
            if drv is None:
                raise ToolError("没有具备截图能力（grab）的驱动")
        shot = drv.grab(self.ctx.window_rect, self.ctx)
        return {"png_base64": base64.b64encode(shot.png).decode("ascii"),
                "bytes": len(shot.png)}

    # ---------- 内部 ----------

    def _driver(self, args, default: str):
        name = str(args.get("driver") or default)
        drv = self.drivers.get(name)
        if drv is None:
            raise ToolError(f"驱动未装配: {name}")
        return drv

    def _require_emulator(self):
        if self.emulator is None:
            raise ToolError("模拟器端口未装配")

    def _locator(self, args) -> tuple:
        driver_name = args.get("driver")
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

        if ltype == LocatorType.UIA and not (args.get("name")
                                             or args.get("automation_id")):
            raise ToolError("uia 定位需要 name 或 automation_id")
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


def _req(args: dict, key: str):
    value = args.get(key)
    if value is None or value == "":
        raise ToolError(f"缺少必填参数: {key}")
    return value

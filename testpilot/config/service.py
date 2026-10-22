"""配置读写服务 —— 用户覆盖层（``data/config.override.yaml``），供设置页安全地改配置。

**为什么需要覆盖层而不是直接改 default.yaml**

1. `default.yaml` 是**随包资源**：安装到 `Program Files` 后目录只读，而且升级会覆盖；
   用户改的东西必须落在**可写数据根**（`TESTPILOT_HOME`）里；
2. 覆盖层与默认值**分离**，升级后"默认值变了、用户改动保留"是自然结果
   （默认值在代码里演进，覆盖层只存差异）；
3. 改坏了能一键回退（删覆盖层即回默认）。

**为什么要有 schema 校验**

`default.yaml` 的键是引擎各模块直接 `cfg[...]` 取的，写进非法值不会立刻报错，
而是在**下次运行时**变成莫名其妙的失败（典型"静默失效"）。
所以写入前必须逐项校验：类型、枚举、范围、以及**键是否真实存在**
（防"界面上能改、实际没人读"的假配置 —— 见 `tests/test_settings.py` 的配置漂移门禁）。

**生效时机**：覆盖层在引擎启动时读入。改完配置需要**重启引擎**才生效，
`apply` 返回里显式带 `restart_required: True`，前端据此提示，不做"看起来生效了"的假象。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import yaml

log = logging.getLogger("testpilot.settings")

OVERRIDE_NAME = "config.override.yaml"

# ---------------------------------------------------------------- 可编辑字段

# 字段规格：path -> (类型, 约束说明)
# **只暴露"改了真的有用"的字段**：每一项都在引擎代码里有读取点，
# 由 tests/test_settings.py 的 key-spec 门禁反向校验（防止页面出现死配置）。
EDITABLE: dict[str, dict] = {
    # 被测程序
    "app.test_app_exe": {
        "type": "string", "default": "",
        "label": "被测程序主 exe",
        "hint": "填了会自动读文件版本写入环境快照（归因可比性）；留空则不探测",
    },
    "app.test_app_version": {
        "type": "string", "default": "",
        "label": "被测程序版本（手工覆盖）",
        "hint": "留空=按上面的 exe 探测；探测不到时手工填写",
    },
    # 驱动路由
    "driver.mode": {
        "type": "enum", "default": "auto",
        "enum": ["auto", "uia_first", "vision_first", "uia_only", "vision_only"],
        "label": "全局驱动模式",
        "hint": "auto/uia_first=UIA 优先；vision_first=视觉优先；*_only=只用一种（方案级与步骤级可覆盖）",
    },
    "driver.vision_min_confidence": {
        "type": "number", "default": 0.70, "min": 0.0, "max": 1.0, "step": 0.05,
        "label": "视觉定位最低置信度",
        "hint": "低于该值不硬点，转人工复核",
    },
    "driver.retry_on_failure": {
        "type": "boolean", "default": False,
        "label": "定位类失败自动重试一次",
        "hint": "等价于给每个步骤设 on_failure=retry（会拉长失败用例耗时）",
    },
    "driver.case_retry": {
        "type": "integer", "default": 0, "min": 0, "max": 3,
        "label": "失败用例复跑次数（flake 过滤）",
        "hint": "只对 fail/error/blocked 复跑；复跑后通过会在报告/详情标记为疑似偶发。0=关闭",
    },
    # UIA
    "uia.input_mode": {
        "type": "enum", "default": "keys", "enum": ["keys", "set_text"],
        "label": "UIA 输入方式",
        "hint": "keys=键盘输入，Qt 按下方专用方式处理；set_text=直接设值，部分控件不触发键盘事件",
    },
    "uia.qt_text_input": {
        "type": "enum", "default": "value", "enum": ["value", "keys"],
        "label": "Qt 文本输入方式",
        "hint": "value=可访问值接口，适合功能回归；keys=逐字键盘，受 Qt 和输入法影响，需配置内容断言",
    },
    "uia.uia_timeout_ms": {
        "type": "integer", "default": 3000, "min": 200, "max": 60000,
        "label": "UIA 查找超时（毫秒）",
        "hint": "单个控件查找的等待上限",
    },
    "uia.app_launch_timeout_s": {
        "type": "integer", "default": 30, "min": 5, "max": 600,
        "label": "启动等待上限（秒）",
    },
    "uia.ready_poll_ms": {
        "type": "integer", "default": 200, "min": 50, "max": 5000,
        "label": "就绪轮询间隔（毫秒）",
    },
    # 视觉
    "vision.template_threshold": {
        "type": "number", "default": 0.86, "min": 0.5, "max": 1.0, "step": 0.01,
        "label": "模板匹配阈值",
        "hint": "低于该值判未命中；调高更严（易漏），调低更松（易错点）",
    },
    "vision.learn_from_vlm": {
        "type": "boolean", "default": True,
        "label": "VLM 命中后自动沉淀模板（L2→L1）",
        "hint": "关闭后每次语义定位都走 VLM（有 token 成本），但模板库不会被自动写入",
    },
    "vision.locate_poll_ms": {
        "type": "integer", "default": 300, "min": 50, "max": 5000,
        "label": "模板轮询间隔（毫秒）",
    },
    "vision.grab_backend": {
        "type": "enum", "default": "mss", "enum": ["mss", "pil"],
        "label": "截图后端",
        "hint": "mss 快；若截图异常可换 pil（需要 Pillow）",
    },
    "vision.vlm.bbox_format": {
        "type": "enum", "default": "xyxy", "enum": ["xywh", "xyxy"],
        "label": "视觉边界框格式", "hint": "pumen-vllm 使用 xyxy；网关统一转换为 x/y/宽/高",
    },
    "vision.vlm.coordinate_space": {
        "type": "enum", "default": "normalized_1000", "enum": ["pixels", "normalized_1000"],
        "label": "视觉模型坐标制式", "hint": "pumen-vllm 使用 normalized_1000；网关按实际图片尺寸转为像素",
    },
    "vision.vlm.model": {
        "type": "string", "default": "pumen-vllm", "label": "视觉模型名",
    },
    "vision.vlm.api_base": {
        "type": "string", "default": "http://192.168.1.77:8989/v1",
        "label": "视觉模型地址",
    },
    # 对话模型
    "llm.model": {
        "type": "string", "default": "pumen-vllm", "label": "对话模型名",
    },
    "llm.api_base": {
        "type": "string", "default": "http://192.168.1.77:8989/v1",
        "label": "对话模型地址",
        "hint": "默认使用内网 pumen-vllm，接口无需密钥；可改成其他兼容地址",
    },
    "llm.temperature": {
        "type": "number", "default": 0.1, "min": 0.0, "max": 2.0, "step": 0.1,
        "label": "采样温度",
    },
    "llm.max_tokens": {
        "type": "integer", "default": 4096, "min": 256, "max": 128000,
        "label": "单次最大 token",
    },
    "llm.timeout_s": {
        "type": "integer", "default": 60, "min": 5, "max": 600,
        "label": "请求超时（秒）",
    },
    # 禅道
    "zentao.api_version": {
        "type": "enum", "default": "v1", "enum": ["v1"],
        "label": "禅道 API 版本", "hint": "当前支持 REST v1；v2 使用不同契约，暂不支持",
    },
    "zentao.build": {
        "type": "string", "default": "", "label": "提测构建/版本",
        "hint": "按公司禅道实际构建填写，建远端测试单时必填",
    },
    "zentao.base_url": {
        "type": "string", "default": "https://zentao.example.com",
        "label": "禅道地址",
        "hint": "出厂占位值，请改成公司实际地址",
    },
    "zentao.account": {
        "type": "secret", "default": "",
        "label": "禅道账号",
        "hint": "API Token 缺失/过期时用它换 Token；留空则回落环境变量 ZENTAO_ACCOUNT",
    },
    "zentao.password": {
        "type": "secret", "default": "",
        "label": "禅道密码",
        "hint": "与账号配套（401/403 自动重登）。注意：明文存于数据根覆盖层，请保证数据根的访问权限；留空则回落环境变量 ZENTAO_PASSWORD",
    },
    "zentao.product_id": {
        "type": "integer", "default": 1, "min": 1, "label": "默认产品 ID",
    },
    "zentao.sync_interval_min": {
        "type": "integer", "default": 60, "min": 5, "max": 1440,
        "label": "增量同步间隔（分钟）",
    },
    "zentao.create_task": {
        "type": "boolean", "default": False,
        "label": "建任务时同步创建禅道任务",
        "hint": "关闭时任务只落本地（sync_status=local）",
    },
    # 模拟器
    "emulator.endpoint": {
        "type": "string", "default": "ws://127.0.0.1:9300",
        "label": "模拟器 WebSocket 地址",
    },
    "emulator.connect_timeout_s": {
        "type": "integer", "default": 5, "min": 1, "max": 60, "label": "连接超时（秒）",
    },
    "emulator.call_timeout_s": {
        "type": "integer", "default": 10, "min": 1, "max": 300, "label": "指令超时（秒）",
    },
    # 知识库
    "knowledge.element_stale_days": {
        "type": "integer", "default": 30, "min": 1, "max": 3650,
        "label": "元素库超期天数",
        "hint": "超过该天数未命中即降权（路由更倾向视觉）",
    },
    "knowledge.router_fail_streak": {
        "type": "integer", "default": 3, "min": 1, "max": 50,
        "label": "路由重排触发连续失败数",
        "hint": "UIA 连续失败达到该值后，视觉提前",
    },
    "knowledge.fuzzy_top_n": {
        "type": "integer", "default": 3, "min": 1, "max": 50,
        "label": "失败库模糊匹配条数上限",
    },
    "knowledge.similar_top_n": {
        "type": "integer", "default": 3, "min": 1, "max": 20, "label": "报告相似历史条数",
    },
    # 日志
    "log.level": {
        "type": "enum", "default": "INFO",
        "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
        "label": "日志级别",
    },
    "log.per_run_file": {
        "type": "boolean", "default": True,
        "label": "额外落一份日志文件",
        "hint": "run 结束会抽取为报告目录下的 run.log",
    },
}

# 分组（设置页按此分块展示，顺序即展示顺序）
GROUPS: list[tuple[str, str, list[str]]] = [
    ("被测程序", "被测软件版本与路径（写入环境快照，供归因可比）",
     ["app.test_app_exe", "app.test_app_version"]),
    ("驱动路由", "决定每步用 UIA 还是视觉；方案级与步骤级可覆盖全局",
     ["driver.mode", "driver.vision_min_confidence", "driver.retry_on_failure",
      "driver.case_retry"]),
    ("UIA 驱动", "控件树定位与输入",
     ["uia.input_mode", "uia.qt_text_input", "uia.uia_timeout_ms", "uia.app_launch_timeout_s",
      "uia.ready_poll_ms"]),
    ("视觉驱动", "模板匹配与 VLM 语义定位",
     ["vision.template_threshold", "vision.learn_from_vlm", "vision.locate_poll_ms",
      "vision.grab_backend", "vision.vlm.bbox_format", "vision.vlm.coordinate_space", "vision.vlm.model", "vision.vlm.api_base"]),
    ("对话模型", "用例解析与失败归因（内网部署）",
     ["llm.model", "llm.api_base", "llm.temperature", "llm.max_tokens",
      "llm.timeout_s"]),
    ("禅道", "用例来源与任务回写（含凭据）",
     ["zentao.api_version", "zentao.build", "zentao.base_url", "zentao.account", "zentao.password",
      "zentao.product_id", "zentao.sync_interval_min", "zentao.create_task"]),
    ("设备模拟器", "带外 WebSocket 控制接口",
     ["emulator.endpoint", "emulator.connect_timeout_s", "emulator.call_timeout_s"]),
    ("知识库", "元素库/失败库/路由学习阈值",
     ["knowledge.element_stale_days", "knowledge.router_fail_streak",
      "knowledge.fuzzy_top_n", "knowledge.similar_top_n"]),
    ("日志", "输出级别与落盘",
     ["log.level", "log.per_run_file"]),
]

# 含密钥语义的字段名：出现在路径里就必须显式声明 type="secret" 才可写
# （脱敏展示 + 明文落覆盖层），否则 apply 拒绝。注意用带分隔符的精确名：
# `llm.max_tokens` 含 "token" 但不是密钥，用无脑子串匹配会误伤（自测已钉住）。
SECRET_LIKE = ("token_env", "api_key_env", "api_key", "account", "password")


class ConfigError(ValueError):
    """配置值非法（消息面向用户，直接可展示）。"""


def _get_in(cfg: dict, path: str):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_in(cfg: dict, path: str, value) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _coerce(spec: dict, raw):
    """把前端传来的值规整为配置需要的类型；不合法就抛 ``ConfigError``。"""
    kind = spec["type"]
    label = spec.get("label", "")
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0"):
            return raw.strip().lower() in ("true", "1")
        raise ConfigError(f"{label} 需要 true/false")
    if kind == "integer":
        try:
            num = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{label} 需要整数") from None
    elif kind == "number":
        try:
            num = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{label} 需要数字") from None
        if not math.isfinite(num):
            # NaN 与任何比较都是 False，min/max 门禁会被整体绕过
            raise ConfigError(f"{label} 不能是 NaN/Infinity")
    elif kind == "enum":
        text = str(raw).strip()
        if text not in spec.get("enum", []):
            raise ConfigError(f"{label} 只能是 {spec.get('enum')}")
        return text
    else:                                   # string / secret
        return str(raw).strip()

    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and num < lo:
        raise ConfigError(f"{label} 不能小于 {lo}")
    if hi is not None and num > hi:
        raise ConfigError(f"{label} 不能大于 {hi}")
    return num


def validate(path: str, raw):
    """校验并规整单个值。路径必须是 ``EDITABLE`` 里的键。"""
    spec = EDITABLE.get(path)
    if spec is None:
        raise ConfigError(f"不可编辑的配置项：{path}")
    return _coerce(spec, raw)


class ConfigService:
    """配置读写（覆盖层 on ``<data_root>/config.override.yaml``）。"""

    def __init__(self, cfg: dict, override_path: str | Path | None = None):
        self.cfg = cfg or {}
        self.path = Path(override_path) if override_path else self._default_path()

    def _default_path(self) -> Path:
        """覆盖层位置：数据根（可写）优先，否则落 cwd。

        复用 `app.db` 所在目录而不是另算一遍数据根：数据库一定在可写目录里，
        用它推导最稳（CLI 形态是 `./data/`，壳形态是 `TESTPILOT_HOME/data/`）。
        """
        db = (self.cfg.get("app") or {}).get("db") or "./data/testpilot.db"
        try:
            return Path(db).resolve().parent / OVERRIDE_NAME
        except OSError:                       # pragma: no cover - 极端路径
            return Path("./data") / OVERRIDE_NAME

    # ---------- 读 ----------

    def override(self) -> dict:
        """读覆盖层；文件不存在或损坏都返回空 dict（坏文件不该让设置页打不开）。"""
        if not self.path.exists():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            log.warning(f"覆盖层读取失败（按无覆盖处理）: {self.path} {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def items(self) -> list[dict]:
        """可编辑字段的当前状态（分组顺序），供设置页渲染表单。

        ``value`` 是**实际生效值**（默认值 + 覆盖层合并后），
        ``overridden`` 标记该项是否被用户改过，便于"恢复默认"。
        """
        overlay = self.override()
        out: list[dict] = []
        for group, description, paths in GROUPS:
            fields = []
            for path in paths:
                spec = EDITABLE[path]
                raw = _get_in(overlay, path)
                value = raw if raw is not None else _get_in(self.cfg, path)
                field = {
                    "path": path,
                    "label": spec.get("label", path),
                    "hint": spec.get("hint", ""),
                    "type": spec["type"],
                    "enum": spec.get("enum"),
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                    "step": spec.get("step"),
                    "value": value,
                    "default": spec.get("default"),
                    "overridden": raw is not None,
                    "secret_like": any(s in path for s in SECRET_LIKE),
                }
                if spec["type"] == "secret":
                    # 脱敏：真实值永不出引擎（describe 会被前端拿到并渲染），
                    # 只告诉界面"设没设过"，输入框显示占位提示
                    field["value"] = ""
                    field["has_value"] = bool(value)
                fields.append(field)
            out.append({"group": group, "description": description, "fields": fields})
        return out

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "groups": self.items(),
        }

    # ---------- 写 ----------

    def apply(self, values: dict, *, reset: bool = False) -> dict:
        """写入覆盖层。

        ``values``：``{"driver.mode": "uia_first", ...}``。
        ``reset=True``：先清空覆盖层（恢复出厂默认），此时 ``values`` 可为空。
        返回 ``{path, written, overridden, changed, restart_required}``。
        """
        if reset:
            overlay: dict = {}
        else:
            if not values:
                raise ConfigError("没有要写入的配置项")
            overlay = self.override()

        changed: list[dict] = []
        for path, raw in (values or {}).items():
            spec = EDITABLE.get(path)
            if spec is None:
                raise ConfigError(f"不可编辑的配置项：{path}")
            # 密钥样字段名必须显式声明 type="secret" 才可写（禅道账密等本地凭据）；
            # 其余（token_env/api_key_env 等）仍然拒绝，引导走环境变量
            if any(s in path for s in SECRET_LIKE) and spec.get("type") != "secret":
                raise ConfigError(f"{path} 涉及密钥，请用环境变量配置，不要在界面里写")
            # 空串表示"清除该项覆盖"（回到默认），而不是写一个空字符串
            if isinstance(raw, str) and raw.strip() == "":
                self._clear(overlay, path)
                changed.append({"path": path, "value": None, "cleared": True})
                continue
            value = _coerce(spec, raw)
            if value == spec.get("default"):
                # 与默认值相同就不必写进覆盖层（保持覆盖层只存"差异"）
                self._clear(overlay, path)
                changed.append({"path": path, "value": value, "cleared": True})
                continue
            _set_in(overlay, path, value)
            changed.append({"path": path, "value": value, "cleared": False})

        self._write(overlay)
        remaining = self._count_leaves(overlay)
        return {
            "path": str(self.path),
            "written": len([c for c in changed if not c["cleared"]]),
            "cleared": len([c for c in changed if c["cleared"]]),
            "changed": changed,
            "overridden": remaining,
            "restart_required": True,       # 引擎启动时读取 -> 必须重启才生效
        }

    def _write(self, overlay: dict) -> None:
        """写覆盖层。**空覆盖层 = 删除文件**，而不是留一个只有注释的空壳。

        留空壳的后果不只是"多个没用的文件"：`describe()` 用 `path.exists()` 判断
        「是否改过配置」，空壳会让 UI 一直显示「已存在（0 项有效配置）」——
        用户点了「恢复默认」却看到文件还在，会以为没生效而反复去删别的东西。
        删除文件后，磁盘状态与 UI 显示的「全部出厂默认」才是一回事。
        """
        if not overlay:
            self._remove()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
        header = ("# TestPilot 用户配置覆盖层（由设置页写入）\n"
                  "# 只存与内置默认值的**差异**；删除本文件即恢复出厂默认。\n"
                  "# 改动需重启引擎生效；密钥类请用环境变量，不要写在这里。\n")
        import os
        import tempfile
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(header + text)
                stream.flush()
                os.fsync(stream.fileno())
            yaml.safe_load(temporary.read_text(encoding="utf-8"))
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _remove(self) -> None:
        """删除覆盖层文件（不存在时静默通过 —— 幂等，调用方不必先判断）。"""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:                  # 权限/占用：不能静默吞掉，否则"恢复默认"假成功
            raise ConfigError(f"删除覆盖层失败（{self.path}）：{e}") from e

    @staticmethod
    def _clear(overlay: dict, path: str) -> None:
        parts = path.split(".")
        node = overlay
        stack = []
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                return
            stack.append((node, part))
            node = nxt
        node.pop(parts[-1], None)
        # 清理空节点，避免覆盖层里留一堆没用的空壳
        for parent, key in reversed(stack):
            if isinstance(parent.get(key), dict) and not parent[key]:
                parent.pop(key, None)

    @staticmethod
    def _count_leaves(node: dict) -> int:
        total = 0
        for value in node.values():
            if isinstance(value, dict):
                total += ConfigService._count_leaves(value)
            else:
                total += 1
        return total


def load_with_override(cfg: dict, override_path: str | Path | None = None) -> dict:
    """把覆盖层合并进 cfg（原地修改并返回）。

    合并规则：**只覆盖已知可编辑键**，其余键原样保留 —— 覆盖层是用户可写的文件，
    不能让它变成一个"能改任意配置（含把日志目录指到只读位置）"的后门。
    """
    svc = ConfigService(cfg, override_path)
    overlay = svc.override()
    applied = 0
    for path in EDITABLE:
        raw = _get_in(overlay, path)
        if raw is None:
            continue
        try:
            _set_in(cfg, path, _coerce(EDITABLE[path], raw))
            applied += 1
        except ConfigError as e:
            log.warning(f"覆盖层配置项无效，已忽略: {path}={raw!r}（{e}）")
    if applied:
        log.info(f"已应用 {applied} 项用户配置覆盖（{svc.path}）")
    return cfg

"""LLM 统一网关 — 解析/定位/视觉断言/归因四类调用收敛，token 计量（设计 8.13 / 11 章）。

要点：
- 四类能力全部收敛到本网关，prompt 版本化（``adapters/prompts.py``）；
- 每次调用记录 模型 / prompt 版本 / token / 耗时，进日志与 run 统计（``usage_snapshot``）；
- LLM 输出**必须过结构校验**：非法步骤不丢弃，而是降级为 ``uncertain=True`` 交人审
  （设计 1.3：LLM 只做辅助，执行主干确定性）；
- ``transport`` 可注入（测试用假传输，生产用 OpenAI 兼容 ``/chat/completions``）。
"""
import json
import logging
import math
import threading
from contextlib import contextmanager
import os
import time
from typing import Callable

from testpilot.adapters import prompts
from testpilot.core.errors import AbortRequested, LlmError

log = logging.getLogger("testpilot.llm")

_DEFAULT: "LlmGateway | None" = None

# 合法的 verify.type / op（与 core/dsl.py 保持一致）
_VERIFY_TYPES = {"text", "state", "vision", "screenshot_diff", "none"}
_VERIFY_OPS = {"contains", "equals", "regex"}

#: 能力清单校验：args JSON Schema 支持的关键字（超出部分容忍忽略，见 11.2）
_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}

# DSL 动作白名单（设计 6.1）；LLM 产出表外动作一律标 uncertain 交人审
KNOWN_ACTIONS = frozenset({
    "app.launch", "app.close", "app.wait_ready",
    "ui.click", "ui.input", "ui.select", "ui.toggle", "ui.keypress", "ui.wait",
    "vision.click",
    "device.call", "device.scene",
    "assert.ui", "assert.state", "assert.vision",
    "sleep",
})


class LlmGateway:
    def __init__(self, cfg: dict, transport: Callable[[str, str | None], tuple] | None = None):
        self.cfg = cfg or {}
        self.model = self.cfg.get("model", "company-llm")
        self.api_base = os.path.expandvars(str(self.cfg.get("api_base") or ""))
        self.api_key = os.environ.get(self.cfg.get("api_key_env", ""), "")
        self.transport = transport          # callable(prompt, image_b64) -> (content, usage)
        self.emulator_capabilities: dict | None = None   # 模拟器能力白名单（设计 11.2）
        self.stats = {"calls": 0, "tokens": 0, "ms": 0, "errors": 0}
        self._control = threading.local()
        self._http_slots = threading.BoundedSemaphore(4)

    @contextmanager
    def request_context(self, *, abort=None, timeout_s=None):
        previous = getattr(self._control, "abort", None)
        previous_deadline = getattr(self._control, "deadline", None)
        self._control.abort = abort
        if timeout_s is not None:
            deadline = time.monotonic() + timeout_s
            self._control.deadline = min(previous_deadline, deadline) if previous_deadline else deadline
        try:
            yield
        finally:
            self._control.abort = previous
            self._control.deadline = previous_deadline

    def _check_abort(self):
        abort = getattr(self._control, "abort", None)
        if abort is not None:
            abort.check()

    def _bounded_post(self, url, *, timeout, **kwargs):
        """Bound the whole request and let cancellation return without waiting on IO."""
        import httpx
        self._check_abort()
        if not self._http_slots.acquire(blocking=False):
            raise LlmError("此前模型请求仍在收尾，请稍后重试")
        done, result = threading.Event(), []
        def request():
            try:
                result.append(httpx.post(url, timeout=timeout, trust_env=False, **kwargs))
            except Exception as exc:
                result.append(exc)
            finally:
                self._http_slots.release()
                done.set()
        threading.Thread(target=request, daemon=True, name="llm-http").start()
        deadline = time.monotonic() + timeout
        while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
            self._check_abort()
            if time.monotonic() >= deadline:
                raise LlmError("模型请求总超时")
        self._check_abort()
        if time.monotonic() > deadline:
            raise LlmError("模型请求总超时")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    # ---------- 计量 ----------

    def usage_snapshot(self) -> dict:
        return dict(self.stats)

    @staticmethod
    def usage_delta(before: dict, after: dict) -> dict:
        return {k: int(after.get(k, 0)) - int(before.get(k, 0)) for k in after}

    # ---------- 对外四类能力 ----------

    def parse_case(self, raw: dict) -> dict:
        """禅道原文 -> DSL 草稿（含 source_text 与不确定标记）。"""
        prompt = prompts.parse_case_prompt(raw, self.emulator_capabilities)
        out = self._chat_json(prompt, scene="parse_case")
        return self._normalize_draft(out, raw)

    def ground(self, image_b64: str, desc: str, region=None) -> dict:
        """VLM 语义定位 -> {bbox:[x,y,w,h], confidence:float}。"""
        space = self.cfg.get("vision", {}).get("coordinate_space", "pixels")
        box_format = self.cfg.get("vision", {}).get("bbox_format", "xywh")
        raw = self._chat_json(prompts.ground_prompt(desc, region, space, box_format),
                              image_b64=image_b64, scene="ground")
        bbox = raw.get("bbox")
        if bbox is not None:
            try:
                bbox = [int(v) for v in bbox]
                if len(bbox) != 4:
                    bbox = None
            except (TypeError, ValueError):
                bbox = None
        if bbox is not None and box_format == "xyxy":
            x1, y1, x2, y2 = bbox
            if x2 <= x1 or y2 <= y1:
                raise LlmError("模型 bbox 右下角必须在左上角之后")
            bbox = [x1, y1, x2 - x1, y2 - y1]
        elif box_format not in ("xywh", "xyxy"):
            raise LlmError(f"未知边界框格式: {box_format}")
        if bbox is not None and space == "normalized_1000":
            import base64
            import struct
            data = base64.b64decode(image_b64, validate=True)
            if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
                raise LlmError("归一化坐标转换需要有效 PNG 尺寸")
            width, height = struct.unpack(">II", data[16:24])
            if any(v < 0 or v > 1000 for v in bbox):
                raise LlmError("归一化 bbox 必须在 0..1000 范围内")
            x, y, w, h = bbox
            bbox = [round(x * width / 1000), round(y * height / 1000),
                    round(w * width / 1000), round(h * height / 1000)]
        elif space != "pixels":
            if space != "normalized_1000":
                raise LlmError(f"未知视觉坐标制式: {space}")
        return {"bbox": bbox, "confidence": _as_float(raw.get("confidence")),
                "reason": str(raw.get("reason", ""))}

    def verify_vision(self, image_b64: str, expect: str) -> dict:
        raw = self._chat_json(prompts.verify_vision_prompt(expect),
                              image_b64=image_b64, scene="verify_vision")
        return {"pass": _as_bool(raw.get("pass")),
                "confidence": _as_float(raw.get("confidence")),
                "reason": str(raw.get("reason", ""))}

    def judge_failure(self, ctx: dict) -> dict:
        context = dict(ctx)
        images = context.pop("images", None)
        out = self._chat_json(prompts.judge_prompt(context),
                              image_b64=images or None, scene="judge")
        return {"category": str(out.get("category", "unknown")),
                "confidence": _as_float(out.get("confidence")),
                "evidence": str(out.get("evidence", "")),
                "suggestion": str(out.get("suggestion", ""))}

    # ---------- 草稿归一化 ----------

    def _normalize_draft(self, out: dict, raw: dict) -> dict:
        """校验并归一化 LLM 输出；非法步骤降级为 uncertain 步骤而非报错。"""
        steps, problems = [], []
        items = out.get("steps") or []
        if not isinstance(items, list):
            raise LlmError("解析结果 steps 必须为数组")
        for i, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                problems.append(f"第 {i} 步不是对象，保留为待修正步骤")
                steps.append({"id": f"s{i}", "action": "review.required",
                              "source_text": str(item), "uncertain": True})
                continue
            step, err = _sanitize_step(item, i, self.emulator_capabilities)
            steps.append(step)
            if err:
                problems.append(err)

        source_steps = raw.get("steps") or []
        if not source_steps and raw.get("steps_text"):
            source_steps = [{"desc": line, "expect": ""}
                            for line in str(raw["steps_text"]).splitlines() if line.strip()]
        for step in steps:
            if source_steps and not step.get("source_text"):
                step["uncertain"] = True
                problems.append(f"{step['id']} 缺少来源原文，须核对步骤与期望覆盖")
        for index, source in enumerate(source_steps, 1):
            desc = str(source.get("desc") or "").strip()
            related = [s for s in steps if desc and desc in str(s.get("source_text") or "")]
            if not related:
                problems.append(f"原文第 {index} 步未找到来源映射")
                for step in steps:
                    step["uncertain"] = True
            elif source.get("expect") and not any(
                    (s.get("verify") or {}).get("type", "none") != "none" for s in related):
                problems.append(f"原文第 {index} 步有期望但 DSL 未验证")
                for step in related:
                    step["uncertain"] = True
        notes = str(out.get("notes", "") or "")
        if problems:
            notes = (notes + "\n" if notes else "") + "校验提示: " + "；".join(problems)

        draft = {
            "case_id": out.get("case_id") or raw.get("case_id"),
            "title": out.get("title") or raw.get("title", ""),
            "preconditions": [str(p) for p in (out.get("preconditions") or [])],
            "notes": notes,
            "steps": steps,
            "prompt_version": prompts.PROMPT_VERSIONS["parse_case"],
            "uncertain_count": sum(1 for s in steps if s.get("uncertain")),
        }
        return draft

    # ---------- 底层调用 ----------

    def _chat_json(self, prompt: str, image_b64: str | None = None,
                   scene: str = "") -> dict:
        t0 = time.monotonic()
        self.stats["calls"] += 1
        try:
            content, usage = self._complete(prompt, image_b64)
            data = _parse_json_object(content)
        except AbortRequested:
            raise
        except Exception as e:
            self.stats["errors"] += 1
            ms = int((time.monotonic() - t0) * 1000)
            self.stats["ms"] += ms     # 失败同样耗时：usage 的 ms 与墙钟对得上
            log.error(f"LLM 调用失败 scene={scene} ms={ms}: {e}")
            if isinstance(e, LlmError):
                raise
            raise LlmError(f"LLM 调用失败 scene={scene}: {e}") from e

        ms = int((time.monotonic() - t0) * 1000)
        tokens = int(usage.get("total_tokens", 0) or 0)
        self.stats["tokens"] += tokens
        self.stats["ms"] += ms
        log.info("LLM 调用完成", extra={
            "llm_model": self.model, "llm_ms": ms, "llm_tokens": tokens,
            "prompt_version": prompts.PROMPT_VERSIONS.get(
                scene, prompts.PROMPT_VERSIONS["parse_case"])})
        return data

    def _complete(self, prompt: str, image_b64: str | None) -> tuple[str, dict]:
        if self.transport is not None:
            return self.transport(prompt, image_b64)
        return self._http_completion(prompt, image_b64)

    def _http_completion(self, prompt: str, image_b64: str | None) -> tuple[str, dict]:
        """OpenAI 兼容 ``/chat/completions``（公司内网部署模型，v1.2 定案）。"""
        import httpx

        configured_base = (self.cfg.get("vision", {}).get("api_base") if image_b64 else None) or self.api_base
        if not configured_base:
            raise LlmError("llm.api_base 未配置（config/default.yaml llm 节）")
        if "${" in configured_base:
            # ${ENV} 展不开时 settings 保留占位串（truthy），直接发请求只会报
            # 难懂的 InvalidURL；这里给出可行动的错误
            raise LlmError(
                f"llm.api_base 含未解析的环境变量占位符: {self.api_base}"
                "（请在环境中设置对应变量，见 config/default.yaml）")
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image_b64:
            images = image_b64 if isinstance(image_b64, list) else [image_b64]
            for encoded in images[:2]:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        config = {**self.cfg, **(self.cfg.get("vision", {}) if image_b64 else {})}
        api_base = str(config.get("api_base") or self.api_base)
        api_key = os.environ.get(config.get("api_key_env", ""), "")
        body = {
            "model": config.get("model", self.model),
            "temperature": float(self.cfg.get("temperature", 0.1)),
            "max_tokens": int(self.cfg.get("max_tokens", 4096)),
            "messages": [{"role": "user", "content": content}],
        }
        if self.cfg.get("chat_template_kwargs") is not None:
            body["chat_template_kwargs"] = self.cfg["chat_template_kwargs"]
        url = f"{api_base.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = float(config.get("timeout_s", 60))

        deadline = time.monotonic() + timeout
        context_deadline = getattr(self._control, "deadline", None)
        if context_deadline is not None:
            deadline = min(deadline, context_deadline)
        last_error: Exception | None = None
        for attempt in (1, 2, 3):            # 瞬时网络错误与服务端过载重试
            try:
                self._check_abort()
                # 浮点加减在不同平台可能得到 timeout + 1e-14；传给 httpx 的单次
                # timeout 绝不能大于配置总预算，否则既违反契约也会让边界测试抖动。
                remaining = min(timeout, deadline - time.monotonic())
                if remaining <= 0:
                    raise LlmError("模型请求总超时")
                resp = self._bounded_post(url, headers=headers, json=body, timeout=remaining)
                if resp.status_code in (429, 500, 502, 503, 504):
                    # 内网模型瞬时过载一次即失败太脆：与传输错误同口径重试
                    last_error = LlmError(
                        f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                    log.warning(f"LLM 服务端过载（第 {attempt} 次）: {resp.status_code}")
                elif resp.status_code >= 400:
                    raise LlmError(
                        f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    data = resp.json()
                    choice = data["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise LlmError("模型输出达到 token 上限，结果不完整；请增加输出预算或关闭思考")
                    text = choice["message"].get("content")
                    return text, data.get("usage") or {}
            except LlmError:
                raise
            except httpx.TransportError as e:
                last_error = e
                log.warning(f"LLM 传输异常（第 {attempt} 次）: {e}")
            if attempt < 3:
                delay_end = min(deadline, time.monotonic() + 0.5 * attempt)
                while time.monotonic() < delay_end:
                    self._check_abort()
                    time.sleep(min(0.05, delay_end - time.monotonic()))    # 简单退避：0.5s / 1s
        raise LlmError(f"LLM 网络不可达: {last_error}")


def get_default_llm() -> LlmGateway:
    global _DEFAULT
    if _DEFAULT is None:
        from testpilot.config.settings import load_settings
        cfg = load_settings()
        _DEFAULT = LlmGateway({**cfg.get("llm", {}),
                               "vision": cfg.get("vision", {}).get("vlm", {})})
    return _DEFAULT


def set_default_llm(gateway: "LlmGateway | None") -> None:
    """测试/装配层替换全局网关。"""
    global _DEFAULT
    _DEFAULT = gateway


# ---------- 内部工具 ----------

def _parse_json_object(text: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 ```json 代码块与前后说明文字）。"""
    if not isinstance(text, str) or not text.strip():
        raise LlmError("LLM 返回空内容")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]                       # 去首行围栏（含语言标注）
        if cleaned[:4].lower().startswith("json"):
            cleaned = cleaned[4:]
        end_fence = cleaned.rfind("```")
        if end_fence >= 0:
            cleaned = cleaned[:end_fence]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise LlmError(f"LLM 返回中找不到 JSON 对象: {text[:200]}")
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError as e:
        raise LlmError(
            f"LLM 返回非法 JSON: {e}; 片段={cleaned[start:start + 200]}") from e
    if not isinstance(data, dict):
        raise LlmError("LLM 返回的 JSON 顶层不是对象")
    return data


# ---------- 模拟器能力清单校验（设计 11.2 幻觉防线） ----------

def _cap_commands(caps: dict) -> dict[str, dict]:
    """归一化指令表：``["load_scene"]`` 与 ``[{"cmd": ..., "args_schema": ...}]`` 均支持。"""
    out: dict[str, dict] = {}
    for entry in caps.get("commands") or []:
        if isinstance(entry, str):
            out[entry] = {}
        elif isinstance(entry, dict) and entry.get("cmd"):
            out[str(entry["cmd"])] = entry
    return out


def _cap_scenes(caps: dict) -> set[str]:
    out: set[str] = set()
    for entry in caps.get("scenes") or []:
        if isinstance(entry, str):
            out.add(entry)
        elif isinstance(entry, dict) and entry.get("id"):
            out.add(str(entry["id"]))
    return out


def _type_ok(value, expect: str) -> bool:
    if expect == "object":
        return isinstance(value, dict)
    if expect == "array":
        return isinstance(value, list)
    if expect == "string":
        return isinstance(value, str)
    if expect == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expect == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expect == "boolean":
        return isinstance(value, bool)
    if expect == "null":
        return value is None
    return True


def _schema_errors(value, schema, path: str = "args") -> list[str]:
    """轻量 JSON Schema 校验（支持 type/required/properties/enum/items/min-max）。

    能力清单里的 ``args_schema`` 是模拟器自带契约；这里只做**保守校验**：
    不认识的 Schema 关键字一律忽略，避免把合法指令误判成人审。
    """
    if not isinstance(schema, dict):
        return []
    errs: list[str] = []
    expect = schema.get("type")
    if expect in _JSON_TYPES and not _type_ok(value, expect):
        return [f"{path} 类型应为 {expect}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errs.append(f"{path}={value!r} 不在枚举 {enum} 内")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path}.{req} 缺失（必填）")
        if schema.get("additionalProperties") is False:
            extra = [k for k in value if k not in props]
            if extra:
                errs.append(f"{path} 含未定义字段 {extra}")
        for key, sub in props.items():
            if key in value:
                errs += _schema_errors(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errs += _schema_errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}={value} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}={value} 大于最大值 {schema['maximum']}")
    return errs


def _device_step_problem(data: dict, caps: dict | None) -> str | None:
    """device.* 与能力清单对齐；返回首个问题描述（None = 通过或不可校验）。

    仅在拿到清单时校验：模拟器未起（caps 为空）时不误伤，prompt 层已禁止生成 device.*。
    """
    if not caps:
        return None
    action = data.get("action")
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    if action == "device.call":
        cmds = _cap_commands(caps)
        cmd = str(args.get("cmd") or "")
        if cmds and cmd not in cmds:
            return f"cmd={cmd!r} 不在指令表"
        schema = (cmds.get(cmd) or {}).get("args_schema")
        if isinstance(schema, dict):
            errs = _schema_errors(args.get("args") or {}, schema)
            if errs:
                return "args 不符合指令 Schema：" + "；".join(errs[:3])
    elif action == "device.scene":
        scenes = _cap_scenes(caps)
        scene = str(args.get("scene_id") or "")
        if scenes and scene not in scenes:
            return f"scene_id={scene!r} 不在场景清单"
    return None


def _sanitize_step(item: dict, index: int, caps: dict | None = None) -> tuple[dict, str | None]:
    """把单步 LLM 输出规整成合法 Step 字典。

    四种结局：
    1. 完全合法 -> 原样返回；
    2. 枚举字段非法但可纠正 -> 纠正并标 ``uncertain``（交人审，不静默放过）；
    3. 动作不在白名单 / device.* 不合能力清单 -> 保留步骤但标 ``uncertain``；
    4. 结构非法（缺字段/类型错）-> 降级为 sleep + ``uncertain``。
    """
    from pydantic import ValidationError

    from testpilot.core.dsl import DriverMode, OnFailure, Step

    data = dict(item)
    data.setdefault("id", f"s{index}")
    data.setdefault("action", "")
    fixed: list[str] = []

    verify = data.get("verify")
    if isinstance(verify, dict):
        verify = dict(verify)
        has_type = "type" in verify          # 区分"显式给了非法值"与"压根没给"
        vtype = verify.get("type")
        if vtype not in _VERIFY_TYPES:
            verify["type"] = "none"
            if has_type:
                fixed.append(f"verify.type={vtype!r}")
        vop = verify.get("op")
        if vop is not None and vop not in _VERIFY_OPS:
            verify["op"] = "contains"
            fixed.append(f"verify.op={vop!r}")
        data["verify"] = verify
    elif verify is not None and not isinstance(verify, dict):
        data["verify"] = {"type": "none"}
        fixed.append("verify 非对象")

    vdriver = data.get("driver")
    if vdriver in ("", "null", "None"):
        data["driver"] = None
    elif vdriver is not None and vdriver not in {m.value for m in DriverMode}:
        data["driver"] = None
        fixed.append(f"driver={vdriver!r}")
    von_fail = data.get("on_failure")
    if von_fail in ("", "null", "None", None):
        data["on_failure"] = OnFailure.ABORT.value
    elif von_fail not in {m.value for m in OnFailure}:
        data["on_failure"] = OnFailure.ABORT.value
        fixed.append(f"on_failure={von_fail!r}")

    try:
        step = Step.model_validate(data)
    except ValidationError as e:
        reason = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                          for err in e.errors()[:3])
        fallback = {
            "id": str(data.get("id") or f"s{index}"),
            "action": str(data.get("action") or "sleep"),
            "source_text": data.get("source_text"),
            "target": None,
            "value": None,
            "args": data.get("args") if isinstance(data.get("args"), dict) else {},
            "verify": {"type": "none"},
            "driver": None,
            "on_failure": "abort",
            "timeout_ms": 10000,
            "uncertain": True,
        }
        return fallback, f"第 {index} 步结构非法（{reason}），已降级待人审"

    dumped = step.model_dump(mode="json")
    from testpilot.core.dsl import execution_errors
    reasons: list[str] = execution_errors(step)
    if step.action not in KNOWN_ACTIONS:
        reasons.append(f"action={step.action!r} 不在动作表内")
    if fixed:
        reasons.append(f"字段被纠正（{'、'.join(fixed)}）")
    problem = _device_step_problem(dumped, caps)
    if problem:
        reasons.append(f"device 白名单校验失败（{problem}）")
    if reasons:
        dumped["uncertain"] = True
        return dumped, f"第 {index} 步 " + "；".join(reasons) + "，已标记待人审"
    return dumped, None


def _as_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise LlmError("模型 confidence 不能为布尔值")
    try:
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise LlmError("模型 confidence 必须是 0..1 的有限数字")
        return number
    except (TypeError, ValueError) as exc:
        raise LlmError("模型 confidence 必须为数字") from exc


def _as_bool(value, default: bool = False) -> bool:
    """严格的布尔解析：模型输出 ``{"pass": "false"}`` 时 ``bool("false")`` 是 True，
    会反向翻转断言结论 —— 字符串只认 true/false（大小写不敏感）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
        raise LlmError("模型 pass 必须为 true/false")
    raise LlmError("模型 pass 必须为布尔值")

"""Prompt 模板（版本化）— 换模型/改措辞都会导致输出漂移，必须可追溯（设计 11 章）。

约定：
- 每个 prompt 有唯一版本号（``*_V1``），调用处把版本号写进日志与 run 统计；
- 模板只做"骨架 + 输出格式约束"，具体数据由调用方拼进 payload；
- 模拟器能力清单以白名单方式注入解析 prompt，防止 LLM 幻觉出不存在的指令（设计 11.2）。
"""
from testpilot.core.dsl import DriverMode, OnFailure

PROMPT_VERSIONS = {
    "parse_case": "parse_case_v4",
    "ground": "ground_v3",      # v2: 消除裁剪区域绝对坐标歧义（bbox 一律相对本图）
    "verify_vision": "verify_vision_v1",
    "judge": "judge_v2",
}

# ---------- 解析（parse_case_v1） ----------

_ACTION_ACTIONS = "\n".join(
    f"- {line}" for line in (
        "app.launch     启动被测程序；args: {exe, args, title_re}",
        "app.close      关闭被测程序；args: {mode: graceful|kill}",
        "app.wait_ready 等待就绪；timeout_ms",
        "ui.click       点击控件；target 必填",
        "ui.input       输入文本；target 必填，value 为文本，args: {clear: bool}",
        "ui.select      下拉/列表/页签选择；target 必填，value 为选项文本",
        "ui.toggle      确定性勾选复选框；target 必填，value 为 true 或 false",
        "ui.keypress    在目标控件发送键序列；target 必填，value 如 {F2}、{ENTER}",
        "ui.right_click 在目标控件执行右键/上下文菜单；target 必填",
        "ui.scroll      在目标控件上滚动；target 必填，args: {direction: up|down|left|right, amount: 1..20}",
        "ui.drag        从目标控件中心相对拖拽；target 必填，args: {dx, dy, duration_ms}",
        "表格单元格定位：target.primary 使用表格定位字段并指定 row、column（0 起）",
        "ui.wait        等待控件出现/消失；target 必填，args: {state: exists|gone}",
        "vision.click   纯视觉点击（自绘控件）；target.primary.type=visual",
        "device.call    模拟器指令；args: {cmd, args}（cmd 必须来自能力清单）",
        "device.scene   加载模拟器场景；args: {scene_id}（必须来自能力清单）",
        "assert.ui      界面文本/属性断言；target + verify{type:text,expect,op}",
        "assert.state   内部状态断言；args: {path, expect}",
        "assert.vision  视觉语义断言；verify{type:vision,expect}",
        "sleep          硬等待；args: {ms}（优先用 app.wait_ready / ui.wait）",
    )
)

_OUTPUT_SCHEMA = """{
  "case_id": "TC-1024",
  "title": "用例标题",
  "preconditions": ["模拟器场景: normal_sample_a"],
  "notes": "解析中的不确定点汇总，可为空字符串",
  "steps": [
    {
      "id": "s1",
      "action": "ui.click",
      "source_text": "禅道原文步骤，原样保留",
      "target": {
        "key": "btn.start_detect",
        "primary": {"type": "uia", "name": "开始检测", "control_type": "Button"},
        "fallback": {"type": "visual", "desc": "主界面底部「开始检测」按钮"},
        "vision_only_hint": false
      },
      "value": null,
      "args": {},
      "verify": {"type": "none"},
      "driver": null,
      "on_failure": "abort",
      "timeout_ms": 10000,
      "uncertain": false
    }
  ]
}"""


#: 能力清单注入上限（按用例相关性裁剪，控制 token；设计 11.2）
MAX_INJECT_COMMANDS = 15
MAX_INJECT_SCENES = 10
MAX_INJECT_TERMS = 20


def relevant_capabilities(raw: dict, capabilities: dict | None) -> dict | None:
    """按用例文本裁剪能力清单（设计 11.2：注入与用例相关的子集控制 token）。

    策略：命中用例文本的条目排前，未命中的按原序补足到上限——清单小的时候等于全量注入，
    清单大时优先给"用例提到过的"，但**校验侧始终用全量清单**，不会因此误判幻觉。
    """
    if not capabilities:
        return None
    text = _case_text(raw)
    commands = _rank(capabilities.get("commands") or [], text, _entry_blob)[:MAX_INJECT_COMMANDS]
    scenes = _rank(capabilities.get("scenes") or [], text, _entry_blob)[:MAX_INJECT_SCENES]
    terms = capabilities.get("terms") or {}
    if isinstance(terms, dict) and len(terms) > MAX_INJECT_TERMS:
        hit = {k: v for k, v in terms.items() if str(k).lower() in text}
        rest = {k: v for k, v in terms.items() if k not in hit}
        terms = {**hit, **rest}
        terms = dict(list(terms.items())[:MAX_INJECT_TERMS])
    return {"commands": commands, "scenes": scenes, "terms": terms}


def _case_text(raw: dict) -> str:
    parts = [str(raw.get("title") or ""), str(raw.get("steps_text") or ""),
             " ".join(str(p) for p in (raw.get("preconditions") or []))]
    return " ".join(parts).lower()


def _entry_blob(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return f"{entry.get('cmd') or entry.get('id') or ''} {entry.get('desc') or ''}"
    return ""


def _tokens(blob: str) -> list[str]:
    import re
    chunks = [c for c in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", blob) if c]
    out: list[str] = []
    for chunk in chunks:
        out.append(chunk)
        if len(chunk) > 3 and any("\u4e00" <= ch <= "\u9fff" for ch in chunk):
            out += [chunk[i:i + 2] for i in range(len(chunk) - 1)]
    return out


def _rank(entries: list, text: str, blob_fn) -> list:
    matched, rest = [], []
    for entry in entries:
        blob = blob_fn(entry).lower()
        if blob and (blob in text or any(t in text for t in _tokens(blob))):
            matched.append(entry)
        else:
            rest.append(entry)
    return matched + rest


def _render_cap_entries(entries: list) -> str:
    """把指令/场景渲染成紧凑单行（保留 cmd/desc 语义，避免整段 JSON 膨胀 prompt）。"""
    out = []
    for entry in entries:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("cmd") or entry.get("id") or "?"
            desc = entry.get("desc")
            out.append(f"{name}（{desc}）" if desc else str(name))
    return "; ".join(out) if out else "（无）"


def parse_case_prompt(raw: dict, capabilities: dict | None = None) -> str:
    """构造用例解析 prompt；``capabilities`` 为模拟器 hello 能力清单（白名单）。"""
    emu_section = "【模拟器能力清单】未提供，禁止生成 device.* 步骤。"
    caps = relevant_capabilities(raw, capabilities) if capabilities else None
    if caps:
        cmds = _render_cap_entries(caps.get("commands") or [])
        scenes = _render_cap_entries(caps.get("scenes") or [])
        terms = caps.get("terms") or {}
        emu_section = (
            "【模拟器能力清单（device.* 只能用这些，禁止编造；已按用例相关性裁剪）】\n"
            f"- 指令 cmd: {cmds}\n"
            f"- 场景 scene_id: {scenes}\n"
            f"- 术语映射: {terms}\n"
            "若用例确需的指令/场景未列出，请把该步 uncertain 置 true 并在 notes 说明。"
        )
    modes = ", ".join(m.value for m in DriverMode)
    failures = ", ".join(m.value for m in OnFailure)
    return f"""你是桌面软件自动化测试用例结构化助手。把禅道自然语言用例转换为 TestPilot DSL。

## 可用动作（action 只能取以下值）
{_ACTION_ACTIONS}

## 字段约束
- driver 只能取: {modes}（null 表示跟随全局路由）
- on_failure 只能取: {failures}
- verify.type 只能取: text | state | vision | screenshot_diff | none
- verify.op 只能取: contains | equals | regex

{emu_section}

## 输出格式（严格 JSON：不要 Markdown 代码块，不要任何解释文字）
{_OUTPUT_SCHEMA}

## 硬性规则
1. 每个步骤必须保留 source_text = 禅道原文的 desc（原样抄写，不得改写或翻译）。\n   必须覆盖每个原始步骤及其 expect；不能把有明确期望的步骤改为 verify=none。\n   preconditions 必须保留原用例前置条件，不得省略。
2. target.key 用「控件语义.用途」小写点分命名，如 btn.start_detect / edit.sample_id / tbl.result。
3. 自绘控件、图表、QWebView/Web 区域：vision_only_hint=true，primary 用 {{"type":"visual","desc":"..."}}。
4. 启动被测程序必须用 app.launch（exe 与 title_re 从用例或环境描述推断；推断不出则 uncertain=true）。
5. 不要编造断言期望值；无法确定期望值时 verify.type 用 "none"。
6. 无法准确映射的步骤：选最接近的动作并把 uncertain 置为 true，同时在 notes 中说明原因。
7. 只输出 JSON 对象，第一个字符必须是 "{{"，最后一个字符必须是 "}}"。

## 待解析用例
{_dump(raw)}"""


# ---------- VLM grounding（ground_v1） ----------

def ground_prompt(desc: str, region=None, coordinate_space="pixels", bbox_format="xywh") -> str:
    crop_hint = ("\n截图已按目标所在区域裁剪，目标元素应出现在本图内。"
                 if region else "")
    fields = "x1,y1,x2,y2" if bbox_format == "xyxy" else "x,y,w,h"
    units = ("0..1000 归一化坐标：x/w 按本图宽度归一化，y/h 按本图高度归一化"
             if coordinate_space == "normalized_1000" else "像素坐标")
    return f"""你是界面元素定位助手。在给定截图中找到目标元素，返回其边界框。

目标元素描述：{desc}{crop_hint}

## 输出格式（严格 JSON）
{{"bbox": [{fields}], "confidence": 0.0~1.0, "reason": "判断依据"}}

## 规则
1. bbox 字段顺序为 {fields}；xyxy 为左上角和右下角，xywh 为左上角及宽高。\n   bbox 使用 {units}，相对**本图片**左上角（不是屏幕、也不是裁剪前的原始图）。
2. 找不到目标时返回 {{"bbox": null, "confidence": 0.0, "reason": "未找到"}}。
3. confidence 反映把握程度：模糊、被遮挡、候选多个时不得高于 0.6。
4. 只输出 JSON 对象。"""


# ---------- 视觉断言（verify_vision_v1） ----------

def verify_vision_prompt(expect: str) -> str:
    return f"""你是界面校验助手。判断截图是否满足给定的期望描述。

期望：{expect}

## 输出格式（严格 JSON）
{{"pass": true/false, "confidence": 0.0~1.0, "reason": "判断依据"}}

## 规则
1. 只依据截图中可见内容判断，不得推测截图外信息。
2. 界面模糊、加载中、内容被遮挡时 confidence 不得高于 0.5。
3. 只输出 JSON 对象。"""


# ---------- 失败归因（judge_v1） ----------

def judge_prompt(ctx: dict) -> str:
    return f"""你是自动化测试失败归因助手。根据失败步骤的轨迹信息给出根因判断。

## 可选类别（category 只能取其一）
app_crash（被测程序崩溃/退出）、uia_change（界面或控件定位失效）、
timing（等待不足/时序问题）、env（环境或模拟器异常）、dsl_error（用例 DSL 本身有误）、
product_bug（疑似被测软件真实缺陷）、unknown（无法判断）

## 输出格式（严格 JSON）
{{"category": "uia_change", "confidence": 0.0~1.0,
 "evidence": "支持该判断的轨迹证据", "suggestion": "下一步处置建议"}}

## 规则
1. 证据必须来自给定轨迹和附带图片，不得臆测。\n   screenshots 中的文件名仅用于对应证据；只能查看实际附带图片。\n   若 analysis_mode 为 text_only，必须说明图像不可用，不得声称看到了截图。
2. 无法判断时 category 用 "unknown" 且 confidence <= 0.3。
3. 只输出 JSON 对象。

## 失败轨迹
{_dump(ctx)}"""


def _dump(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)

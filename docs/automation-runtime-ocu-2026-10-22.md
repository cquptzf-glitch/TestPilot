# TestPilot 自动化层增强设计（参考 open-computer-use）

> 日期：2026-10-22  
> 范围：Explore / MCP / UI 自动化会话层  
> 目标：在不破坏现有确定性 DSL 回归主干的前提下，补齐更适合 AI Agent 的 Computer Use 能力，并为后续继续开发留下稳定边界与演进路线。

---

## 1. 为什么要做这轮增强

TestPilot 原有自动化主链路已经比较完整：

- UIA 对象驱动；
- Vision 模板 / VLM 兜底；
- Router 学习与降级；
- Executor 负责确定性 DSL 执行；
- Verifier 负责动作后的业务验证；
- Explore ToolBox / MCP 为外部 Agent 提供探索入口。

但 Explore 自动化层此前还有几个明显缺口：

1. MCP 默认 build_server 只装配 EmulatorAdapter，外部 Agent 默认拿不到真实 UIA / Vision 工具。
2. Agent 只能“按 locator 直接动作”，缺少先观察当前 UI 状态、再选择元素的工作流。
3. 没有会话级 element_index，连续动作必须重复定位。
4. 没有统一的旧元素失效机制，界面变化后存在使用旧句柄的风险。
5. screen.grab 返回的 PNG base64 被塞进 JSON 文本，不符合 Computer Use 常见的 text + image 分块形态。
6. 输入动作缺少一个明确的“写后回读”路径，某些控件可能接受 API 调用但实际值没有变化。
7. MCP 缺少明确的 turn boundary，旧 UI 状态可能跨 Agent 回合残留。

open-computer-use 在这些问题上的实现思路比较成熟，因此本轮借鉴其架构思想，但不直接依赖或复制其平台 runtime。

参考仓库：
https://github.com/iFurySt/open-computer-use

---

## 2. 本轮采用的核心原则

### 2.1 Observation First

Agent 不应一上来就盲点控件，而是先观察：

~~~text
Agent
  |
  v
ui.observe / ui.query
  |
  v
snapshot_id + element_index + UIA metadata
  |
  v
ui.click / ui.input / ui.set_value / ui.select
~~~

这让“看见什么”和“对什么做动作”分开。

---

### 2.2 element_index 只属于会话，不属于回归 DSL

element_index 是短生命周期句柄，只适合 Agent 连续探索：

~~~text
snapshot #12
  element 1 = 主窗口
  element 2 = 样本号输入框
  element 3 = 开始检测按钮
~~~

它不能替代 TestPilot 现有的稳定 locator：

~~~text
automation_id
accessibleName
control_type
visual desc
template
~~~

因此：

- Explore/MCP：可以使用 element_index；
- 固化为正式测试用例：仍应使用稳定 locator；
- Executor：继续只执行稳定 DSL，不依赖会话索引。

这是本轮最重要的架构边界。

---

### 2.3 Fail Closed

如果无法确认旧元素还是原来的元素，就拒绝动作，而不是猜。

动作前会检查：

- element_index 是否仍存在；
- snapshot_id 是否匹配；
- wrapper 是否仍存在；
- AutomationId / control type / class / name 身份是否变化；
- 控件是否可见；
- 控件是否仍有有效几何区域。

失败时要求 Agent 重新 ui.observe 或 ui.query。

---

### 2.4 Selective Observation

ui.observe 支持：

- 只读 AX；
- 只截图；
- AX + 截图。

参数：

~~~text
include_ax
include_screenshot
max_nodes
max_depth
~~~

默认只读 AX，不主动截图，避免每个步骤都产生高成本图片。

特别注意：

screenshot-only 也会创建新的 snapshot_id，并主动清空旧 element_index。

原因是：

~~~text
旧 AX 元素
   |
截图后界面可能已经变化
   |
继续复用旧 index = 风险
~~~

因此这里选择和 open-computer-use 一样的“索引主动失效”思路。

---

## 3. 新增代码结构

### 3.1 testpilot/explore/computer_use.py

新增 ComputerUseSession，职责：

~~~text
ComputerUseSession
├── observe()
│   ├── bounded UIA tree
│   ├── optional screenshot
│   └── register element_index
├── query()
│   └── targeted UIA search
├── resolve()
│   └── stale / identity validation
├── set_value()
│   └── write + read-back verification
└── reset()
    └── clear session handles
~~~

它只依赖现有 Driver 实例和 ExecContext。

不会进入 Executor 主链路。

---

### 3.2 testpilot/explore/toolbox.py

ToolBox 新增工具：

| Tool | 用途 |
|---|---|
| ui.observe | 获取 AX 状态，可选截图 |
| ui.query | 按 text / AutomationId / control type 定向查找 |
| ui.set_value | UIA 值写入 + 写后回读 |
| ui.click | 新增 element_index 支持 |
| ui.input | 新增 element_index 支持 |
| ui.select | 新增 element_index 支持 |

原有 locator 调用方式保持兼容。

例如仍可：

~~~json
{
  "name": "开始检测",
  "control_type": "Button"
}
~~~

也可以先观察：

~~~json
{
  "include_ax": true,
  "include_screenshot": false
}
~~~

得到：

~~~json
{
  "snapshot_id": 7,
  "elements": [
    {
      "element_index": 3,
      "name": "开始检测",
      "automation_id": "startButton",
      "control_type": "Button"
    }
  ]
}
~~~

再调用：

~~~json
{
  "element_index": 3,
  "snapshot_id": 7
}
~~~

---

## 4. ui.observe 的状态模型

单个元素当前输出以下主要字段：

~~~text
element_index
depth
parent_index
name
automation_id
control_type
class_name
framework_id
value
rect
visible
enabled
actions
~~~

actions 是根据当前 wrapper 能力做的轻量推断，例如：

~~~text
click
input
set_value
select
toggle
~~~

注意：actions 只用于 Agent 决策辅助，不是正式 DriverManifest 能力声明。

---

## 5. bounded tree 的原因

Qt Widgets / Qt Quick / WebView / Electron 都可能暴露很深的可访问树。

如果不限制：

~~~text
root
  group
    group
      group
        ...
~~~

可能导致：

- UIA provider 查询长时间阻塞；
- MCP 返回巨大 JSON；
- LLM 上下文浪费；
- 真正可操作控件反而被噪音淹没。

所以当前默认：

~~~text
max_nodes = 300
max_depth = 12
~~~

硬上限：

~~~text
max_nodes <= 5000
max_depth <= 64
~~~

后续可继续做：

- Qt/QML 空 Group 压缩；
- hidden/offscreen 节点轻量化；
- action-critical 节点优先；
- tree diff。

---

## 6. ui.query

当 Agent 已经知道大致控件信息时，不应该每次读取整棵树。

例如：

~~~json
{
  "text": "开始检测",
  "control_type": "Button",
  "exact": true
}
~~~

当前 query 会：

1. 做一次 bounded AX observation；
2. 不截图；
3. 对 name / value / class_name 做匹配；
4. 返回 matches；
5. 为命中项注册 element_index。

当前是第一版实现。

后续建议直接下沉到 UIADriver，利用 UIA 原生条件查询，避免完整遍历。

---

## 7. element_index 的生命周期

当前规则：

### 创建

以下调用会生成新 snapshot：

~~~text
ui.observe
ui.query
~~~

### 失效

以下情况旧 index 失效：

~~~text
新的 ui.observe
新的 ui.query
screenshot-only observe
app.launch
app.close
notifications/turn-ended
MCP EOF
~~~

这是故意设计的。

不要为了“省一次查询”让旧句柄长时间存活。

---

## 8. set_value 写后回读

传统自动化常见一个隐蔽问题：

~~~text
SetValue API 返回 success
          |
          v
控件实际上没更新
          |
          v
自动化系统误报成功
~~~

例如：

- controlled Web input；
- 自定义 Qt 控件；
- 日期/数字编辑器；
- UIA provider 实现不完整的控件。

因此新增 ui.set_value：

~~~text
read old value
    |
set_edit_text
    |
poll
    |
read actual value
    |
compare
~~~

比较逻辑当前支持：

- 字符串 trim 后相等；
- 数字语义相等，例如 15 == 15.0；
- true/false 与 1/0 布尔语义。

写入未生效会明确抛 ActionError。

注意：

ui.input 仍保留“真实键盘/普通输入”的语义；

ui.set_value 是“可访问值直接写入”的确定性语义。

两者不要合并。

---

## 9. MCP 改动

### 9.1 默认真正装配 UI 驱动

旧 build_server：

~~~text
ToolBox({}, emulator)
~~~

因此 tools/list 实际默认只有模拟器能力。

现在：

~~~text
build_registry()
    |
    +-- UIA create + health
    |
    +-- Vision create + health
    |
    +-- EmulatorAdapter
    |
    v
ToolBox(drivers, emulator)
~~~

某个依赖不可用时只跳过对应驱动，不击穿 MCP server。

---

### 9.2 image block

旧 screen.grab：

~~~text
content:
  text:
    {"png_base64": "iVBOR...."}
~~~

现在：

~~~text
content:
  - text: metadata
  - image:
      mimeType: image/png
      data: ...
~~~

ui.observe(include_screenshot=true) 也使用同样形式。

这样更适合支持多模态工具调用的 Agent。

---

### 9.3 turn-ended

新增：

~~~text
notifications/turn-ended
~~~

收到后：

~~~text
ComputerUseSession.reset()
~~~

MCP EOF 同样清理。

未来如果增加：

- 软件光标；
- overlay；
- 临时窗口；
- clipboard transaction；
- 输入锁；

也应该统一挂在这个 session cleanup 上。

---

## 10. 与现有 Executor 的关系

这轮没有把 ComputerUseSession 接入 Executor。

这是刻意的。

现有回归主链路：

~~~text
DSL
 |
Router
 |
locate
 |
action
 |
Verifier
 |
Recorder
~~~

它的优点是：

- 确定性；
- 可复现；
- 便于统计；
- 失败签名稳定；
- 不依赖 Agent 当前上下文。

Explore/MCP：

~~~text
Agent
 |
observe/query
 |
element_index
 |
action
 |
探索轨迹
 |
人工确认
 |
DSL
~~~

两者应该继续分离。

---

## 11. 后续开发优先级

### P0：真实 Windows / Qt 验证

必须做：

1. Qt Widgets：
   - QPushButton
   - QLineEdit
   - QComboBox
   - QCheckBox
   - QTableView
   - QTreeView
2. Qt Quick：
   - Button
   - TextField
   - ComboBox
   - ListView
   - TableView
3. Windows 缩放：
   - 100%
   - 125%
   - 150%
   - 200%
4. 双屏：
   - 左侧负坐标；
   - 不同 DPI；
   - 窗口跨屏。

建议新增 scripts/tests/windows-computer-use-smoke.ps1。

---

### P0：观察层下沉到 Driver 协议的可选扩展

当前 ComputerUseSession 为了不破坏现有 Driver Protocol，会读取 UIADriver 的 _top / _rect_of。

这是有意控制改动面的阶段性实现。

下一步建议引入正式可选协议：

~~~text
ObservationDriver
├── observe()
├── query()
├── resolve_handle()
└── invalidate_handles()
~~~

UIADriver 实现它。

VisionDriver 不需要实现 AX observe。

这样 Explore 层不再知道 pywinauto 内部字段。

---

### P1：稳定 selector

当前 element_index 是会话索引，不是持久 selector。

后续可增加：

~~~text
ElementIdentity
├── automation_id
├── control_type
├── accessible_name
├── framework_id
├── class_name
├── ancestor path
└── relative geometry
~~~

当 Agent 通过 element_index 完成操作后，可以自动生成候选稳定 locator，进入人审。

这一步对“探索自动生成测试用例”价值很高。

---

### P1：动作后轻量 read-back

Executor 已有 Verifier。

Explore 侧还可以增加低成本动作 read-back：

~~~text
click:
  optional state change check

input:
  optional text read-back

select:
  selected value read-back

toggle:
  toggle state read-back
~~~

注意不要把业务断言和动作 read-back 混成一个概念。

动作 read-back 只回答：

“刚才这个动作真的执行了吗？”

业务 Verifier 回答：

“执行后系统是否满足测试期望？”

---

### P1：窗口/进程身份

后续 snapshot 建议增加：

~~~text
pid
hwnd
window_title
window_rect
process_start_time
~~~

动作前一起校验。

这样可以避免：

~~~text
旧程序退出
新程序重新启动
窗口标题一样
旧 handle 被误复用
~~~

---

### P2：Selective observation 优化

继续参考 open-computer-use：

- include_ax=false 时完全跳过 UIA；
- include_screenshot=false 时完全跳过截图/编码；
- hidden node detail 可选；
- unchanged screenshot fingerprint；
- action 后默认只取轻量 read-back，不自动全量截图。

---

### P2：平台输入策略分层

如果后面 TestPilot 需要操作第三方桌面软件，可考虑：

~~~text
semantic UIA action
      |
window message
      |
guarded global input
~~~

但对医疗/设备测试软件，默认不建议直接开放全局物理输入。

优先继续使用：

~~~text
UIA semantic action
Qt Accessibility
测试端口
模拟器带外接口
~~~

---

## 12. 本轮文件清单

新增：

~~~text
testpilot/explore/computer_use.py
tests/test_computer_use.py
docs/automation-runtime-ocu-2026-10-22.md
~~~

修改：

~~~text
testpilot/explore/toolbox.py
testpilot/explore/mcp_server.py
~~~

没有修改：

~~~text
Executor
Router
Verifier
DSL
UIADriver
VisionDriver
数据库 schema
桌面壳 RPC
~~~

因此现有确定性回归链路风险较低。

---

## 13. 验证清单

当前会话无法直接 clone GitHub 仓库执行本机 pytest，因此提交后请在项目标准开发环境执行：

~~~powershell
cd D:\MyCode\TestPilot
.venv\Scripts\python.exe tools\verify.py
~~~

至少重点确认：

~~~text
tests/test_computer_use.py
tests/test_explore.py
tests/test_uia_driver.py
tests/conformance/
~~~

真实桌面再补：

~~~text
Qt Widgets smoke
Qt Quick smoke
125/150/200% DPI
双屏
窗口移动/遮挡
动态控件销毁后旧 index
应用重启后的旧 snapshot
~~~

---

## 14. 下一轮建议从这里继续

推荐顺序：

1. 跑 tools/verify.py，先处理静态/单测问题；
2. 做真实 Qt UIA observation smoke；
3. 把 _top / _rect_of 私有依赖下沉成 ObservationDriver；
4. element_index -> 稳定 selector 自动生成；
5. ExploreSession 固化时自动把临时 index 转换成稳定 locator；
6. 增加 click/input/select/toggle 的轻量动作 read-back；
7. 再考虑跨应用、全局输入、录制回放等能力。

如果后面继续参考 open-computer-use，优先借鉴“会话安全、观察预算、动作回读、平台边界测试”，不要直接复制它面向通用桌面 Agent 的所有输入策略。TestPilot 的核心价值仍应是“可重复、可审计、可验证”的自动化测试。


---

## 10. 第二阶段功能扩展（2026-10-22）

在第一阶段 observation-first / element_index 会话层基础上，本阶段继续补齐“探索 → 操作 → 验证 → 固化”的产品闭环。

### 10.1 UIA locator 稳定化

观察结果现在会为每个 UIA 元素生成：

- `selector_score`：0~1 稳定度评分；
- `selector_quality`：high / medium / low；
- `selector_reason`：评分原因；
- 可持久化 `selector`。

优先级：

1. 唯一 AutomationId；
2. AutomationId + ancestors 父级作用域链；
3. Name + ControlType + ancestors；
4. 同一父级内仍重复时，最后才使用局部 index。

DSL 的 UIA Locator 新增可选：

```json
{
  "type": "uia",
  "name": "确认",
  "control_type": "Button",
  "ancestors": [
    {"automation_id": "resultPanel", "control_type": "Pane"}
  ]
}
```

注意：顶层主窗口不会写入 ancestors，因为 UIADriver 的搜索 scope 已经从主窗口开始。

### 10.2 Vision Explore

Desktop Explore 新增 Vision 模式：

- `desc` 语义定位；
- template 定位；
- locate / click / input；
- 命中 bbox 在当前截图上高亮；
- 成功动作固化为 `vision_only`，其中 click 固化为 `vision.click`。

这用于 Qt Quick Canvas、自绘控件、OpenGL/WebView 或 Accessibility 不完整区域。

### 10.3 断言能力

探索工具新增并统一固化：

- `assert.ui`：UIA 文本 equals / contains / regex；
- `assert.state`：模拟器状态 equals / contains / regex；
- `assert.vision`：VLM 视觉语义断言；
- UI 动作可开启 `verify_diff`：动作前后抓图并执行 screenshot_diff。

screenshot_diff 有一条重要约束：

> 只有探索期 before/after 实际验证通过、结果中产生 `diff_verified=true` 时，才允许写入正式 DSL。仅传 `verify_diff=true` 不足以固化验证。

### 10.4 探索历史与恢复

探索历史支持：

- removeStep；
- moveStep；
- updateStep；
- restore。

恢复只恢复**历史与固化信息**，不恢复短生命周期 UI 句柄。恢复时会主动 reset ComputerUseSession，因此历史里的 element_index 不能直接继续使用，必须重新 observe/query。

纯观察类历史做压缩：

- ui.observe：只保留 snapshot/window/节点数/截图大小等摘要；
- ui.query：只保留 match_count 等摘要；
- screen.grab：只保留 bytes/rect；
- 不把 PNG base64 或整棵 AX tree 长期写入 Session。

Desktop 会将压缩后的 Session 保存到 localStorage，并在引擎侧空会话时自动恢复。

### 10.5 Desktop 工作流

当前探索页流程：

```text
选择 / 启动窗口
        ↓
观察截图 + UIA Tree
        ↓
┌────────────┬─────────────┬─────────────┐
│ UIA 控件   │ Vision      │ Device      │
│ 元素操作   │ 语义/模板   │ 场景/指令   │
│ 文本断言   │ 视觉断言    │ 状态断言    │
└────────────┴─────────────┴─────────────┘
        ↓
操作历史（编辑 / 排序 / 删除）
        ↓
生成 DSL 草稿
        ↓
保存为 draft
        ↓
进入 DSL 人审
```

截图支持点击反查 UIA 元素；元素列表支持“只看可操作控件”。

### 10.6 并发安全

Explore 与正式回归执行通过同一 interaction mutex 串行化：

- Explore 正在操作 UI → task.run 拒绝启动；
- 正式 run 正在执行 → Explore invoke 拒绝操作。

当前仍只有一个全局 RPC Explore Session。后续若做多 Session，不能只增加 sessionId，因为 UIADriver 是有窗口状态的对象；必须为不同 Session 隔离驱动实例，才能真正支持多窗口并行探索。

### 10.7 当前验证策略

当前阶段刻意不把真机 smoke 作为提交门禁。

GitHub Actions 负责：

- Windows Python Ruff；
- Windows Pytest；
- Desktop TypeScript `tsc --noEmit`；
- Vite build。

提交 `287bdece1755acedfd3ba34d2c1f4829c412da0d` 已完整通过上述 Python 与 Desktop 两个 job。

真机 Qt / DPI / 安装包验证继续保留在 `docs/defect-backlog.md` 的 ENV 项，待 P1 功能基本稳定后集中执行。

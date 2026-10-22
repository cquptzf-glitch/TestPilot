# TestPilot 缺陷与改进台账

> 更新时间：2026-10-22  
> 当前策略：**先堆功能 + 单元测试/契约测试 + 前端编译门禁；暂不把真机 smoke 作为开发阻塞项。**  
> 真机/UIA/DPI/干净机安装验证保留，但放到功能面稳定后集中执行。

## 状态约定

- **DONE**：代码与自动化测试/契约已补齐。
- **DOING**：正在实现或已提交等待 CI 结果。
- **TODO**：已确认缺口，按优先级逐步完成。
- **ENV**：只能依赖真实 Windows/设备环境验证，不作为当前功能开发阻塞项。

## 本轮已完成

| ID | 优先级 | 状态 | 项目 | 说明 |
|---|---:|---|---|---|
| A-001 | P0 | DONE | Explore 稳定定位固化 | element_index 成功操作后自动生成 AutomationId / accessibleName + control type 的稳定 locator 候选，不再把临时索引写进 DSL。 |
| A-002 | P0 | DONE | Explore 与正式执行互斥 | RPC 增加 UI interaction mutex，正式 run 与探索操作不能同时抢鼠标/键盘/UIA。 |
| A-003 | P1 | DONE | 自动化探索桌面工作台 | 新增“自动化探索”页面：窗口选择、UIA 树、截图、元素查询、点击/输入/设值/选择/toggle/keypress/read、历史、DSL 草稿。 |
| A-004 | P1 | DONE | 文本断言闭环 | 新增 assert.ui 探索工具，现场读取并校验，成功后直接固化为正式 DSL assert.ui。 |
| A-005 | P1 | DONE | 窗口发现与精确附加 | app.list + HWND 精确 attach；避免同标题窗口匹配歧义，同时保留标题正则用于 DSL 固化。 |
| A-006 | P1 | DONE | Explore 历史纠错 | 支持删除单条探索历史并重排索引，不再只能整段清空。 |
| A-007 | P1 | DONE | 工具 capability 裁剪 | Agent/UI 只看到当前驱动真正支持的工具，避免“工具可见但调用必失败”。 |
| A-008 | P1 | DONE | 开始测试执行前确认 | 补齐驱动模式、失败用例复跑、禅道同步、执行摘要，并把选择持久化到 sessionStorage。 |
| A-009 | P1 | DONE | 自动化 UI 可视化优化 | 选中元素会在截图上高亮；动作按钮按元素能力禁用；动作后刷新仍保持稳定元素选中。 |
| A-010 | P1 | DOING | CI 单测门禁 | 新增 GitHub Actions：Windows Python 单测+ruff、Desktop TypeScript/Vite build；不跑 smoke、不打包。 |

## 当前开放缺陷 / 不足

### P1 — 优先继续补功能

| ID | 状态 | 缺口 | 影响 | 下一步 |
|---|---|---|---|---|
| A-101 | TODO | Desktop Explore 仍以 UIA element 为主，视觉 desc/template 定位没有完整操作面板 | Qt Quick Canvas、自绘控件、无 Accessibility 控件的探索效率低 | 增加“视觉定位”模式：截图选区/语义描述/模板定位，并可作为 fallback 固化到 DSL。 |
| A-102 | TODO | 探索会话仅内存保存 | 引擎重启后历史丢失，长探索无法恢复 | 增加 session save/load/export；优先 JSON 文件或 DB 独立表，不污染正式 DSL。 |
| A-103 | TODO | device.scene/device.call/device.state 后端工具已有，桌面 Explore 未提供专门 UI | 涉及模拟器/设备状态的路径仍需 MCP/手写参数 | 增加设备面板、场景下拉、指令 schema 表单、状态查看。 |
| A-104 | TODO | Explore 只新增了文本断言；状态断言、视觉断言、截图 diff 未可视化 | 生成用例的“预期结果”能力仍不完整 | 增加 assert.state / assert.vision / screenshot_diff 的探索工具与 UI。 |
| A-105 | TODO | 历史可删除但不能拖拽排序、就地编辑参数 | 误顺序或参数修改仍需删掉重做/进 DSL 页改 | 增加 reorder / edit-step RPC；编辑后重新做 DSL semantic validation。 |
| A-106 | TODO | 自动生成 locator 只用 AutomationId 或 Name，尚无层级/父子上下文 | AutomationId 缺失且同名元素多时稳定性不足 | 引入 selector score：AutomationId > Name+ControlType > parent chain；低稳定度标黄人审。 |
| A-107 | TODO | 同一 RPC service 只有一个全局 Explore session | 多窗口/多工作流并行探索会互相覆盖上下文 | 引入 sessionId，桌面默认一个，MCP/Agent 可创建多个逻辑 session。 |

### P2 — 体验 / 性能 / 工程债

| ID | 状态 | 缺口 | 下一步 |
|---|---|---|---|
| A-201 | TODO | 截图直接 base64 放 JSON-RPC，4K/高频观察开销大 | 大图改临时 asset 引用；UI 侧按需读取，保留小图 inline。 |
| A-202 | TODO | Explore 直接依赖 UIADriver 的 _top/_rect_of 私有实现 | 抽出 ObservationDriver/AccessibilityDriver 可选协议。 |
| A-203 | TODO | keypress 仍要求 pywinauto 原始键序列语法 | UI 提供常用键组合选择器，并保留高级原文输入。 |
| A-204 | TODO | 元素树缺少折叠、仅显示扁平层级缩进 | 增加节点折叠、只看可操作元素、按 action/control type 过滤。 |
| A-205 | TODO | 截图缺少缩放/拖拽/点击截图反查元素 | 增加 zoom/pan 与坐标命中最近 UIA 元素。 |
| A-206 | TODO | Workflow 被测程序路径/窗口配置仅 sessionStorage | 增加“最近目标/目标预设”，可按项目复用。 |

## 环境验证项（当前不阻塞功能开发）

| ID | 状态 | 项目 |
|---|---|---|
| E-001 | ENV | Windows 真机 Qt Widgets / Qt Quick UIA 路径回归 |
| E-002 | ENV | 125% / 150% / 200% / 多屏 DPI 截图与 UIA rect 对齐 |
| E-003 | ENV | 干净 Windows 机器安装包安装/卸载/sidecar 启动 |
| E-004 | ENV | 真设备 + 模拟器场景联合 smoke |

## 执行原则

1. P1 功能优先，能用单测/契约测试覆盖的先覆盖。
2. 每补一个跨层 RPC，同步更新 RpcService.methods()、bridge.ts、devMock 和契约测试。
3. Explore 的临时 element_index 永远不能进入 active DSL；固化必须转稳定 locator。
4. 自动化探索和正式回归执行永远互斥，避免争抢桌面输入。
5. 真机 smoke 暂不作为提交阻塞项；待 P1 功能基本收口后集中验证。

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
| A-010 | P1 | DOING | CI 单测门禁 | 新增 GitHub Actions：Windows Python 单测+ruff、Desktop TypeScript/Vite build；不跑 smoke、不打包。此前已确认 Desktop build 成功；当前等待本批最新提交完整 run。 |
| A-011 | P1 | DONE | 视觉探索模式 | Desktop 支持 visual desc/template 定位、点击与输入；视觉动作固化为 vision_only，避免回归时误走 UIA。 |
| A-012 | P1 | DONE | 状态/视觉/截图差异断言 | assert.state / assert.vision 已可现场验证并固化；UI 动作可选择 screenshot_diff 并现场验证画面变化。 |
| A-013 | P1 | DONE | 探索会话恢复 | RPC 支持 restore；Desktop 自动将探索历史压缩保存到 localStorage，并可在引擎重启后恢复。 |
| A-014 | P1 | DONE | 探索步骤编辑与排序 | history 支持参数编辑、上移/下移、删除；固化顺序实时跟随历史。 |
| A-015 | P1 | DONE | Locator 稳定度与父级链 | observation 给 selector 打分；重复元素自动加入 ancestors 父级作用域链，同父级仍重复时退到局部 index。 |
| A-016 | P1 | DONE | 探索历史压缩 | observe/query/screenshot 的历史只保存摘要，不把 PNG base64 与整棵 UI 树长期塞进会话。 |

## 当前开放缺陷 / 不足

### P1 — 优先继续补功能

| ID | 状态 | 缺口 | 影响 | 下一步 |
|---|---|---|---|---|
| A-107 | TODO | 同一 RPC service 只有一个全局 Explore session | 多窗口/多工作流并行探索会互相覆盖上下文 | 引入 sessionId，桌面默认一个，MCP/Agent 可创建多个逻辑 session。 |
| A-108 | TODO | 视觉探索已有 desc/template，但还不能在截图上框选/点击生成 region 或模板 | 视觉 locator 仍需手填描述/模板路径 | 增加截图 click-to-target、框选 region、选区保存模板。 |
| A-109 | TODO | 探索步骤参数编辑不会重新执行动作 | 参数修正后只有固化文本变化，执行证据仍是旧参数 | 提供“编辑并重跑”动作；保留旧证据并追加新 attempt，而不是覆盖历史。 |

### P2 — 体验 / 性能 / 工程债

| ID | 状态 | 缺口 | 下一步 |
|---|---|---|---|
| A-201 | TODO | 截图直接 base64 放 JSON-RPC，4K/高频观察开销大 | 大图改临时 asset 引用；UI 侧按需读取，保留小图 inline。 |
| A-202 | TODO | Explore 直接依赖 UIADriver 的 _top/_rect_of 私有实现 | 抽出 ObservationDriver/AccessibilityDriver 可选协议。 |
| A-203 | DONE | keypress 常用键预设 | Explore UI 提供 Enter、Tab、Esc、Ctrl+A/C/V/S、F5 快捷填充，并保留高级原文输入。 |
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

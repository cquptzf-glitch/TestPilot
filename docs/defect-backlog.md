# TestPilot 缺陷与改进台账

> 更新时间：2026-10-23  
> 当前策略：**先堆功能 + 单元测试/契约测试 + 前端编译门禁；暂不把真机 smoke 作为开发阻塞项。**  
> 真机/UIA/DPI/干净机安装验证保留，但放到功能面稳定后集中执行。  
> 最新代码门禁证据：GitHub Actions run 39936778196 / SHA 8c5f884c9fad5d2125c512edb064ee3d5d1ab1a0 全绿（Desktop TypeScript/Vite、Ruff、全量 Pytest）。

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
| A-010 | P1 | DONE | CI 单测门禁 | GitHub Actions 持续执行 Windows Python unit+ruff 与 Desktop TypeScript/Vite build；不跑 smoke、不打包，已多轮实际绿灯验证。 |
| A-011 | P1 | DONE | 视觉探索模式 | Desktop 支持 visual desc/template 定位、点击与输入；视觉动作固化为 vision_only，避免回归时误走 UIA。 |
| A-012 | P1 | DONE | 状态/视觉/截图差异断言 | assert.state / assert.vision 已可现场验证并固化；UI 动作可选择 screenshot_diff 并现场验证画面变化。 |
| A-013 | P1 | DONE | 探索会话恢复 | RPC 支持 restore；Desktop 自动将探索历史压缩保存到 localStorage，并可在引擎重启后恢复。 |
| A-014 | P1 | DONE | 探索步骤编辑与排序 | history 支持参数编辑、上移/下移、删除；固化顺序实时跟随历史。 |
| A-015 | P1 | DONE | Locator 稳定度与父级链 | observation 给 selector 打分；重复元素自动加入 ancestors 父级作用域链，同父级仍重复时退到局部 index。 |
| A-016 | P1 | DONE | 探索历史压缩 | observe/query/screenshot 的历史只保存摘要，不把 PNG base64 与整棵 UI 树长期塞进会话。 |
| A-017 | P1 | DONE | 滚动与拖拽完整链路 | ui.scroll / ui.drag 已覆盖 UIA+Vision Driver、Explore、正式 DSL、Executor、Desktop、AI Prompt、StepEditor 与单测；当前 main CI 已全绿。 |
| A-018 | P1 | DONE | 右键上下文动作 | ui.right_click 已覆盖 UIA+Vision Driver、Explore、正式 DSL、Executor、Desktop、AI Prompt、StepEditor 与单测；当前 main CI 已全绿。 |

## 当前开放缺陷 / 不足

### P1 — 优先继续补功能

| ID | 状态 | 缺口 | 影响 | 下一步 |
|---|---|---|---|---|
| A-107 | DONE | 多探索会话隔离 | RPC 按 sessionId 隔离 ToolBox/历史/snapshot（最多 16 个）；Desktop 支持新建、切换、关闭并按会话独立恢复。 |
| A-108 | DONE | 视觉框选与模板保存 | 截图可拖拽生成 region，region 会固化到视觉 locator；选区可直接保存到 Vision template_dir 并回填模板名。 |
| A-109 | DONE | 探索步骤编辑并重跑 | 历史支持直接重跑或“保存并重跑”；旧步骤证据保留，新执行追加记录并标记 rerun_of。 |

### P2 — 体验 / 性能 / 工程债

| ID | 状态 | 缺口 | 下一步 |
|---|---|---|---|
| R-101 | DONE | RPC 数值分页参数统一边界 | _bounded_int / _optional_positive_int / _parse_int_value 统一处理 limit/offset/attempt 等参数；非法字符串、bool、小数、0/负数映射 INVALID_PARAMS；数字字符串兼容，大值受限；run.list 与相关 RPC 已补回归。 |
| A-201 | DONE | 大截图资产化传输 | 128KB 以下继续 inline；大图写入 screenshot_root/_explore/<sessionId> 并返回 asset_path，Desktop 走壳白名单读取；会话 reset/close 自动清理。 |
| A-202 | DONE | ObservationDriver 公共协议 | core 定义可选 ObservationDriver；UIADriver 提供 observation_root/observation_rect，Explore 不再读取 _top/_rect_of 私有字段。 |
| A-203 | DONE | keypress 常用键预设 | Explore UI 提供 Enter、Tab、Esc、Ctrl+A/C/V/S、F5 快捷填充，并保留高级原文输入。 |
| A-204 | DONE | UIA 元素树筛选与折叠 | 支持父节点折叠、只看可操作元素、按 action/control type 过滤，并保留层级缩进。 |
| A-205 | DONE | 截图缩放/平移/反查元素 | 预览支持 50%~250% 缩放、Alt+拖拽平移；UIA 模式点击截图反查最深层元素，Vision 模式拖拽框选区域。 |
| A-206 | DONE | 被测软件目标预设 | Workflow 可保存最多 12 个常用目标（名称/窗口标题/EXE），支持套用、更新、删除并通过 localStorage 跨重启复用。 |

## 流程 / UI / 执行链专项

| ID | 优先级 | 状态 | 项目 | 说明 |
|---|---:|---|---|---|
| F-101 | P0 | DONE | Run progress 事件契约 | phase 从 stats 内层修正为 payload 顶层，Desktop 执行条、Runs、Tasks、RunDetail 恢复真正事件驱动，不再依赖轮询兜底。 |
| F-102 | P0 | DONE | Driver capability-aware fallback | 动作前先按 capability 筛驱动；能力缺失安全跳到后续驱动；真正动作失败后仍保持副作用保护。ui.wait gone 同步修复。 |
| F-103 | P1 | DONE | Router 学习样本去重 | “定位成功 + 动作失败”一次尝试只写 1 条 fail，不再同时 success+fail；定位耗时口径保持一致。 |
| F-104 | P1 | DONE | Task 配置优先级与范围 | global < suite < explicit task；suite/caseIds 互斥并有序去重；显式 driver/remote 与 UI 所见一致。 |
| F-105 | P1 | DONE | Workflow 重解析与审核队列 | 只重解析真正需要的用例；批量 DSL 审核通过后自动下一条，并基于最新 case.list 推进，消除旧快照回跳竞争。 |
| U-101 | P1 | DONE | 日常主路径收敛 | 日常回归统一为“开始测试 → 必要解析/审核 → 执行 → 结果”；任务/方案降为高级复用入口。 |
| U-102 | P1 | DONE | 渐进式 UI | Cases、Workflow、SVN、Automation、Tasks、Settings 把低频/高级操作折叠；RunDetail 改 result-first 并按需加载诊断数据。 |
| U-103 | P1 | DONE | LLM/DSL 动作表单一来源 | llm_gateway 直接复用 core.dsl.KNOWN_ACTIONS，消除 Prompt / DSL / 归一化动作集合漂移。 |
| U-104 | P2 | DONE | 高频操作继续收敛 | Workflow 审核 chip 可直接跳转/回看；Cases 删除重复同步态下拉并补齐“清除筛选”的分组清理；后台 CaseJobs terminal 状态只保留一个“继续审核/查看生成用例”主动作，Agent 轨迹降为“查看详情”。仅 Desktop 改动，无新增 RPC/后端结构。Actions run 39934011635 / SHA 8f8110e70337eabb2f64a22173a1100cfc59cf9b 全绿。 |
| U-105 | P2 | DONE | 探索 / Run 结果继续减噪 | Automation 的视觉断言只在 Vision 模式显示；清空会话复用 ConfirmBar 二次确认。Run 概览提供第一条 fail/blocked 的“查看失败/阻塞轨迹”直达按钮，并去掉概览顶部重复 PassBar（其他 Tab 仍保留）。全部复用现有前端状态/函数，无新增 RPC。Actions run 39936778196 / SHA 8c5f884c9fad5d2125c512edb064ee3d5d1ab1a0 全绿。 |

## 禅道 / Agent / SVN 专项

| ID | 优先级 | 状态 | 项目 | 说明 |
|---|---:|---|---|---|
| Z-101 | P1 | DONE | 禅道真正增量同步 | CaseService 使用按产品隔离 watermark 调用 fetch_cases(since)，只对新增/变更候选补详情；保留 full=true 全量校准。 |
| Z-102 | P1 | DONE | 来源级同步状态 | 新增 source_sync_state；0 变更同步仍记录 synced_at/watermark，UI 不再拿单条 case.last_synced_at 冒充全局同步时间。 |
| Z-103 | P1 | DONE | 增量/全量 UI 语义 | 用例页显式提供“增量同步”和“全量校准”；CLI 增加 case sync --full。 |
| Z-104 | P0 | DONE | 远端测试单创建崩溃幂等 | Orchestrator 上提 creation_attempted/creation_uncertain fail-closed 门禁；POST 已发生但本地未保存远端 ID 时禁止自动重建；远端已就绪但未执行时状态回到 synced；补偿/核对与正式 run 共用生命周期锁，避免快照撕裂。Actions run 39892332857 / SHA ab5ac610f885422323cd493e837307d33ab47df8 全绿。 |
| AG-101 | P0 | DONE | Agent 工具统一主 RPC 执行 | Pi 禁用 built-in/MCP/skills；自定义 tp_* 工具只做 RPC proxy；主进程执行 fs/SVN/case 读取。 |
| AG-102 | P0 | DONE | Agent capability 隔离 | 每次 Agent 会话使用短期 workspace-bound token，仅允许 agent.* 内部方法；不暴露桌面主 Token。 |
| AG-103 | P1 | DONE | SVN 变更级联生成 | SVN snapshot → Pi 影响分析 → Pydantic 校验 → 主进程落本地 draft → 可选解析 DSL → 人审。 |
| AG-104 | P1 | DONE | Agent 后台 Job | change.svn.generate 只能通过 job.start 运行，支持进度/取消并避免 RPC 单线程回调死锁。 |
| AG-105 | P1 | DONE | 桌面 SVN 变更工作流 | 新增“用例与审核 → SVN 变更生成”页：预览变更、revision 范围、最大用例数、Agent 生成、DSL 审核跳转。 |
| AG-106 | P1 | DONE | Agent/SVN 单测与 CI | revision/path 越权、RPC capability、输出 schema、来源追踪、上下文预算、Pi CLI 兼容均有回归；Actions run 39884433953：Python unit+ruff / Desktop build 全绿。 |
| AG-107 | P1 | DONE | Agent 最小权限与 Pi 兼容硬化 | 工具白名单 fail-closed；空列表=0 工具；Pi flags 按 --help 选择安全兼容项；Node 20.10/21 才注入原生 WebSocket 实验开关，22+不注入。 |
| AG-108 | P1 | DONE | SVN Agent 生成幂等 | 相同 SVN 变更 + 相同测试语义按稳定 fingerprint 复用已有 case；同一 Agent 回合重复 intent 主进程直接去重，避免 -2/-3 重复草稿。Actions run 39885272988 全绿。 |
| AG-109 | P1 | DONE | Agent 工具结构化审计 | 每个 capability 会话由主 RPC 记录最多 200 条 agent.* 调用轨迹：方法、裁剪后的输入摘要、成功/失败、耗时与结果元数据；源码/diff 正文不重复入审计。SVN 生成结果可直接展开轨迹。Actions run 39914605156 / SHA c7c318480e1e9b6967267526c958d17f7b1db602 全绿。 |
| AG-110 | P0 | DONE | Agent progress 契约与失败现场保留 | 修复真实 Pi `phase=agent_tool` 经 ChangeAnalysisService 二次传参导致 `multiple values for argument 'phase'` 的 P0 缺陷；Agent 异常、无效 JSON/Schema、取消均在 token 吊销前把主 RPC audit 快照写入 Job progress，桌面页与全局后台任务面板均可展开失败/终止前轨迹。 |
| AG-111 | P1 | DONE | Agent/SVN 参数 fail-fast | `parse/maxCases/max_chars` 不再使用宽松 `bool()/int()` 转换；拒绝 bool、分数、非法下界；空 find/grep/case search 与非字符串 diff paths 直接拒绝，svn.log limit 与工具 schema 统一到 100。Actions run 39916844147 / SHA 03b47bf4f691fcdc1d56c4eddf1377f991481b27 两个门禁 Job 均 success。 |
| AG-112 | P1 | DONE | Agent 生成依据 fail-closed | 主 RPC 新增独立于 200 条展示 audit 的 evidence 摘要，持续记录成功/失败工具、实际 `fs.read` 路径、`case.search/detail` 见过的 case、完整/截断 SVN diff 与按路径 diff 覆盖。生成前必须成功读取 diff；`source_files` 必须被 diff/read 真实覆盖，unversioned 必须 `fs.read`；`related_case_ids` 必须真实存在且本轮 Agent 看过。所有用例统一预检后再写库，避免半提交；新建 case 将证据写入 `raw.generation.grounding`。Actions run 39919509437 / SHA e22cf2530d3d0e92a79a8d47cc542182bc06fac9 全绿。 |
| AG-113 | P1 | DONE | SVN Agent 分析快照漂移保护 | Agent 启动前对完整 SVN diff、revision/repository/status 与 unversioned 内容生成稳定 snapshot guard；返回后、任何落库前再次校验。主 RPC 对 `fs.read` 与 grep 实际暴露给 Agent 的文件记录内容 SHA，固定 revision 分析也能发现本地依赖源码变化；漂移时以 `snapshot_changed` fail-closed 并要求重跑。完整 diff 通过临时文件流式 SHA-256，unversioned 快照限制 10000 项/256 MiB，并校验 workspace/junction 边界；guard 持久化到 `raw.generation.snapshot_guard`。Actions run 39924766570 / SHA 6dbd2662d5e7e658be342840dce2a8ade74f22cc 全绿。 |
| AG-114 | P1 | DONE | 关联用例版本锁定与原子持久化 | `agent.case.search/detail` 返回并由主 RPC evidence 保存 `case_token`；token 对原文、DSL、preconditions、notes、人审/同步状态、accepted steps 等语义内容做 SHA-256，排除纯 `last_synced_at/updated_at` 刷新。grounding 与写事务开始后均复核关联用例 token。SVN Agent 草稿改为 `BEGIN IMMEDIATE` 整批写入，`create_case(commit=false)` 不再逐条提交；INSERT 后、COMMIT 前再次校验关联 case 与 SVN/source snapshot，任一步漂移整批 rollback。CaseService 长 LLM 解析也复用同一 semantic token。业务提交 63362085；最终测试收口 ce76e7a5 / Actions 39927463413 全绿。 |
| AG-115 | P2 | DONE | 轻量生成批次追踪 | 每次 SVN Agent 分析生成一个短 `generation_run_id`；本次结果中的 case 同时返回当前 grounding。新建 case 把 run id 写入 `raw.generation.run_id`，复用旧 case 时只返回本轮 provenance，不修改旧 case 内容，也不增加历史表/新 RPC。Desktop 仅在结果底部显示批次号。Actions run 39932264081 / SHA c1e0b57464b33e62d6e13c9d5c80e3eed34ef90c 全绿。 |

## 环境验证项（当前不阻塞功能开发）

| ID | 状态 | 项目 |
|---|---|---|
| E-001 | ENV | Windows 真机 Qt Widgets / Qt Quick UIA 路径回归 |
| E-002 | ENV | 125% / 150% / 200% / 多屏 DPI 截图与 UIA rect 对齐 |
| E-003 | ENV | 干净 Windows 机器安装包安装/卸载/sidecar 启动 |
| E-004 | ENV | 真设备 + 模拟器场景联合 smoke |
| E-005 | ENV | 公司 SVN + 实际 Pi provider/model 的端到端 Agent 变更分析 smoke（当前不阻塞功能开发） |

## 执行原则

1. P1 功能优先，能用单测/契约测试覆盖的先覆盖。
2. 每补一个跨层 RPC，同步更新 RpcService.methods()、bridge.ts、devMock 和契约测试。
3. Explore 的临时 element_index 永远不能进入 active DSL；固化必须转稳定 locator。
4. 自动化探索和正式回归执行永远互斥，避免争抢桌面输入。
5. 真机 smoke 暂不作为提交阻塞项；待 P1 功能基本收口后集中验证。
6. Agent 工具审计以主 RPC 实际执行为准；只保存摘要元数据，禁止把源码、diff 正文或 capability token 写入审计。
7. Agent 生成用例必须 fail-closed：模型文本不是证据，`source_files/related_case_ids` 必须由主 RPC evidence 证明；任何一条依据不成立时，整批在落库前拒绝。
8. SVN Agent 分析必须基于稳定快照：分析期间变更集、unversioned 内容或 Agent 实际观察过的源码发生变化时，不允许继续落库，必须重新分析。
9. Agent 引用已有用例时必须钉住 semantic token；SVN Agent 新草稿采用单事务批量持久化，并在 COMMIT 前完成最后一次 case + SVN/source 一致性校验。

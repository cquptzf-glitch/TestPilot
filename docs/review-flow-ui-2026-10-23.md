# 2026-10-23 全面代码逻辑 / 处理流程 / UI 操作流复审

> 目标：在不削弱 TestPilot 已有能力的前提下，减少重复解析、重复建任务、无效 RPC、平级按钮和页面跳转，让日常回归形成一条清晰主路径；高级能力保留但降级到二级入口。

## 1. 最终信息架构

### 1.1 日常回归主路径

```text
开始测试
  -> 选择用例
  -> 仅对“确实需要”的用例生成 / 更新 DSL
  -> 内嵌逐条审核（通过后自动下一条）
  -> 执行前确认
  -> 开始执行
  -> 执行与结果
```

普通使用者不再需要先理解“测试方案 / 测试任务”的区别，也不需要为了跑一次回归先手工创建方案和任务。

### 1.2 用例维护路径

```text
用例维护
  -> 拉取 / 增量同步 / SVN 变更生成
  -> 生成或更新 DSL
  -> 审核与编辑
```

同步、全量校准、SVN Agent 等能力仍保留，但不与日常执行入口竞争。

### 1.3 高级复用路径

```text
执行与结果
  -> 测试任务
  -> 测试方案
```

测试方案定位为“长期复用的固定用例集 + 默认参数”，测试任务定位为“可重复执行的快照”。两者都不是日常回归的前置条件。

---

## 2. 代码逻辑复审与修复

### 2.1 避免无意义 LLM 重解析

旧逻辑在“开始测试”中只要填写被测窗口 / 程序路径，就会把所选用例全部重新送进解析流程，即使某些用例已经 active、DSL 已审核且窗口完整。

现在只重解析：

1. 本身没有有效 DSL / 远端有更新的用例；
2. 确实缺被测窗口，并且本次提供了 appTarget 的用例。

已审核且目标完整的用例直接复用，减少 LLM 调用、等待时间和二次审核风险。

### 2.2 任务复用 fingerprint 修正

`caseRetry` 只影响一次 `task.run`，不改变任务快照本身。

旧 fingerprint 把 `caseRetry` 纳入，导致“0 次复跑 -> 1 次复跑”也重新创建任务。现改为只由：

- 用例 ID + DSL version；
- remote；
- driver mode；

决定任务是否可复用。

### 2.3 任务配置优先级明确

统一为：

```text
全局默认 < 测试方案默认 < 本次任务显式覆盖
```

并修复旧实现中“方案默认反向覆盖本次任务选择”的问题。

同时：

- `suite_id` 与 `case_ids` 禁止同时指定；
- case ID 按输入顺序去重；
- suite case ID 按顺序去重；
- 显式 `preconditions=[]` 不再被自动重新推导；
- task driver override 进入编排前校验合法值。

### 2.4 Task UI 与后端语义一致

任务页现在：

- “测试方案”和“临时用例”互斥；
- 驱动模式新增“使用方案 / 全局默认”；
- 显式选择 `auto` 就真的覆盖为 auto；
- “同步到禅道”始终显式传 true / false，不再因为参数省略而被全局默认反向开启。

避免“UI 看起来关闭，后端其实开启”的隐式行为。

### 2.5 禅道同步口径一致

`CaseService.sync()` 现在把最终解析出的 product ID 同时用于：

- 远端查询；
- watermark scope；
- source_sync_state。

`parse=true` 且没有待解析用例时，也返回稳定的批处理结构，而不是从对象结构退化成空字典。

### 2.6 Run 执行参数早校验

RPC 在进入执行器前校验：

- driver mode；
- caseRetry 0..3。

错误直接返回 INVALID_PARAMS，不再让非法参数深入执行链后才失败。

### 2.7 停止执行竞争窗口修复

顶部“停止执行”现在消费 `task.abort` 返回的 `stopping`：

- stopping=true：保持停止中状态；
- stopping=false：说明 run 可能在点击瞬间已经结束，立即回读 run.list 并重新同步顶部状态。

避免界面永久停在“正在停止…”。

### 2.8 Run progress 事件契约修复

这是本轮发现的关键流程缺陷。

旧事件实际形态：

```json
{
  "run_id": 12,
  "stats": {
    "phase": "finished"
  }
}
```

但桌面端一直读取 `payload.phase`，因此 start / finished 事件无法按设计被前端识别，只能被轮询间接纠正。

现在事件统一为：

```json
{
  "run_id": 12,
  "phase": "finished",
  "stats": {}
}
```

因此：

- 顶部执行条；
- Runs 自动刷新；
- Tasks 自动刷新；
- RunDetail 收尾刷新；

都真正按事件驱动工作。

### 2.9 Router 学习统计重复计数修复

旧执行链：

```text
定位成功 -> router success +1
动作失败 -> router fail +1
```

一次真实 driver attempt 被统计成两次样本。

现在一次尝试只写一次最终结果：

- 定位失败 = fail；
- 定位成功 + 动作失败 = fail；
- 定位成功 + 动作成功 = success。

`avg_locate_ms` 仍保持定位耗时口径，不混入动作耗时。

### 2.10 capability-aware fallback

旧逻辑会在“驱动能定位、但不支持动作”时过早失败，例如：

```text
vision_first + ui.select
vision 定位成功
vision 不支持 select
=> action_failed
=> 因担心副作用，不再尝试 UIA
```

但“能力缺失”发生在动作前，没有副作用风险。

现改为动作前 capability preflight：

- 不支持该动作的驱动直接跳过；
- 后续支持该动作的驱动继续尝试；
- 只有真正执行动作后失败，才保持“不自动换驱动”的副作用保护；
- 所有驱动都不支持时返回 `capability_missing`。

同样修复了 `ui.wait state=gone` 在 `vision_first` 下被 Vision 的能力缺失提前击穿、无法落到 UIA 的问题。

### 2.11 DSL 动作白名单单一来源

复审 CI 时发现：`core.dsl` 已经支持 `ui.right_click / ui.scroll / ui.drag`，
Prompt 也会引导模型生成这些动作，但 `llm_gateway` 仍保存一份旧版
`KNOWN_ACTIONS` 副本。

这会造成：

- DSL 层认可；
- Prompt 层生成；
- LLM 归一化层却把它标成 uncertain。

现在 `llm_gateway` 直接复用 `core.dsl.KNOWN_ACTIONS`，动作表只保留一个权威来源。
对应测试按动作构造合法最小参数，避免 scroll / drag 因测试输入本身非法产生误报。

### 2.12 内嵌 DSL 审核队列使用最新状态推进

Workflow 之前在 `dsl.accept` 后调用异步 `cases.reload()`，然后立即基于旧
`chosen` 快照寻找下一条待审核用例。

在 reload 尚未完成的竞争窗口中，旧快照可能再次包含刚刚已经通过的用例，导致
“自动下一条”跳回旧项。

现在每次 accept 后：

1. 直接回读最新 `case.list`；
2. 只在本次 selection 中寻找仍未 approved 的下一条；
3. 无待审核项时进入执行阶段；
4. 后台再刷新页面缓存。

因此批量审核不再依赖 React 异步刷新时序。

---

## 3. UI / UX 复审与简化

### 3.1 主导航从“功能清单”改成“用户目标”

主导航收敛为 6 项：

1. 开始测试
2. 自动化探索
3. 用例维护
4. 执行与结果
5. 问题与知识
6. 设置

“测试任务 / 测试方案”进入“执行与结果”的二级 Tab。

### 3.2 用例维护页去工具箱化

默认只保留：

- 开始测试；
- 生成 / 更新 DSL；
- 审核 DSL。

以下能力降为二级：

- 拉取指定禅道用例；
- 增量同步；
- 全量校准；
- 生成全部待更新；
- 创建独立任务；
- 保存测试方案。

行尾动作也从多个按钮压缩为单一上下文动作。

### 3.3 开始测试页渐进式披露

执行前默认只展示：

- 用例数；
- 总步骤；
- 当前驱动模式摘要；
- 失败复跑摘要；
- 开始执行。

驱动方式、失败复跑、禅道同步收进“执行选项”。

被测软件设置在缺窗口信息时自动展开，否则保持次要。

### 3.4 DSL 批量审核减少一次点击

队列审核中：

```text
保存并通过 -> 自动进入下一条
```

最后一条完成后提示“本批已全部审核完成”。

### 3.5 SVN 变更生成改成单主动作

旧页面同时摊开“配置 / 快照 / Agent 结果”三块空区域。

现在：

- 顶部配置范围；
- 主按钮“分析变更并生成测试用例”；
- “仅预览修改”为次操作；
- 有快照才展示快照；
- 有结果才展示结果。

### 3.6 Settings 主操作改为保存并重启

日常配置：

```text
修改 -> 保存并重启
```

“仅保存”保留给需要连续修改多个 Tab 的场景。

### 3.7 自动化探索按频率分层

UIA 第一层：

- 点击；
- 输入；
- 选择；
- 读取；
- 断言文本。

高级区：

- right click；
- SetValue + 回读；
- toggle；
- keypress；
- scroll；
- drag；
- screenshot diff。

Vision 同样把模板、稳定 key、region、diff、手势动作降为高级区。

会话的新建 / 关闭 / 清空也收进“会话管理”。

### 3.8 Run 结果页改成 result-first

默认概览只突出：

- 通过 / 失败 / 阻塞 / 跳过；
- 与基线上一次 run 的变化；
- 失败原因；
- 抖动 / 复跑 / KB 命中提示。

驱动统计、环境、LLM、env_compat 统一进入折叠的“技术诊断”。

同时修掉一个误导项：旧“最慢步骤 TOP”实际只统计当前默认选中的单个 case，却在 run 概览里表现成全 run 统计，因此删除。

### 3.9 诊断数据按需加载

以下 RPC 不再进入 RunDetail 就立即请求：

- `run.stepTrace`：只有进入轨迹 Tab 才加载；
- `kb.caseCtxList`：只有轨迹 Tab 才加载；
- `kb.envCompat`：只有打开技术诊断才加载。

减少页面初次打开的无效计算和数据库查询。

### 3.10 后台 job 面板降低常驻成本

旧 CaseJobsPanel 无论是否有任务，都每秒请求一次 `job.status`，并且完成结果会长期占据页面。

现在：

- active job：1 秒轮询；
- idle / terminal：5 秒低频巡检；
- 本前端启动 job 时通过事件立即刷新；
- 完成 / 失败结果支持手动关闭。

---

## 4. 不做的简化

本轮不是“删功能”，以下能力保留：

- 测试方案；
- 独立测试任务；
- 禅道远端任务；
- 全量校准；
- Vision / UIA / Device 高级动作；
- run 环境、驱动、LLM 诊断；
- DSL JSON 级编辑；
- KB / env_compat。

原则是：**默认路径只呈现当前决策所需信息，高级能力按需展开。**

---

## 5. 当前建议的页面职责

| 页面 | 只负责什么 |
|---|---|
| 开始测试 | 一次回归从选用例到执行 |
| 自动化探索 | 人工/Agent 辅助探索并固化 DSL |
| 用例维护 | 来源同步、生成 DSL、进入审核 |
| SVN 变更生成 | 代码变更 -> 测试意图 |
| 审核与编辑 | DSL 人审与修订 |
| 执行记录 | 历史 run 列表 |
| RunDetail | 结果、回归变化、失败取证 |
| 测试任务 | 高级可重复执行快照 |
| 测试方案 | 高级可复用测试集合 |
| 问题与知识 | 失败知识、人审上下文 |
| 设置 | 配置与运行自检 |

---

## 6. 回归测试新增重点

本轮新增 / 调整测试用于固定：

- task override 优先级；
- suite / direct case scope 互斥；
- case ID 去重；
- 非法 task driver mode；
- 非法 run mode / caseRetry；
- Zentao effective product scope；
- 空 parse batch 返回结构；
- router action failure 只记一次样本；
- wait_gone capability fallback；
- 通用 UI action capability fallback；
- clear-input capability fallback；
- run.progress 的 phase 顶层事件契约。

---

## 7. 后续边界

代码层下一优先级不建议继续扩大 UI 功能，而应做真实场景验证：

1. Qt Widgets / Qt Quick 实际桌面回归；
2. 125 / 150 / 200% DPI；
3. 双屏 / 负坐标 / 混合 DPI；
4. window move / occlusion；
5. Explore 中动态控件销毁后的 stale snapshot；
6. 干净机器安装验证。

当前阶段继续遵循：正常回归执行必须保持确定性 DSL 主链，Agent / LLM 只参与生成、探索、归因等辅助路径。


---

## 8. 最终自动验证

当前最新业务代码基线（包含后续 case readiness 集中化、Agent 主 RPC 审计、失败诊断、参数 hardening、grounding fail-closed 与 SVN snapshot 漂移保护增量）：

```text
6dbd2662d5e7e658be342840dce2a8ade74f22cc
```

GitHub Actions run：

```text
39924766570
```

结果：

- Desktop TypeScript typecheck + Vite build：**success**；
- Ruff：**success**（All checks passed）；
- 全量 Pytest：**success**；
- Python 与 Desktop 两个实际门禁 Job：**success**。

该流程/UI复审阶段已经收口；后续若继续修改业务代码，必须作为独立增量补测试并重新取得全绿 CI。当前后续增量已按此执行。

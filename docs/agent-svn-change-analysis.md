# Agent Runtime 与 SVN 变更级联测试设计

> 日期：2026-10-23  
> 状态：后端与桌面工作流已落地；当前开发阶段以单元测试/契约测试/前端编译为门禁，真机 smoke 暂缓。

## 1. 目标

TestPilot 新增“从 SVN 修改级联生成测试用例”能力，同时把所有需要多轮工具调用、代码库浏览和影响分析的智能流程统一到 **Agent Runtime**。

默认 Agent Runtime 使用 **Pi Coding Agent**，但业务代码只依赖 `AgentPort`，不直接依赖 Pi。

适用场景包括：

- SVN revision / 工作副本修改 → 影响分析 → 新增回归用例；
- 后续需求文档 → 影响模块 → 测试用例；
- 缺陷/问题单 → 回归用例；
- 代码变更 → 已有用例覆盖缺口分析；
- 复杂失败调查等需要多轮检索的流程。

简单、一次性的结构化转换仍可继续使用 `LlmGateway`。**确定性测试执行主干不依赖 Agent/LLM。**

## 2. 最重要的安全边界

### 2.1 Agent 不直接拥有任何读写/操作工具

Pi 启动时明确关闭：

- built-in tools；
- MCP；
- skills；
- context files；
- prompt templates；
- 自动加载 extension。

然后只显式加载 TestPilot 的 `pi_rpc_tools.mjs`。

因此 Pi 自身不能直接：

- 读文件；
- grep/find；
- 调 shell；
- 执行 svn；
- 编辑/创建/删除文件；
- 访问数据库；
- 执行测试任务；
- 修改 TestPilot 配置。

### 2.2 所有工具调用必须回到 TestPilot 主 RPC

数据流：

```text
Pi Agent
  │  Tool Call: tp_svn_diff / tp_read_file / tp_case_search ...
  ▼
pi_rpc_tools.mjs
  │  WebSocket JSON-RPC + scoped capability token
  ▼
TestPilot Main RPC
  ├─ agent.fs.*
  ├─ agent.svn.*
  └─ agent.case.*
       │
       ├─ 路径/工作区校验
       ├─ SVN 参数校验
       ├─ 输出体积限制
       ├─ 日志/审计
       └─ 真正执行
```

Pi 不持有桌面主 RPC Token。

每个 Agent 流程由主进程签发一个短生命周期 capability token。Pi 的原生 WebSocket
无法设置自定义 Authorization header，因此扩展使用本机回环 RPC URL 的
`?agent_token=<ephemeral>`；主进程仍兼容 Bearer 形式，但两者最终映射到同一份
workspace scope。该 token：

- 只允许 `agent.*` 内部方法；
- 绑定一个 canonical workspace；
- Agent 结束后立即吊销；
- 不能调用 `task.run`、`config.apply`、`dsl.accept` 等业务接口。

即使 Agent 构造越权 JSON-RPC，也会在 RPC 层拒绝。

## 3. 当前 Agent RPC 工具

| Pi 工具 | 主 RPC | 权限 |
|---|---|---|
| `tp_read_file` | `agent.fs.read` | 只读；仅工作区内 UTF-8 文本；2 MiB 上限 |
| `tp_find_files` | `agent.fs.find` | 只读；工作区 glob；跳过 .svn/.git/node_modules |
| `tp_grep` | `agent.fs.grep` | 只读；有文件数/命中数/单文件体积上限 |
| `tp_svn_status` | `agent.svn.status` | 主进程执行 svn status |
| `tp_svn_diff` | `agent.svn.diff` | 主进程执行受限 svn diff；默认 120k 字符，硬上限 200k，超限后按文件分片读取 |
| `tp_svn_log` | `agent.svn.log` | 主进程执行受限 svn log |
| `tp_case_search` | `agent.case.search` | 只读查询已有本地用例 |
| `tp_case_detail` | `agent.case.detail` | 只读读取单条用例原文/DSL |

当前没有任何 Agent 写工具。

未来如果需要 Agent 参与文件修改，也必须新增类似 `agent.fs.patch` 的主 RPC 能力，并单独定义：

1. 允许根目录；
2. 操作白名单；
3. 并发/版本检查；
4. 操作日志；
5. 人工确认或事务语义。

禁止把 Pi 原生 edit/write/bash 直接打开。

## 4. SVN 变更生成流程

### 4.1 输入

桌面“用例与审核 → SVN 变更生成”提供：

- SVN 工作副本；
- 起始 revision；
- 结束 revision；
- 最大生成用例数；
- 是否继续解析为确定性 DSL。

revision 均可为空：

- 两者为空：分析工作副本未提交修改；
- from + to：分析 revision 范围；
- 仅 to：按该 revision change；
- 仅 from：from → HEAD。

revision 仅允许数字以及 `HEAD/BASE/COMMITTED/PREV`，不接受任意命令字符串。

### 4.2 确定性快照

`SvnAdapter` 在主进程执行：

- `svn info --xml`；
- `svn status --xml`；
- `svn diff --summarize`；
- 必要时 `svn diff` / `svn log --xml`。

这一步先确定“发生了哪些修改”，再交给 Agent 研究级联影响。

### 4.3 Agent 影响分析

Agent 必须：

1. 先通过 `tp_svn_diff` 阅读修改；
2. 按需要通过 `tp_read_file/tp_grep/tp_find_files` 研究调用链和相关模块；
3. 通过 `tp_case_search/tp_case_detail` 检查已有覆盖，避免重复造用例；
4. 识别直接影响、调用链级联、公共模块影响、异常/边界/兼容风险；
5. 最终只返回结构化 JSON。

Agent 不能直接创建 TestPilot 用例。

### 4.4 主进程校验与落库

`ChangeAnalysisService` 使用 Pydantic 校验 Agent 最终输出：

- summary；
- impacted_modules；
- risks；
- cases；
- 每条 case 的 priority/category/rationale/preconditions/steps/source_files/related_case_ids。

校验通过后由 TestPilot 主进程创建本地草稿：

```text
SVN-<revision>-<signature>-NN
```

`raw_json` 会保存：

- `source=svn_agent`；
- SVN working copy/revision/repository；
- source_files；
- rationale；
- related_case_ids；
- analysis summary / risks。

此时 `status=draft`，不可执行。

若勾选“生成后继续解析 DSL”，再调用现有 `CaseService.parse_many` 把自然语言测试意图转换为确定性 DSL 草稿。

最终仍必须经过 `dsl.accept` 人工审核才能 active。

### 4.5 生成幂等与重复意图

Agent 输出不能直接决定“创建多少数据库行”。主进程会为每条测试意图计算稳定
`generation.fingerprint`，输入包括：

- repository UUID / root；
- revision 范围；
- source files；
- preconditions；
- 有序测试步骤。

priority / rationale 等易受模型措辞影响的元数据不进入 fingerprint。

因此：

1. 同一变更重复执行，若测试语义相同，直接复用已有 SVN Agent case；
2. 已有 draft 未解析且本次勾选“解析 DSL”，会解析这个复用草稿，不再新建一条；
3. active / rejected 等已有 case 也保持其人工状态，不被 Agent 覆盖；
4. 同一次 Agent 输出中出现完全重复 intent 时，主进程只保留一条，并计入
   `duplicates_dropped`。

返回结果显式区分：

- `created`：真正新增；
- `reused`：跨运行复用；
- `duplicates_dropped`：本轮 Agent 自身重复而被丢弃。

这样幂等性由 TestPilot 主进程保证，而不是依赖 prompt 让模型“尽量不要重复”。

## 5. Pi 集成方式

Pi 采用 RPC 子进程，而不是嵌入 Node：

Pi 各版本的禁用参数有演进，因此 TestPilot **不硬编码一组假定版本的 flags**，
而是启动前读取 `pi --help`，组装当前版本能支持的最严格命令：

```text
pi --mode rpc --no-session
   [--no-builtin-tools | --no-tools]
   [当前版本支持的 --no-mcp / --no-skills / --no-context-files / ...]
   --extension testpilot/agent/pi_rpc_tools.mjs
```

若当前 Pi 既不支持禁用内置工具，也不支持显式 extension，TestPilot 会拒绝启动，
不会为了兼容而降级成“Agent 可直接读写本机”。

`pi_rpc_tools.mjs` 不依赖 `undici/ws/node:fs/node:child_process`，只使用 Pi 提供的
TypeBox 与运行时原生 WebSocket。Node 20.10~21 会按实际版本启用
`--experimental-websocket`，Node 22+ 直接使用默认 WebSocket。

工具白名单采用 **fail-closed**：只有 TestPilot 本次工作流显式传入的 `tp_*` 才注册；
空白名单代表 0 个工具，而不是“注册全部工具”。

TestPilot Python 主进程：

1. 签发 capability token；
2. 启动 Pi RPC subprocess；
3. 发送 prompt；
4. 接收 Agent/Tool 事件；
5. Pi extension 回调主 RPC 执行工具；
6. 等待 `agent_settled`；
7. 获取最终 assistant text；
8. 吊销 token；
9. 结构校验、落库、人审。

官方参考：

- Pi Coding Agent: https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent
- Pi RPC mode: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md
- Pi extensions: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md

## 6. 配置

```yaml
agent:
  runtime: pi
  pi:
    executable: pi
    provider: ""
    model: ""
    timeout_s: 300

svn:
  executable: svn
  working_copy: ""
  timeout_s: 30
  max_output_chars: 800000
```

设置页“集成”Tab 可修改上述参数。

`doctor/health` 会显示 Pi 和 SVN CLI 的可用状态。

## 7. 后台任务与死锁约束

Agent 流程不能直接跑在普通 RPC 单线程 IO executor。

原因：

```text
普通 RPC executor
  └─ 等 Pi Agent
       └─ Pi tool call 回主 RPC
            └─ 若还要等同一个 executor -> 自锁
```

因此 `change.svn.generate` 只能通过 `job.start` 后台任务池执行。

`change.svn.preview` 不调用 Agent，可以作为普通 RPC。

桌面端可：

- 查看进度；
- 查看当前 Agent tool；
- 取消；
- 切页后继续查询；
- 完成后直接进入用例/DSL 审核。

## 8. 测试策略

当前不要求真实 Pi/SVN smoke 作为提交门禁。

自动化测试覆盖：

- SVN XML 解析；
- revision 注入拒绝；
- 工作区路径逃逸拒绝；
- Agent 输出 schema 校验；
- Agent 只得到 `tp_*` RPC proxy tools；
- Pi runtime 必须根据当前 CLI 选择 `--no-builtin-tools` 或 `--no-tools`，无法安全禁用时拒绝启动；
- extension 不得导入 `node:fs` / `node:child_process` / `undici` / `ws`；
- Agent 工具白名单为空时必须注册 0 个工具；
- Node WebSocket flag 必须按版本启用，不能污染 Node 22+；
- SVN diff Agent 上下文有独立 120k/200k 预算；
- scoped Agent RPC 不能调用非 `agent.*` 方法；
- Agent 不能直接写 executable DSL / active case；
- SVN 生成结果的来源追踪入库；
- Pi `agent_tool` progress 的 phase 透传不会发生重复关键字冲突；
- Agent runtime 异常、最终 JSON/Schema 无效、用户取消时，后台 Job 都保留终止前主 RPC audit；
- `parse/maxCases/max_chars` 严格类型/边界校验，bool/分数/非法下界拒绝；
- 空 find/grep/case search、非法 diff paths 不会退化成大范围扫描；
- Agent 未成功执行 `agent.svn.diff` 时整批拒绝，不允许只靠 prompt 自称“已阅读 diff”；
- 展示 audit 即使裁剪到最后 200 条，主 RPC evidence 仍保留关键成功计数、读取路径、已观察 case 与 diff 覆盖；
- `source_files` 必须有真实 diff/read 依据，unversioned 文件必须 `agent.fs.read`；
- `related_case_ids` 必须真实存在且本轮通过 `agent.case.search/detail` 观察过；
- 所有 grounding 在任何 case 写库前统一校验，失败不会留下半批草稿；
- 新建 SVN Agent case 会把 per-case grounding 持久化到 `raw.generation.grounding`。

后续环境验证统一列入 ENV：

- 公司 SVN 凭据/代理环境；
- 实际 Pi provider/model；
- 大 revision diff 的上下文与耗时；
- 真实项目级级联影响召回率。


## 9. 主进程结构化 Agent 工具审计

Agent 的工具调用现在不仅“经过主 RPC”，还由主 RPC 产生结构化审计证据。

记录点位于 JSON-RPC capability 边界：

```text
Pi tp_* tool
  -> agent.* RPC
  -> 主进程校验 capability / workspace
  -> 主进程执行 fs / SVN / case 查询
  -> 主进程记录 audit entry
  -> 返回摘要给 Pi
```

每条轨迹包含：

- `seq`：会话内完成序号；
- `method`：实际执行的 `agent.*` 方法；
- `params`：裁剪后的输入摘要；
- `ok`：主进程实际执行是否成功；
- `elapsed_ms`：主进程执行耗时；
- `result` 或 `error`：结果元数据或失败原因。

安全约束：

1. 审计由主进程产生，不信任 Agent 自报；
2. capability token、内部 `_agentScope`、workspace 注入字段不写入轨迹；
3. `fs.read` 不记录文件正文；
4. `svn.diff` 不记录 diff 正文，只记录 chars / truncated / path_count；
5. `case.detail` 不复制完整用例正文，只记录状态与 step_count；
6. 每个 Agent 会话最多保留 200 条，结束后 capability 与内存审计一起销毁；
7. SVN 变更生成完成后，将审计快照挂到 `result.agent.audit`，桌面端按需展开；
8. Agent runtime 异常或最终输出无效时，在 capability token 吊销前把 audit 快照写入 `job.progress.agent_audit`；用户取消也走同样的终态写入，但终态诊断不会再次触发 abort checkpoint；
9. 桌面 SVN 页面和全局 CaseJobsPanel 共用 `AgentAuditTrail`，成功结果默认折叠，失败/取消轨迹可直接展开。
10. 与展示 audit 分离维护 `agent.evidence`：记录每个 `agent.*` 方法 calls/successes/failures、实际成功读取的文件、Agent 观察过的本地 case，以及 SVN diff 是完整、截断还是按路径读取；因此关键证据不会因 200 条 UI audit 裁剪而消失。

这样真实 Pi/provider 联调时可以回答“Agent 实际调用了什么、主进程是否执行成功、耗时在哪里”，同时不会把大段源码再次复制到 UI 或任务结果中。

自动测试覆盖：

- 成功的 `agent.fs.read` 会生成结构化轨迹；
- 轨迹结果不包含 `content` 正文；
- workspace 越界失败同样记录 `ok=false + error`；
- token revoke 后审计不可继续读取；
- ChangeAnalysisService 会把主进程审计透传到最终 Agent 结果；
- 真实 `phase=agent_tool` progress 可贯穿 Pi runtime → ChangeAnalysisService → CaseJobs，不再因 `phase` 重复参数中断；
- Agent provider/runtime 失败会在 Job progress 中保留 `agent_failed + agent_error + agent_audit`；
- 最终 JSON/Schema 无效会保留 `agent_output_invalid + agent_audit`；
- 已请求取消时仍先落 `agent_cancelled + agent_audit`，再结束任务。

## 10. 生成用例 grounding 门禁

SVN Agent 的最终 JSON 现在只是“候选计划”，**不能直接成为落库依据**。在任何草稿写入前，`ChangeAnalysisService` 会使用主 RPC 产生的 evidence 做一次整批 preflight：

1. 本轮至少有一次成功的 `agent.svn.diff`；
2. `source_files` 至少 1 条；
3. 对普通 SVN 变更文件：
   - 完整且未截断的全量 diff 可以覆盖对应变更路径；
   - 若全量 diff 被截断，则该 source 必须再被按路径 diff，或通过 `agent.fs.read` 读取；
4. 对 `svn status` 中的 unversioned 文件：普通 SVN diff 不包含其正文，因此必须有成功的 `agent.fs.read`；
5. 对非本次变更但用于级联分析的源码：必须有成功的 `agent.fs.read`，并且文件在校验时仍真实存在；
6. `related_case_ids` 必须同时满足：
   - 本地数据库真实存在；
   - 本次 Agent 会话通过 `agent.case.search` 或 `agent.case.detail` 实际观察过；
7. 任一 case grounding 失败，整批在持久化前拒绝，不会出现“前几条已落库、后几条才失败”的半提交状态。

主 RPC evidence 与 UI audit 分离：audit 最多保留最后 200 条用于查看；evidence 用小型集合/计数保存关键事实，因此早期的 `svn.diff` 或 `case.search` 不会因为长会话裁剪而丢失。

新创建的 SVN Agent 用例会持久化 `raw.generation.grounding`，其中记录 `svn_diff_verified`、每个 `source_file` 的 `agent.svn.diff/agent.fs.read` basis，以及每个 `related_case_id` 的 `agent.case.search/detail` basis。

这样后续审核、问题追踪或真实 Pi/provider 调优时，不依赖后台 Job 仍在内存中，也能知道用例当时基于什么证据生成。

## 11. SVN 分析快照稳定性 / 漂移保护

Grounding 只能证明“Agent 看过什么”，还需要保证 **Agent 看过的内容在生成结果落库前没有变化**。当前 `change.svn.generate` 因此增加双阶段 snapshot guard：

```text
主进程建立初始 SVN snapshot
  -> 生成 snapshot_guard
  -> Pi Agent 通过主 RPC 研究 diff / source / cases
  -> 解析 Agent JSON
  -> 主进程重新读取 SVN snapshot + observed source hashes
  -> signature/hash 一致才进入 grounding 与持久化
```

### 11.1 snapshot_guard 内容

初始 guard 由主进程确定性生成，包含：

- from/to revision；
- repository root / UUID；
- working-copy revision；
- 排序后的 `svn diff --summarize` 变更路径；
- working-copy status；
- **完整 SVN diff 的 SHA-256 与字节数**；
- unversioned 路径及其内容摘要。

完整 diff 不经过 Agent 的 120k/200k 上下文裁剪。`SvnAdapter.diff_digest()` 把 SVN stdout 写入临时文件，再按 1 MiB 分块计算 SHA-256，因此既能覆盖超大 diff，又不会在 Python 内存中保留整份正文。

### 11.2 Agent 实际观察内容也纳入稳定性

固定 revision 分析本身的 SVN diff 可以保持不变，但 Agent 仍可能读取当前工作副本中的辅助源码。因此主 RPC 还记录：

- `agent.fs.read`：完整原始文件 SHA-256；
- `agent.fs.grep`：只有真正命中、把文本暴露给 Agent 的文件才记录完整文件 SHA-256；
- 同一路径被多次观察时，`content_hashes` 保持最后一次真实观察结果。

Agent 返回后，主进程重新计算这些文件的 hash。若任一文件被修改、删除或替换，即使固定 revision 的 SVN diff 没变化，也会以 `snapshot_changed` 拒绝本轮结果。

### 11.3 working copy 与 unversioned 内容

working-copy 模式除了完整 SVN diff/status 外，还对 unversioned 内容做摘要，因为普通 `svn diff` 不包含未版本控制文件正文。

为避免未忽略的构建目录把后台任务拖死，快照保护采用硬预算：

- 最多 10,000 个 unversioned 文件/目录条目；
- 最多 256 MiB unversioned 文件内容；
- 超限直接 fail-fast，提示配置 `svn:ignore` 或缩小工作副本；
- 递归目录与根目录都会校验 resolve 后仍位于 workspace 内，避免 symlink/junction/reparse 越界。

### 11.4 漂移时的行为

只要初始和最终 snapshot signature 不一致，或 Agent 实际观察过的源码 hash 改变：

1. 不创建/复用任何新的 SVN Agent case；
2. Job progress 写入 `phase=snapshot_changed`；
3. 保留 agent audit/evidence、前后 signature 摘要与 changed read paths；
4. 返回明确错误，要求重新执行变更分析。

通过校验后，新创建的用例会把本次 guard 持久化到 `raw.generation.snapshot_guard`，与 `raw.generation.grounding` 一起形成可追溯的“分析快照 + 生成依据”。

自动化回归覆盖包括：

- working-copy diff 内容在 Agent 分析期间变化时拒绝落库；
- 固定 revision 下，Agent `fs.read` 过的辅助源码变化时拒绝；
- grep 命中文件变化时拒绝；
- 同一路径多次观察以最后一次内容 hash 为准；
- unversioned 快照超过预算时在 Agent 启动前拒绝；
- Windows CRLF 文件按真实原始字节计算 hash；
- snapshot guard 持久化到生成用例。

## 12. 关联用例版本锁与原子持久化

源码 snapshot 稳定后，还需要解决另一类竞态：Agent 在分析时读取了已有测试用例，但该用例可能在 Agent 返回前或草稿落库前被禅道同步、LLM 重解析或人工审核修改。

### 12.1 case semantic token

仓储层提供统一 `case_semantic_token(case)`，对真正影响测试语义的内容做 canonical JSON + SHA-256：

- case/title/zentao id；
- DSL version / steps / preconditions / notes；
- status / sync_status；
- raw 原文；
- accepted_steps；
- zentao_updated_at；
- module id/name。

`last_synced_at` 与本地 `updated_at` 不进入 token。这样远端没有语义变化、仅刷新同步时间时不会误判；原文、DSL、人审状态等真正变化时 token 必然变化。

CaseService 原有长 LLM 解析的 `revision` 也改为复用同一 semantic token，因此 Agent 与普通 case parse 使用同一套乐观并发语义。

### 12.2 主 RPC evidence

`agent.case.search` 与 `agent.case.detail` 都把当前 `case_token` 返回给 Agent，并由可信主 RPC evidence 保存：

```json
{
  "case_ids": ["TC-1"],
  "case_tokens": {"TC-1": "<sha256>"}
}
```

同一个 case 被多次观察时保留最后一次主 RPC 返回的 token。最终 grounding 不再只判断“ID 存在 + Agent 看过”，还要求当前数据库语义 token 与 Agent 实际观察 token 完全一致。

### 12.3 两阶段 case 校验

关联 case 会校验两次：

1. Agent JSON 解析后的 grounding preflight：尽早发现分析期间已经变化的 case；
2. `BEGIN IMMEDIATE` 获得 SQLite 写锁后：再次比较 token，封住 preflight → persist 之间的数据库写竞态。

如果任一 related case 被删除、缺少 token 或语义 token 改变，整轮返回 fail-closed，要求重新分析。

### 12.4 草稿整批事务

SVN Agent 生成不再对每条 `repos.create_case()` 单独 commit。当前持久化顺序为：

```text
BEGIN IMMEDIATE
  -> 复核 related case tokens
  -> 在事务内重新建立 fingerprint 索引
  -> 整批 create/reuse，新增 case 使用 commit=false
  -> 再复核 related case tokens
  -> INSERT 后重新校验 SVN snapshot + Agent observed source hashes
COMMIT
```

任何一步抛错、用户取消或最终 snapshot 漂移，SQLite context 会 rollback 整批新增草稿。这样不会出现“前几条已经提交、后面才发现 case/source 变化”的部分提交状态。

把最终 SVN/source 校验放到 INSERT **之后、COMMIT 之前**，也把文件系统变化的竞态窗口压缩到最后一次 hash 校验到 SQLite COMMIT 之间的极小区间；在不对用户工作副本加文件锁的前提下，这是当前最稳妥的 fail-closed 边界。

自动回归覆盖：

- Agent 观察后 related case 原文发生变化时拒绝；
- grounding 完成后、写事务开始前 case 变化时，事务内二次检查拒绝；
- 最终 SVN snapshot 在 INSERT 后发生漂移时整批 rollback；
- semantic token 忽略纯同步/更新时间戳变化；
- semantic token 对原文语义变化敏感；
- 主 RPC evidence 在长会话 audit 裁剪后仍保存 case token。

## 13. 轻量生成批次追踪

为区分重复执行的 SVN Agent 分析，但避免引入 generation history 表或额外 RPC，当前只保留轻量 provenance：

- 每次 `change.svn.generate` 生成一个 16 字符 `generation_run_id`；
- 返回结果和每条本轮 case 都带该 run id；
- 每条返回 case 同时带本轮 grounding；
- 新建 case 把 run id 写入 `raw.generation.run_id`；
- 复用已有 case 时只在本次返回结果里带当前 run id/grounding，**不回写旧 case**；
- Desktop 只在分析结果底部展示批次号，不新增历史页面。

这样可以回答“这一屏结果属于哪次分析、这次复用基于什么证据”，同时保持现有数据库和 UI 结构不变。

## 14. 当前自动验证证据

GitHub Actions run `39884433953`（RPC/权限 hardening 基线 `4f287f7ad06538804b282c89c778702a30d2f639`）：

- Windows Python install：success；
- Ruff：success；
- 全量 Pytest：success；
- Desktop TypeScript + Vite build：success。

后续幂等生成基线 `721c86f8b47029150f6c893fbddff7146e91255d` 对应
GitHub Actions run `39885272988` 同样 Python / Ruff / Pytest / Desktop 全绿。

结构化 Agent 审计增量基线 `c7c318480e1e9b6967267526c958d17f7b1db602` 对应 GitHub Actions run `39914605156`：Desktop TypeScript/Vite、Ruff、全量 Pytest 全部 success。

Agent progress/失败诊断/参数边界增量基线 `03b47bf4f691fcdc1d56c4eddf1377f991481b27` 对应 GitHub Actions run `39916844147`：Desktop TypeScript/Vite Job 与 Python（Ruff + 全量 Pytest）Job 均 success。

Agent grounding fail-closed 与持久化 provenance 基线 `e22cf2530d3d0e92a79a8d47cc542182bc06fac9` 对应 GitHub Actions run `39919509437`：Desktop TypeScript/Vite、Ruff、全量 Pytest 全部 success。

SVN snapshot drift / observed-source hash / bounded unversioned guard 最终基线 `6dbd2662d5e7e658be342840dce2a8ade74f22cc` 对应 GitHub Actions run `39924766570`：Desktop TypeScript/Vite、Ruff、全量 Pytest 全部 success。中间 `2e09f3ab` 的 Windows 测试仅因 LF/CRLF 期望 hash 不一致失败，`424cc307` 改为按真实文件字节断言后全绿；生产实现始终按原始字节计算 SHA-256。

关联 case semantic token + 原子持久化业务基线 `63362085a524526a14dc48d78f7abfac9303e5ac` 对应 Actions run `39926984423` 全绿；测试语义收口 `ce76e7a5cf83bdf40379013d54f919ebf398b6f2` 对应 Actions run `39927463413` 同样 Desktop TypeScript/Vite、Ruff、全量 Pytest 全部 success。

轻量 generation run provenance 基线 `c1e0b57464b33e62d6e13c9d5c80e3eed34ef90c` 对应 Actions run `39932264081`：Desktop TypeScript/Vite、Ruff、全量 Pytest 全部 success。

真实公司 SVN、真实 Pi provider/model、真实大仓库级级联影响质量仍属于 ENV 验证，不作为当前功能开发阻塞项。

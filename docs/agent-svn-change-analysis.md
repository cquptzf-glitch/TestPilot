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
- SVN 生成结果的来源追踪入库。

后续环境验证统一列入 ENV：

- 公司 SVN 凭据/代理环境；
- 实际 Pi provider/model；
- 大 revision diff 的上下文与耗时；
- 真实项目级级联影响召回率。


## 9. 当前自动验证证据

GitHub Actions run `39884433953`（RPC/权限 hardening 基线 `4f287f7ad06538804b282c89c778702a30d2f639`）：

- Windows Python install：success；
- Ruff：success；
- 全量 Pytest：success；
- Desktop TypeScript + Vite build：success。

后续幂等生成基线 `721c86f8b47029150f6c893fbddff7146e91255d` 对应
GitHub Actions run `39885272988` 同样 Python / Ruff / Pytest / Desktop 全绿。

真实公司 SVN、真实 Pi provider/model、真实大仓库级联影响质量仍属于 ENV 验证，不作为当前功能开发阻塞项。

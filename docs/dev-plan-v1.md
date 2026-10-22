# TestPilot 开发方案与计划 v1.0

> 基线：[ai-autotest-design-v1.md](design/ai-autotest-design-v1.md) v1.8
> 状态：M1–M4 代码全部落地（668 单测全绿）；**剩余=外部实机验证**（干净机器安装 / UIA 实机 / 多缩放 DPI）| 日期：2026-09-11
> 当前进度快照与接手须知：[handoff.md](handoff.md)｜逐项变更与验证记录：[dev-log.md](dev-log.md)

## 1. 工程现状

| 项 | 状态 | 说明 |
|---|---|---|
| 目录骨架 | 完成 | core/engine/drivers/adapters/knowledge/storage/infra/report/app/config/tests/docs |
| core 契约 | 完成 | dsl(含 uncertain) / contracts(Protocol 端口 + NullAbort) / registry / errors / events |
| 执行域 | 完成 | executor / router(三级覆盖) / verifier / judge / orchestrator（本地优先建任务） |
| 驱动 | 完成 | uia / vision / emulator（GUI 依赖惰性导入；一致性门禁 `tests/conformance`） |
| 应用服务 | 完成 | `case_parser.py`(拉取/同步/解析/人审) / `suites.py`(方案) |
| 知识域 | 完成 | signature / service（精确+模糊匹配、置信衰减、元素库自动积累、路由统计增量均值） |
| 存储层 | 完成 | db(WAL + 幂等迁移) / recorder(轨迹全字段) / repos(全量 CRUD) / shotstore |
| 横切 | 完成 | abort(协作取消) / logsys(JSON 日志走 stderr + run.log 抽取) / events(事件总线) / **dpi(DPI 感知与坐标空间归一)** / winver(被测版本探测) / config |
| RPC | 完成 | 设计 2.5 方法表 **43 个** + 事件推送（阻塞方法走线程池）+ 跨语言契约门禁 |
| CLI | 完成 | db-init / doctor / case / suite / task / run / kb / serve / schedule |
| 调度 | 完成 | `app/scheduler.py`（run_once / run_forever / stop / status） |
| 桌面壳 | 完成（待联调） | `app/desktop/`：Tauri 2 壳（sidecar 监督/RPC 桥/资源白名单）+ React 18 八页；`packaging/` + `scripts/` 打包链路 |
| 单测 | **34 模块 / 668 用例全绿** | dsl / registry / signature / storage / knowledge（含**语义召回钩子**）/ llm（含**能力清单校验/裁剪**）/ case_parser / suites / router / verifier / judge / executor（含**失败复跑**、**设备回执**）/ orchestrator / **stats**（降级率 + 稳定性/对比 + **签名历史画像**）/ reporter / rpc / cli / zentao / scheduler / uia / vision / emulator / **explore** / **case_retry** / logsys / settings / winver / **dpi** / **desktop_contract** / conformance |
| 复用移植 | 完成（M2-3 已收编） | IssuePilot zentao client 的错误语义并入 `core/errors.py`；移植的 async `ZentaoClient`（bug 域）零引用已删除，`adapters/zentao_adapter.py` 同步实现为唯一禅道 HTTP 实现 |
| 设计文档 | 已迁移 | docs/design/ai-autotest-design-v1.md |

## 2. 里程碑计划（对齐设计 12 章）

### M1 — MVP 闭环（CLI 形态）✅ 已完成

| 任务 | 状态 | 交付物 / 验收方式 |
|---|---|---|
| M1-1 用例拉取 | ✅ | `CaseService.pull/sync`（幂等、stale/conflict 流转）；`test_case_parser.py` 全覆盖 |
| M1-2 LLM 解析 | ✅ | 版本化 prompt + 输出结构校验与 `uncertain` 降级；`test_llm_gateway.py` |
| M1-3 DSL 人审 | ✅ | `case export/import/accept/reject`（json/yaml）；草稿 v0 -> 生效 v1，重审再 +1 |
| M1-4 方案管理 | ✅ | `SuiteService` CRUD + 前置条件汇总 + 可执行性门禁 + 快照展开 |
| M1-5 UIA 执行 | ⏳ 待环境 | 代码路径与门禁就绪；**需被测软件 P0 适配后联调**（accessibleName/--test-mode/窗口标题） |
| M1-6 轨迹+报告 | ✅ | step_trace 全字段（target/verify_detail/source_text/device_state）+ 报告含原文对照/双截图/归因分布 |

M1 边界说明：M1 采用**本地优先**建任务（`test_task.zentao_task_id` 可空），
禅道建任务与回写按 M2-1 实现并可选用（`task create --remote`），失败自动降级 `sync_pending`。

### M2 — 禅道任务 + 模拟器联动

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| M2-1 禅道建任务 | ✅ | create_testtask + 回读校验 + sync_pending 补偿入口（`task sync-pending`） | 单测覆盖成功/失败/补偿三路径 |
| M2-2 模拟器接入 | ✅ | hello 握手/能力清单缓存/场景预加载/`device_state_json` 状态快照 | device 步骤已纳入 `test_executor.py` |
| M2-3 深度复用 ZentaoClient | ⏳ | 现有同步实现已覆盖 认证/重登录/分页/重试（功能等价）；收编 async ZentaoClient 仅为去重 | 认证失效自动恢复（已单测） |
| M2-4 增量同步 | ✅ | stale/conflict 流转 + 重解析（`case sync --parse`） | `test_case_parser.py` 全分支覆盖 |
| M2-5 env_snapshot | ✅ | 模拟器 protocol 版本 + **被测 exe 文件版本自动探测**（`infra/winver.py`，配置可覆盖） | 归因可比性字段：机器/平台/DPI 屏幅/驱动模式/DSL 版本集/被测版本/模拟器协议均已落库 |

### M3 — 知识闭环 + 视觉兜底

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| M3-1 元素库 | ✅ | 定位成功自动写 element 知识 + 超期 stale 降权（`prefer_vision`） | 机制单测覆盖；"耗时下降"需真实环境回归 |
| M3-2 失败库闭环 | ✅ | 签名精确/模糊匹配 -> 既定处置（blocked/retry）+ `kb pending/confirm/reject` 人审 | 命中率指标需真实数据积累 |
| M3-3 视觉 L1 | ✅ | 模板匹配 + **L2→L1 自动沉淀**（`visual` 定位先查已学模板，未被学才走 VLM） | 毕业/回落/裁剪尺寸/关闭开关均已单测 |
| M3-4 视觉 L2 | ✅ | VLM grounding + 置信度门禁 + region 坐标还原 + **DPI 感知/缩放比/坐标空间归一**（`infra/dpi.py`，含 `describe()` 入 env_snapshot） | 逻辑与 125/150/200% 换算已单测（`test_dpi.py` 40 例）；实机多缩放像素对齐需真机回归确认 |
| M3-5 路由学习 | ✅ | `router_stats` 连续失败重排（UIA 移至队尾兜底） | `test_router.py` 覆盖；每次驱动尝试均回写（含失败） |

### M4 — 桌面壳 + 批量回归

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| M4-1 Tauri 壳 | ✅ 代码就绪 | `src-tauri`（sidecar 监督 + RPC 桥 + 资源白名单读取）/ 打包脚本 / 图标；`cargo check` 0 error 0 warning | 真机联调与 NSIS 打包需在开发机跑一次（脚本已就绪） |
| M4-2 RPC 全表 | ✅ 引擎侧 | 32 个方法 + 事件推送（阻塞方法走线程池）+ `kb.setDisposition` | `test_rpc.py` 方法表门禁 + `test_desktop_contract.py` 跨语言门禁 |
| M4-3 页面 | ✅ 代码就绪 | Cases/Suites/Runs/RunDetail(三 Tab)/KBReview/DslReview/Tasks/Settings 八个页面；轨迹回放含原文对照+双截图+相似历史 | `tsc --noEmit && vite build` 通过；需实机联调 |
| M4-4 调度 | ✅ 内核 | `app/scheduler.py` 定时回归 + 夜间无人跑；CLI `schedule` | `test_scheduler.py` 覆盖 |
| M4-5 归因报告 | ✅ | 报告含 TOP 根因分布 + 处置建议 + **相似历史对照**（同族历史失败/命中次数/建议） | 相似历史已落 `case_exec.root_cause_json`，RunDetail 页可直接渲染 |

### M4+ 代码增强（2026-09-11 第七轮收口）

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| 驱动降级率与 `uia→vis` 统计 | ✅ | `step_trace.driver_chain_json` + `engine/stats.py` + 报告段 + RunDetail 面板/徽章 | `test_driver_stats.py`（口径/落库/汇总/报告） |
| `case_ctx` 人审维护 | ✅ | 知识服务 CRUD + CLI/RPC + KBReview Tab + 执行期场景预加载与归因注入 | `test_knowledge_service.py` + `test_rpc.py` + `test_reporter.py` |
| `env_compat` 通过率矩阵 | ✅ | run 收尾增量汇总 + `kb.envCompat` + 报告矩阵 + RunDetail 矩阵面板 | 同上（累加/多版本/无方案/上限） |

### M4+ 回归质量分析（2026-09-11 第八轮）

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| 用例稳定性（偶发 vs 必现） | ✅ | `stats.case_stability`（窗口/翻转/连续/四档）+ `run.detail.stability` + RunDetail 稳定性列与抖动卡片 + 报告段 | `test_regression_stats.py`（窗口截断/阻塞口径/样本不足/空集合语义） |
| 两次 run 对比 | ✅ | `stats.compare_runs` + RPC `run.compare` + CLI `run compare` + RunDetail「回归对比」面板 | 四桶与同因判定、参数校验、CLI 失败路径 |

### M4+ 失败复跑（flake 过滤，2026-09-11 第九轮）

| 任务 | 状态 | 交付物 | 验收 |
|---|---|---|---|
| 失败用例复跑 | ✅ | `case_exec.attempts/retry_json` + `step_trace.attempt` + 编排复跑循环（上限 3）+ `driver.case_retry` 三级覆盖 + CLI `--retry`/RPC `caseRetry` | `test_case_retry.py`（通过/耗尽/默认关/上限/轨迹分次/RPC/报告） |
| 消费端与稳定性联动 | ✅ | 报告复跑记录行 + RunDetail 复跑徽章与「第几次执行」切换 + `case_stability.retried`（复跑通过判抖动） | `test_regression_stats.py` 复跑口径 + 报告断言 |

### M5+（探索，非承诺）

piagent 自主探索测试（工具层封装驱动、模拟器接口包 MCP server、探索路径人审固化回 DSL）。
**工具层已就绪**（`testpilot/explore/`，含 `explore mcp/export/import` CLI 与 `test_explore.py` 门禁）；
agent 运行时由外部接入，探索产物一律先落 `draft`、经 `case accept` 人审后进主链路。

## 3. 工程规范（延续既有约定）

- **门禁**：所有驱动过 DriverConformanceTest（**已落地** `tests/conformance/`：manifest 完整性 / entry 可调用 / 能力与实现一致 / 协议方法齐备 / 不支持能力必须抛 ActionError）；pytest 全绿才可合入
- **注册表**：新驱动只注册 manifest，不改引擎；config_schema 前后端共用
- **DI**：构造期注入；禁止运行时属性注入
- **轨迹地基**：step_trace 字段只增不改语义；截图只存路径
- **LLM 纪律**：四类调用收敛 llm_gateway；prompt 版本化；token 计量入 run 统计
- **E-SafeNet**：关键文件单次完整写入；避免多进程并行写同一文件
- **构建**：产物统一顶层 dist/（PyInstaller onedir + Tauri bundle）；目标机免预装 Python/Node

## 4. 风险与对策（摘设计 13 章）

| 风险 | 对策 | 责任里程碑 |
|---|---|---|
| 高 DPI 坐标错位 | _ensure_dpi_aware + 125/150/200% 单测 | M3-4 |
| LLM 幻觉污染知识库 | 失败知识人审门禁 + confidence 衰减 | M3-2 |
| 禅道 task 断链 | zentao_task_id 唯一 + 回读校验 + 补偿 | M2-1 |
| 被测软件无 UIA 树 | P0 适配先行；过渡期 vision_only | M1 前置 |
| 模拟器协议漂移 | hello 能力清单 + manifest 版本入 env_snapshot | M2-2 |
| SQLite 轨迹膨胀 | 截图只存路径 + `run archive --before ...` 归档命令 | M4-4 |

## 5. 依赖与环境

- Python 3.11+（Windows）；dev 依赖 pytest/ruff；rpc 可选 websockets
- 运行时驱动依赖：pywinauto / mss / opencv / numpy / pywin32 / websocket-client
- 外部：禅道（token via ZENTAO_TOKEN）、内网 LLM/VLM（LLM_API_BASE/VLM_API_KEY）、设备模拟器 9300、被测 Qt 软件

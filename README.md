# TestPilot

AI 自动化测试平台：禅道测试用例 -> LLM 解析 DSL -> 双驱动（UIA + 视觉）自动执行设备模拟器联动 -> 轨迹/知识库闭环 -> 结果汇总回写禅道。

> 设计基线：[docs/design/ai-autotest-design-v1.md](docs/design/ai-autotest-design-v1.md)（v1.5）
> 开发计划：[docs/dev-plan-v1.md](docs/dev-plan-v1.md)
> **接手必读**：[docs/handoff.md](docs/handoff.md)（当前进度快照 + 环境约束 + 下一步命令）
> 自动化会话层增强：[docs/automation-runtime-ocu-2026-10-22.md](docs/automation-runtime-ocu-2026-10-22.md)（参考 open-computer-use：观察/查询/element_index/写后回读/MCP 会话安全）
> Agent/SVN 变更生成：[docs/agent-svn-change-analysis.md](docs/agent-svn-change-analysis.md)（Pi 只推理；所有工具通过主 RPC 执行）
> 禅道复审：[docs/review-zentao-2026-10-23.md](docs/review-zentao-2026-10-23.md)（真实增量水位 + 全量校准）
> 逐轮变更流水：[docs/dev-log.md](docs/dev-log.md) ｜ 打包/排障：[docs/build-and-verify.md](docs/build-and-verify.md)

## 核心能力

- **用例管理**：禅道用例按 ID 拉取入库、按产品水位真正增量同步（stale/conflict 流转）+ 全量校准
- **测试方案**：本地持久化用例集（顺序/前置模拟器场景/方案级驱动配置），一键建任务
- **真实 AI 联调**：默认vllm（空密钥），支持图片归因与视觉坐标转换；[修复与实测记录](docs/fix-ai-zentao-2026-10-16.md)
- **Agent 变更分析**：SVN revision/工作副本修改 → Pi Agent 级联影响分析 → 本地测试用例草稿 → 可选 DSL 解析 → 人审；Pi 无原生文件/Shell工具，所有工具均回到主 RPC 执行
- **双驱动执行**：UIA 对象驱动（pywinauto 主干）+ 视觉驱动（OpenCV 模板 + 内网 VLM 兜底），路由带学习闭环
- **Qt 专项适配**：Widgets / Quick-QML 的表格行列、页签、树、列表、复选框、弹窗、多窗口及可访问自绘控件；真实桌面端和驱动验收见 [Qt 专项记录](docs/qt-specialist-2026-10-16.md)
- **模拟器联动**：WebSocket 带外接口直控模拟核心（hello 握手 + 能力清单 + 指令回执 + 状态快照入轨迹）
- **轨迹地基**：step_trace 全字段落库（含 source_text 用例原文对照、device_state_json 设备快照、前后截图）
- **知识库**：失败签名精确/模糊匹配 -> 已知失败即时处置；新失败 LLM 归因 -> 人审入库；`case_ctx` 用例上下文（场景/已知坑/备注）人审维护并在执行期消费
- **驱动降级统计**：`step_trace` 记录每次驱动尝试链 -> run 汇总降级率与 `uia→vis` 路径（报告 + RunDetail）
- **环境通过率矩阵**：每次 run 汇总「被测版本 × 用例集」的 `env_compat` 矩阵，跨版本历史对照
- **回归质量分析**：用例稳定性（偶发/必现，窗口内翻转与连续状态）+ 两次 run 对比（新增失败/修复/持续失败），报告与 RunDetail 直接可用
- **失败用例复跑（flake 过滤）**：按策略在 run 内复跑失败用例（`driver.case_retry`，默认关闭），复跑通过判为疑似偶发；每次尝试的轨迹与签名分别留档
- **探索工具层（M5+，非承诺）**：驱动/模拟器包装为 agent 工具 + 模拟器 MCP server；探索路径只落 draft，人审后固化回 DSL
- **禅道边界**：仅任务级汇总回写（zentao_task_id 唯一约束 + 回读校验），执行明细只在本地

## 快速开始

```powershell
# 1. 环境（Windows 10/11 x64, Python 3.11+）
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,rpc]"

# 2. 初始化本地库（含幂等迁移）
testpilot db-init --pretty

# 3. 引擎自检（DB / 驱动 / 禅道 / 模拟器 / DPI）
testpilot doctor --pretty

# 4. 运行单测（含驱动一致性门禁 + 壳↔引擎跨语言契约门禁）
pytest -q

# 5. 闭环：拉取 -> 解析 -> 人审 -> 建方案 -> 建任务 -> 执行 -> 报告
testpilot case pull --ids 1024,1025 --parse
testpilot case list --status draft --pretty
testpilot case export TC-1024 --format yaml --out TC-1024.yaml   # 人工修订
testpilot case import --file TC-1024.yaml
testpilot case accept TC-1024
testpilot suite create --name "日常回归" --cases TC-1024,TC-1025
testpilot task create --name "回归-0910" --suite 1
testpilot task run 1 --pretty
testpilot task run 1 --retry 1 --pretty         # 失败用例复跑 1 次（flake 过滤）
testpilot run report 1
testpilot kb pending --pretty
testpilot kb confirm 1

# 6. 用例上下文与环境矩阵（人审维护 / 历史对照）
testpilot kb ctx-set --case TC-1024 --scene normal_sample_a --pitfalls "预热要等 3s" --notes "人审备注"
testpilot kb ctx-list --case TC-1024
testpilot kb env-matrix --suite 1

# 6.1 回归质量：这次比上次退步了吗（用例级四桶），以及各用例是偶发还是必现
testpilot run compare --base 12 --target 13 --pretty
testpilot run show 13 --pretty        # detail 内含 stability（每用例最近 10 次执行口径）

# 7. 定时回归（夜间无人值守）
testpilot schedule --task 1 --every-min 120          # 常驻
testpilot schedule --task 1 --once                   # 配合系统计划任务

# 8. M5+ 探索（非承诺）：会话固化回 DSL / 模拟器 MCP server
testpilot explore export --file session.json --format yaml --out TC-9001.yaml
testpilot explore import --file session.json --case-id TC-9001   # 落 draft，随 case accept 人审
testpilot explore mcp                                            # stdio，供外部 agent 接入
```

驱动执行（`testpilot task run`）依赖 pywinauto / opencv / mss / 模拟器与被测软件环境，见设计文档附录 A 适配要求。

### 当前验证状态

```powershell
cd D:\MyCode\TestPilot
.venv\Scripts\python.exe tools\verify.py      # 一条命令：ruff + pytest + 报告(logs/verify-report.md)
```

| 检查 | 命令 | 结果 |
|---|---|---|
| 单测 | `tools\verify.py` | **799 passed / 0 failed**（含驱动、Qt 适配、真实网络与跨语言契约回归） |
| 静态检查 | 同上（脚本内含 ruff stdin 通道） | **0 problems** |
| 前端 | `cd testpilot/app/desktop; npm run build` | **0 error**（tsc 类型检查 + vite 产物） |
| Rust 壳 | `cd testpilot/app/desktop/src-tauri; cargo check` | **0 error / 0 warning** |

> 直接跑 `pytest` / `ruff check <path>` 会踩两个本机环境坑（垫片导致 tmp 清理失败、ruff 直读被 DLP 拦），
> 因此统一走 `tools/verify.py`；细节见 [docs/build-and-verify.md](docs/build-and-verify.md)。

## 桌面壳（M4）

```powershell
# 开发态：壳 + 引擎一起起
cd testpilot/app/desktop; npm install
npm run shell:dev

# 打包前静态自检（不派生子进程，8 组 / 74 项：配置、图标、权限、清单一致性、脚本语法与编码）
cd D:\MyCode\TestPilot
powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1

# 一体化打包（sidecar -> 前端 -> NSIS 安装包 -> 顶层 dist/bundle；目标机免装 Python/Node）
powershell -ExecutionPolicy Bypass -File scripts\build-shell.ps1

# 只重打壳 / 换包格式 / 快速出单文件 sidecar / 清理产物
... build-shell.ps1 -SkipSidecar
... build-shell.ps1 -Bundles msi
... build-sidecar.ps1 -Onefile
... scripts\clean.ps1 -WhatIf
```

架构边界、方法表、打包形态与已知边界见 [testpilot/app/desktop/README.md](testpilot/app/desktop/README.md)；
验证/打包/排障 runbook 见 [docs/build-and-verify.md](docs/build-and-verify.md)。

## 目录结构

```
testpilot/
├── core/          # 领域核心：dsl / contracts(Protocol 端口 + DriverManifest) / registry / errors / events
├── engine/        # 执行域：executor / router / verifier / judge / orchestrator(含失败复跑) / stats(驱动降级/稳定性/对比)
├── drivers/       # 驱动适配：uia_driver / vision_driver / emulator_adapter（GUI 依赖惰性导入）
├── adapters/      # 外部适配：zentao_adapter / svn_adapter / llm_gateway / prompts
├── agent/         # AgentPort 默认实现：Pi RPC runtime + RPC-only tools + SVN 变更生成服务
├── knowledge/     # 知识域：signature / service（元素库/失败库/case_ctx 人审/env_compat 矩阵/路由统计）
├── storage/       # 持久化：db(WAL+DDL+迁移) / recorder(轨迹+驱动尝试链+每次尝试摘要) / repos / shotstore
├── infra/         # 横切：abort(协作取消) / logsys(结构化日志) / events(事件总线) / dpi(DPI 坐标归一) / winver
├── report/        # Markdown 报告（步骤轨迹/原文对照/双截图/归因分布/相似历史/降级统计/env 矩阵/稳定性）
├── explore/       # M5+ 探索（非承诺）：驱动工具层 / 会话固化回 DSL / 模拟器 MCP server
├── case_parser.py # 用例应用服务：拉取/增量同步/LLM 解析/人审流转（M1-1~M1-3）
├── suites.py      # 测试方案应用服务：CRUD/前置汇总/门禁/快照展开（M1-4）
├── app/           # CLI / scheduler（定时回归）
│   └── desktop/   # M4 桌面壳：src-tauri(Rust) + src(React 18 + TS)
├── config/        # default.yaml + settings(${ENV} 展开)
└── rpc.py         # JSON-RPC 2.0 over WebSocket(壳<->引擎，43 个方法)
packaging/         # PyInstaller sidecar 打包（run_sidecar.py + sidecar.spec + hiddenimports.txt）
scripts/           # build-sidecar.ps1 / build-shell.ps1 / clean.ps1
tools/             # verify.py（ruff + pytest 一条命令跑完，含两个环境坑的处理）
tests/
├── conformance/   # DriverConformanceTest（驱动一致性门禁）
├── test_desktop_contract.py  # 壳↔引擎跨语言契约门禁（方法表/事件/命令/打包形态/数据根）
└── ...            # 含 DPI 换算、Qt 适配与 pywinauto/mss/websocket 回归
docs/              # design(设计基线) + dev-plan(开发计划) + dev-log(实时开发日志) + build-and-verify(runbook)
```


## 工程约定

- 注册表取代条件分支创建驱动；驱动 manifest 自描述 + **DriverConformanceTest 门禁已落地**（`tests/conformance/`，过门禁才可注册）
- 依赖构造期注入，禁止属性后置注入；core/ 零平台依赖（只定义 Protocol）
- GUI 依赖（pywinauto/opencv/mss/win32）一律**方法内惰性导入** —— manifest 与注册表不需要 GUI 运行时
- **执行主干确定性**：正常回归不依赖 LLM/Agent；单次结构化推理走 LlmGateway，多轮代码库/影响分析统一走 AgentPort（默认 Pi）
- **Agent 工具必须经主 RPC**：Pi 禁用 built-in/MCP/skills；文件/SVN/用例读取由短期 workspace-scoped capability token 回调 `agent.*` 主进程工具
- LLM 输出必须过结构校验：非法步骤降级为 `uncertain` 交人审，不静默丢弃
- 结构化 JSON 日志写 **stderr**（stdout 只留命令结果，保证 CLI 输出可解析）
- **DPI 坐标纪律**：UIA 矩形 = 物理像素；`infra/dpi.py` 负责判定截图坐标空间并做换算，驱动不得自行硬编码 96 DPI
- **跨语言契约门禁**：前端方法表 / 事件名 / 壳命令 / 打包形态与引擎必须一致，由 `tests/test_desktop_contract.py` 守门
- 构建产物统一顶层 `dist/`；关键文件单次完整写入（E-SafeNet 约束）
- 里程碑进度与逐项变更见 [docs/dev-log.md](docs/dev-log.md)

## 复用资产

| 资产 | 来源 |
|---|---|
| 禅道 REST client（认证/重登录/分页） | IssuePilot `plugins/zentao/client.py` -> `testpilot/adapters/zentao_adapter.py`（同步门面，M2-3 收编后唯一实现） |
| 注册表/manifest/一致性测试模式 | IssuePilot `core/` 工程约定 |
| sidecar 打包链（PyInstaller onedir + 资源目录投放） | ZentaoDsh 桌面端经验（M4）；**注：Tauri 的 externalBin 装不下 onedir，已改走 bundle.resources** |
| UI 交互规范（紧凑徽章/Tab 分栏/顶部导航） | ZentaoDsh 桌面端约定（React 重实现） |

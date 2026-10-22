# TestPilot 修复与 UI 重构交付记录

日期：2026-10-15。对应审查：[review-2026-10-15.md](review-2026-10-15.md)。本记录保留原审查结论，不以修改设计文档代替实现或验收。

## 交付内容

已针对审查发现的 F01–F19 实施代码修复，并处理 D1–D5 的主要设计缺陷。前端改为六个主入口：工作台、用例与审核、测试方案、测试执行、问题与知识、设置。工作台提供待办与最近执行；方案支持一键创建任务并开始测试；审核默认使用结构化步骤编辑器，高级 JSON 编辑保留；执行中的全局停止按钮始终可达；失败根因、建议和证据可人工修订。

## 问题与修复对应

| 编号 | 实现结果 | 主要入口 |
|---|---|---|
| F01 | 图片请求使用独立 VLM 地址、模型、密钥和超时配置 | adapters/llm_gateway.py、engine/orchestrator.py |
| F02 | 审核和执行共用语义校验，拒绝缺失判据的断言、未知动作和重复步骤 ID | core/dsl.py、case_parser.py、engine/executor.py |
| F03 | app/device 分支执行配置的验证，不再直接忽略 | engine/executor.py |
| F04 | 无法判定的验证结果转为阻塞及待审，不能计作通过 | engine/executor.py、engine/verifier.py |
| F05 | 任务页和全局运行条提供可用的停止入口 | App.tsx、TasksPage.tsx |
| F06 | WebSocket 使用会话 Bearer 令牌，限制 Origin 和回环绑定；壳向内置引擎传递令牌 | rpc.py、src-tauri/src/rpc.rs、engine.rs |
| F07 | RPC 查询/编辑与运行线程使用独立 SQLite 连接，阻止重复排队启动 | rpc.py |
| F08 | 开始运行时冻结全部用例 DSL，并持久化完整快照与版本集合 | engine/orchestrator.py、storage/recorder.py |
| F09 | 保存远端建任务参数；缺失远端 ID 时支持补偿创建；读回失败保留已取得的 ID | engine/orchestrator.py |
| F10 | 失败、阻塞、中止或证据不完整的运行返回非零 CLI 退出码 | app/cli.py |
| F11 | 存储步骤最终状态，前端优先显示该状态 | storage/db.py、storage/recorder.py、RunDetailPage.tsx |
| F12 | 截图差异绑定用例尝试及具体轨迹 ID；步骤重试截图另存，不覆盖原证据 | rpc.py、engine/executor.py、RunDetailPage.tsx |
| F13 | 非对象及不完整 LLM 步骤保留为待修订项，避免静默丢弃 | adapters/llm_gateway.py |
| F14 | 保存当前编辑内容并审核，使用 revision 拒绝过期覆盖 | rpc.py、case_parser.py、DslReviewPage.tsx |
| F15 | 文本和状态断言在截止时间内轮询，并响应取消 | engine/verifier.py |
| F16 | 壳 RPC 提供健康、普通和长任务超时，清理 pending | src-tauri/src/rpc.rs |
| F17 | 读取健康状态后释放互斥锁，再更新状态 | src-tauri/src/engine.rs |
| F18 | 新运行记录执行者 PID/session；启动时仅收尾确定失去执行者的运行 | storage/db.py、storage/recorder.py、app/cli.py |
| F19 | 历史默认基线限定为同任务且早于当前运行的已结束记录 | rpc.py、RunDetailPage.tsx |
| D1 | 每条用例及复跑前恢复场景；前置条件失败阻塞用例 | engine/orchestrator.py、engine/executor.py |
| D2 | 环境矩阵按实验条件指纹隔离；稳定性限定 DSL、被测版本和执行配置 | knowledge/service.py、engine/stats.py |
| D3 | 失败精确签名加入设备命令、场景及断言路径/期望等上下文 | knowledge/signature.py |
| D4 | 低置信规则允许模型进一步分析；人审可修订根因、建议、证据并留历史 | engine/judge.py、knowledge/service.py、KBReviewPage.tsx |
| D5 | 轨迹/截图错误计入证据完整性；配置临时文件原子替换；归档排除运行中记录并带资源/hash 清单 | storage/recorder.py、storage/shotstore.py、config/service.py、storage/repos.py |
| D6 | 增加关键缺陷回归测试、真实 WebSocket 握手测试、浏览器流程验证及打包程序启动检查；完整实机验收仍待外部环境 | tests/test_review_fixes.py、logs/verify-report.md |

附加修复：前端异步加载丢弃过期结果；修正全局快捷键；打包联调发现 TESTPILOT_RPC_PORT 仅由壳读取，现已由引擎配置同步读取并校验范围，加入回归测试。

## 验证结果

- 完整 Python 检查：tools/verify.py，707 项测试全部通过；91 个文件静态检查，0 个问题文件。详细记录见 ../logs/verify-report.md。
- Rust 壳：cargo test --offline，7 项通过，0 失败。
- 前端：TypeScript 类型检查及 Vite 生产构建通过。
- sidecar 打包：PyInstaller 成功；check_bundle.py 检查 26 项必要模块和 1 项配置资源全部通过。Pillow 是可选依赖，本包采用 mss 截图后端。
- 浏览器开发预览：验证首页 → 方案 → 开始测试、结构化步骤修改 → 保存并审核、运行条停止 → 已中止；控制台未发现错误。浏览器使用明确标注的开发样例数据，不能作为真实业务执行证据。
- 打包程序：在独立 TESTPILOT_HOME 及端口 19322 启动实际 release 桌面壳，内置 sidecar 启动成功；会话鉴权下 health、run.list、task.abort 均成功。隔离目录为 .cache/acceptance-final-20261015，shell.log 确认引擎 running、restarts=0。检查结束只关闭本次启动的进程树。

## 安装包与使用

安装包：dist/bundle/review-20261015/TestPilot_0.1.0_x64-setup.exe（57,977,329 字节，约 55.3 MiB）。该目录用于本次交付，旧发行物不作为本次结果。

SHA-256：`F8FCD6D9E35EA1076FE92A8B0E5D815613530347CC6154EAAB1C252B38289959`。最终 NSIS 构建成功，并已复制到交付目录。

新版数据库启动时自动迁移新增字段。外部 RPC 客户端需要使用 TESTPILOT_RPC_TOKEN 对应的 Authorization: Bearer 请求头；桌面壳自动完成会话令牌传递。

## 尚需外部环境验收的边界

1. 真实禅道、LLM/VLM、模拟器和目标软件的完整执行/回写闭环，以及网络故障恢复，需要在实际服务和软件上验收。现有测试包含假依赖，不能推断真实环境全部可用。
2. 干净机器安装、无 Python/Node 环境及 125%/150%/200% DPI 未完成实机验收；打包程序检查在开发机进行。
3. 旧运行记录没有执行者或完整 DSL 快照，不能可靠补造历史证据；为避免误伤其他实例，不批量关闭无法确定归属的旧 running 记录。
4. 远端 POST 已成功但连接在返回 ID 前中断，仍存在服务端创建结果不明确的窗口。彻底保证创建幂等需要禅道服务支持幂等键或可核验的唯一标识。
5. 场景恢复依赖模拟器 load_scene 的实际语义，不能保证所有外部硬件副作用均可回滚。归档目前包含资源和校验清单，尚未提供独立的一键恢复导入向导。

因此，本次交付是已实现并验证的修复和 UI 重构版本；不将其描述为所有外部环境均已验收。

# AI 与禅道链路修复及真实模型验收

日期：2026-10-16。本记录对应 [专项审查](review-ai-zentao-2026-10-16.md) 中的问题。

## 默认模型

对话与视觉模型均改为 pumen-vllm，地址 http://192.168.1.77:8989/v1，API Key 默认空，不发送 Authorization 头。模型列表实测返回 max_model_len=262144；本轮没有进行满 256K 上下文压力测试。

真实推理时发现默认思考会耗尽输出预算，现对该 vLLM 模型默认发送 chat_template_kwargs.enable_thinking=false。若响应 finish_reason=length，会明确报告结果不完整。视觉定位按原生 normalized_1000 / xyxy 接收，再根据实际 PNG 尺寸统一转换为像素 x/y/宽/高；设置页可切换坐标制式和边界格式。

## 修复对照

| 原问题 | 修复结果 |
|---|---|
| Z1 列表缺步骤 | 缺少 steps 时获取详情；详情缺字段或 ID 不匹配直接失败，不用残缺摘要覆盖原文。 |
| Z2 前置条件丢失 | 保留远端 precondition，分行送入 AI，并合并到草稿；原文前置条件始终保留。 |
| Z3 新增用例漏解析 | 同步勾选解析后，新增且无步骤的用例同样解析；已驳回用例保持不自动解析。 |
| Z4 假回写成功 | 检查业务失败信封；更新后回读核对实际状态和汇总，核对通过才清除 pending。汇总写入支持的 desc，保留原有描述，重试幂等。 |
| Z5 丢失创建 ID | 所有已创建后的回读失败均携带远端 ID；本地先保存创建意图。网络结果不确定时停止自动重建，通过“核对远端 ID”恢复。 |
| Z6 错误用例编号 | 编排器使用数据库明确的 zentao_case_id；本地专属用例不再从名称抽数字。回读实际 testcases 关联范围，不能用请求字段回显冒充关联成功。 |
| A1 非法置信度放行 | NaN、Infinity、范围外数字、布尔置信度均拒绝；视觉断言返回 unknown。pass 严格校验布尔值。 |
| A2 归因未发送图片 | 读取证据根目录内的真实 PNG，校验尺寸/路径/体积，最多附带两张图片，通过视觉模型归因；图片不可用时明确标记仅文本分析。 |
| A3 能力清单残留 | 每次解析前清空旧清单，再获取当前清单；获取失败时不会沿用旧模拟器能力。 |
| A4 长任务阻塞 | 用例拉取/同步/解析支持 job.start/status/cancel，在独立线程及独立数据库连接中执行。页面可查看、取消或切页后重新查看；健康检查保持可用。模型重试共享总预算，支持取消；视觉调用也受步骤/断言预算约束。 |
| A5 来源或期望遗漏 | 对照原始步骤检查 source_text 和有期望步骤的验证覆盖；缺失时置 uncertain 并提示人审。后台解析使用版本检查和短写事务，避免覆盖解析期间的人工作业。 |

## 禅道版本边界

配置显式标记 REST v1。v2 在配置/适配器入口拒绝，避免误用 v1 请求；本轮没有宣称新增 v2 支持。

远端创建需要真实 build，并传递起止日期；支持复用已在禅道关联好用例的测试单 ID。创建后的实际关联范围必须与本地任务一致；若实例不支持创建时 cases 字段，必须先在禅道关联用例，再执行“核对远端 ID”或补偿，程序不会把“拿到 id”视为全部成功。

本轮禅道使用本地严格模拟服务，校验必填字段、实际用例范围、业务失败和汇总持久化；真实公司禅道尚需提供测试实例和产品做兼容验收。模拟通过不代表所有禅道版本兼容。

## 真实 pumen-vllm 验收

使用默认超时和空密钥调用真实接口，6 项检查全部通过：

- 中文自然语言用例解析为合法 DSL，覆盖原始步骤。
- 期望值 SAMPLE-A17、Completed、42.5 保留。
- 图片显示 99.9 时，正确期望通过，错误期望 42.5 不通过。
- 800×480 测试图片中的 Export 按钮定位为像素 [500,340,220,80]。
- 失败归因实际接收图片，引用图中的 99.9 等证据，保持 needs_review=true。

本轮 5 次真实模型调用，共 4870 tokens，网关累计 17190ms，0 调用错误。图片是专门生成的合成样例。

证据：dist/simulation/evidence-ai-live-20261016/acceptance.json，after.png。
复现：tools/accept_ai_provider.py --out <新目录>。

## 真实模型 → Qt → 禅道链路

禅道模拟列表省略 steps；本工程补取详情后，通过后台任务调用真实 pumen-vllm。自然语言用例生成 7 个 DSL 步骤，经逐条核对后原样审核通过，没有手工替换模型步骤。

本工程的鉴权 RPC → TaskOrchestrator → Executor → UIADriver 操作实际 C++ Qt 程序：附加窗口、输入 AI-REAL-17、选择 Precision、启动测量、等待 Completed、校验 42.5 mg/L、导出报告。

结果：7 步通过，6 次 UIA 交互成功、0 失败，实际导出 JSON 经独立读取确认 sample_id=AI-REAL-17、mode=Precision、value=42.5；远端测试单 id=1001，实际关联用例 91901，状态 done，汇总回读正确。后台模型解析期间健康检查最大耗时 0.0043 秒。正常执行阶段按确定性 DSL 运行，LLM 调用数为 0；模型调用发生在前面的解析阶段。

证据：dist/simulation/evidence-ai-qt-chain-20261016/parse-acceptance.json、chain-acceptance.json、draft.json、reports/1/report.md、exports/。

## 回归与制品

全量验证：799 项 Python 测试通过，0 失败，耗时 106.3 秒；101 个文件静态检查 0 问题。TypeScript、Vite、Rust release 和 NSIS 安装包构建通过。详细结果见 logs/verify-report.md。新增 tests/test_ai_zentao_hardening.py 覆盖置信度、布尔值、真实图片内容、坐标转换、来源覆盖、首次同步、业务失败、错误关联、创建不确定状态、模型取消、总超时、后台任务和并发人审保护。

原有网络/Qt 联调继续覆盖故障注入、认证刷新、回写补偿、创建恢复、中止保护和连续两次 Qt GUI 操作。多模态模拟端点的请求体上限调为 24 MiB，兼容双截图，不再因原先 1 MiB 限制回落仅文本归因。

## 打包桌面实测及交付

最终程序 dist/desktop/ai-zentao-20261016/testpilot-shell.exe 使用同目录 binaries/sidecar/testpilot-sidecar.exe。已从其真实“重新解析”按钮调用指定模型，生成 7 步 DSL，页面显示“用例后台任务：已完成”，前置条件和逐步期望均可审阅。准备脚本仅拉取原文，不发起 AI 解析；点击证据和数据库结果分开保存。

桌面证据：dist/simulation/evidence-desktop-ai-20261016/desktop-acceptance.json、ui-action.json、ai-parsing.jpg、ai-result.jpg。没有将 RPC 会话凭证或测试配置复制进证据目录。

安装包：[TestPilot_0.1.0_x64-setup.exe](/D:/MyCode/TestPilot/dist/bundle/ai-zentao-20261016/TestPilot_0.1.0_x64-setup.exe)，63,500,582 字节；SHA256 为 3fb7e613eabd4d65d1db6e1fea8fab72ee64d00dfd4c8fd19e3032078f350c7d。制品清单见同目录 artifacts.json。

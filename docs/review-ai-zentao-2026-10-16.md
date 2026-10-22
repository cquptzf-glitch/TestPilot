# AI 分析与禅道链路专项审查

日期：2026-10-16。审查对象为当前工作区源码，不是对生产环境的验收。

后续修复和真实模型验证见 [修复验收记录](fix-ai-zentao-2026-10-16.md)。以下保留审查当时的问题和复现结果。

## 结论

不能判定两条链路已完全正确。既有结构已具备：禅道拉取 → AI 解析草稿 → 人工审核 → 本地执行 → 失败归因 → 知识审核 → 任务级回写/补偿。但已隔离复现 11 项问题或功能缺口（5 项 P1、6 项 P2），另有版本兼容验收缺口。

本次复跑 196 项相关现有测试，0 失败、0 错误；另外运行 11 个审查探针，均复现下文所述现象。探针显示 CONFIRMED 代表问题被复现，不代表功能验收通过。没有向真实禅道写数据，也没有调用真实模型；业务源码和安装包未在本次审查中修改。

## P1：优先修复

### Z1 批量同步未获取详情，步骤可能为空

位置：[zentao_adapter.py:150](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:150)、[zentao_adapter.py:218](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:218)。

fetch_cases 把产品列表的每行直接交给 _to_raw_case，没有逐条补取 testcase 详情。列表只含 stepNumber、不含 steps 时，结果是 steps_text=""，后续 parse 报“缺少禅道原文步骤”。远端更新时还可能用缺步骤的列表摘要替换完整原文快照。

复现：列表返回 id=501、stepNumber=1；详情端点提供真实步骤。实际仅请求 products/1/testcases 与 modules，没有请求 testcases/501，steps_text 为空。

建议：列表仅用于枚举和变更识别，对新增/变更用例补取详情；详情失败不能用不完整摘要覆盖原文。官方维护的 SDK 将列表定义为 stepNumber，详情定义为 steps，支持此契约区别：[官方源码](https://raw.githubusercontent.com/easysoft/go-zentao/master/zentao/testcases.go)。

### Z2 禅道前置条件在进入 AI 前丢失

位置：[zentao_adapter.py:219](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:219)、[case_parser.py:216](/D:/MyCode/TestPilot/testpilot/case_parser.py:216)。

_to_raw_case 没保留远端 precondition；新建本地记录也没有把它写入 preconditions。_parse_payload 读取的是本地 preconditions，首次解析因此通常是空数组。模型无从知道原用例要求的场景、登录条件或数据准备。

复现：详情包含 precondition="LOAD_REQUIRED_SCENE"，实际 AI 输入 preconditions=[]，整个 payload 都没有该条件。

建议：保留原始前置条件，并明确“远端原文前置条件”和“解析后的执行条件”的来源，确保同步、解析、审核全程可对照。

### Z4 禅道业务失败被记为同步成功

位置：[zentao_adapter.py:92](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:92)、[zentao_adapter.py:272](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:272)、[orchestrator.py:425](/D:/MyCode/TestPilot/testpilot/engine/orchestrator.py:425)。

HTTP 层只检查状态码/JSON 可解析性；update_task_status 不检查业务返回体，也不回读实际状态。遇到 HTTP 200 + status=fail 的网关/实例返回时，回写不抛错，上层把 sync_pending 改为 synced，补偿入口随之消失。

复现：本地 httpx.MockTransport 返回 200 / {"status":"fail","message":"permission denied"}，实际 _writeback 返回 synced，数据库也保存 synced。

建议：按选定禅道版本校验业务成功/失败信封；回读确认状态及汇总确已保存，再清除待同步标记。该问题的触发前提是实例使用 HTTP 200 表达业务失败，不代表所有版本都如此返回。

### Z6 从本地用例名抽数字，可能发出错误禅道关联号

位置：[zentao_adapter.py:285](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:285)、[orchestrator.py:169](/D:/MyCode/TestPilot/testpilot/engine/orchestrator.py:169)。

_zentao_ids 对任意 case_id 删除全部非数字字符。编排器未按数据库 zentao_case_id 建立关联，传的是本地 case_ids；补偿保存的 remote_spec 也是同样口径。

复现：QT-DESKTOP-001 → 1，LOCAL-2026-17 → 202617。这些本地用例并没有对应的远端关联依据。即使人工导入时明确提供了另一个 zentao_case_id，当前建任务路径也没有读取它。

建议：使用数据库已确认的 zentao_case_id；本地专属用例不得猜测远端 ID。若混合创建远端任务，应明确提示未映射用例并阻止误关联。

### A1 非有限置信度能绕过视觉断言门禁

位置：[llm_gateway.py:449](/D:/MyCode/TestPilot/testpilot/adapters/llm_gateway.py:449)、[verifier.py:150](/D:/MyCode/TestPilot/testpilot/engine/verifier.py:150)。

_as_float 接受 NaN/Infinity 及范围外数值；Verifier 只用 conf < min_conf。NaN 的比较结果为 False，导致失效门禁。grounding 路径也使用同类比较，应一起处理。

复现：真实 LlmGateway 收到合法 JSON {"pass":true,"confidence":"NaN"}，实际 Verifier 返回 pass，confidence=nan。

建议：结构化输出严格校验 confidence 为有限的 0..1 数字；pass 使用严格布尔类型；非法输出降级 unknown，不得放行。

## P2：完整性与稳定性

| ID | 问题及复现 | 影响与建议 | 代码 |
|---|---|---|---|
| Z3 | 全新数据库执行 sync(parse=True)，新增 2 条、AI 调用 0 次、parse={}。筛选仅包含 stale/conflict，遗漏 new。 | UI“同时解析 DSL”勾选后首次同步仍没有草稿步骤。将新增且未解析用例纳入，保留 rejected 不自动解析的规则。 | [case_parser.py:70](/D:/MyCode/TestPilot/testpilot/case_parser.py:70) |
| Z5 | 创建返回 id=44；回读正常返回但 id 不匹配时，异常不携带 task_id。 | 上层无法保存已创建 ID，补偿可能再次 POST，产生重复任务。所有“已创建、确认失败”分支均保留 ID；对创建超时后的不确定结果也需防重复策略。 | [zentao_adapter.py:253](/D:/MyCode/TestPilot/testpilot/adapters/zentao_adapter.py:253) |
| A2 | Judge 把 screenshots 的本地路径放进文本 JSON；LlmGateway.judge_failure 没有传 image_b64，探针记录 image_attached=false。 | 目前失败归因实际只看文本。设计 11 章要求“文本模型+截图”，尚未兑现；需加载真实证据图片并调用支持图片的模型，或明确告知仅文本分析。 | [judge.py:153](/D:/MyCode/TestPilot/testpilot/engine/judge.py:153)、[llm_gateway.py:90](/D:/MyCode/TestPilot/testpilot/adapters/llm_gateway.py:90) |
| A3 | 曾有 old_command/old_scene，下一次能力清单获取异常后仍保留旧清单。 | Prompt 和校验可能继续使用上一个模拟器/版本的指令。每次解析前清理或按连接/版本重新绑定；不可用时明确禁止 device 生成。执行器还有运行时校验，这不是直接绕过执行白名单。 | [case_parser.py:226](/D:/MyCode/TestPilot/testpilot/case_parser.py:226) |
| A4 | 每次模型请求 timeout=60 秒，共重试 3 次，另有 0.5/1 秒退避；桌面非 task.run RPC 超时为 180 秒。 | 单次解析可能超过壳期限；批量解析更易超过，且占用唯一 rpc-io 线程，health 也排队。需要总截止时间/协作取消，以及批量后台作业、进度和最终结果查询。探针验证参数传递，未真实等待 180 秒。 | [llm_gateway.py:189](/D:/MyCode/TestPilot/testpilot/adapters/llm_gateway.py:189)、[rpc.rs:160](/D:/MyCode/TestPilot/testpilot/app/desktop/src-tauri/src/rpc.rs:160)、[rpc.py:62](/D:/MyCode/TestPilot/testpilot/rpc.py:62) |
| A5 | 原文有明确期望结果，但模型遗漏 source_text 且输出 verify=none，归一化结果 uncertain_count=0。 | 结构校验不能保证“测试了原用例要求”。人审仍是有效门禁，但无自动缺失提醒。应验证来源映射、原始步骤覆盖、期望覆盖，缺失时强制提示待核。 | [llm_gateway.py:99](/D:/MyCode/TestPilot/testpilot/adapters/llm_gateway.py:99)、[core/dsl.py:87](/D:/MyCode/TestPilot/testpilot/core/dsl.py:87) |

## 禅道版本和字段契约尚未完成验收

当前路径固定为 /api.php/v1；创建发 product/name/build/cases，默认 build=trunk；状态更新只发 status/comment。RPC 的 task.create 也没有接收 build、begin/end 等字段的路径。

官方当前 v2 创建接口要求 productID、数值 build、begin/end 等，更新接口也有必填字段，与本工程 v1 形态不同。因此不能把一个新禅道地址填进配置就视为已适配 v2。[官方创建接口](https://www.zentao.net/book/api/post-testtasks-2234.html?fullScreen=cw1yidVe)、[官方更新接口](https://www.zentao.net/book/api/put-testtasks-testtaskid-2235.html?fullScreen=rb0qL4jT)。

官方维护的 v1 Go SDK 任务结构包含构建、负责人、起止日期、描述等字段，没有本工程所依赖的 cases/comment 字段：[官方 SDK](https://raw.githubusercontent.com/easysoft/go-zentao/master/zentao/testtasks.go)。这证明需要确认目标版本和实例契约；单凭文档/SDK字段缺失，还不能断言每个 v1 实例必然拒绝这些字段。

必须验证：具体版本是否支持创建/修改测试单、用例范围关联是否需要单独操作、build 的真实含义、汇总保存字段和更新接口的必填项。创建后只比对 id，并不能证明用例关联和汇总正确。

## 现有测试能证明什么

已覆盖并复跑通过：解析 JSON/动作合法性、人审后 active 的执行门禁、非法类别归为 unknown、LLM 归因待审核入库、认证刷新、分页、网络重试、中止运行不能补偿为 done 等。

模拟服务的模型接口按固定 case ID 返回预置 DSL；归因返回固定 unknown，且注明未做真实模型分析。禅道模拟服务创建直接保存请求体，修改时直接 task.update(body)，没有真实版本的必填字段/关联语义验证。因此原先模拟联调通过只能证明内部链路可通，不能证明模型推理质量或真实禅道兼容。

证据：[simulation/services.py:262](/D:/MyCode/TestPilot/testpilot/simulation/services.py:262)、[simulation/services.py:300](/D:/MyCode/TestPilot/testpilot/simulation/services.py:300)。

## 复现与后续验证

- 基线：.cache/ai-zentao-review-baseline.xml（196 passed，14.85 秒）。
- 探针：[audit_ai_zentao_review.py](/D:/MyCode/TestPilot/.cache/audit_ai_zentao_review.py)。
- 机器可读结果：[ai-zentao-review-probes.json](/D:/MyCode/TestPilot/.cache/ai-zentao-review-probes.json)。

执行：

```powershell
.venv\Scripts\python.exe .cache/audit_ai_zentao_review.py
```

建议先修 Z1/Z2/Z4/Z6/A1，再处理幂等创建、真实图片归因、能力清单生命周期和长耗时任务。随后在专用禅道测试产品上完成“拉取详情 → 实际模型解析 → 人审 → 真实 Qt 执行 → AI 分析 → 禅道回读核对”的端到端验收，并记录模型版本、提示词版本、真实返回和预期差异。

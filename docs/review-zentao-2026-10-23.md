# 禅道用例对接复审与增量同步优化

> 日期：2026-10-23  
> 对象：TestPilot 当前 REST v1 禅道链路。  
> 结论：此前 2026-10-16 审查发现的 Z1–Z6 关键问题已经修复；本轮又发现并修复“名义增量、实际可能全量补详情”和“0 变更无法准确记录最后同步时间”两个问题。

## 1. 当前链路

```text
禅道产品用例列表
   │
   ├─ 增量水位过滤
   │
   └─ 新增/变更候选
         │
         └─ 单用例详情（必须包含 steps）
               │
               ▼
          dsl_case.raw_json
               │
        LLM 解析为 DSL 草稿
               │
            人工审核
               ▼
             active
```

任务侧：

```text
本地任务
  └─ 可选创建禅道测试单
       ├─ 明确 build/begin/end
       ├─ 只使用数据库中真实 zentao_case_id
       └─ 创建后回读实际 testcases 关联范围

Run 完成
  └─ 只回写任务级汇总
       ├─ 状态
       ├─ 通过/失败/阻塞统计
       ├─ 幂等 summary marker
       └─ PUT 后再次 GET 校验
```

执行明细仍以 TestPilot 本地轨迹为权威，不把每一步轨迹灌进禅道。

## 2. 2026-10-16 问题复核

| 原问题 | 当前状态 |
|---|---|
| Z1 列表缺 steps 导致空原文 | 已修：列表行无 steps 时强制补取 testcase 详情 |
| Z2 precondition 丢失 | 已修：远端 precondition 进入 raw 与 AI payload |
| Z3 首次 sync --parse 不解析 new | 已修：无 DSL 的新用例纳入解析 |
| Z4 HTTP 200 业务失败被当成功 | 已修：统一 business envelope 校验 + 回读 |
| Z5 创建后确认失败丢 task id | 已修：保留 task_id / creation_uncertain，阻止自动重复 POST |
| Z6 从本地 case_id 猜禅道 ID | 已修：只使用数据库真实 zentao_case_id；本地用例不猜 ID |

此外已经具备：

- Token / account-password 登录；
- 401/403 自动重登后重放；
- 分页；
- module 分组读取；
- stale/conflict 三版人审语义；
- 后台 pull/sync/parse job；
- 可取消；
- 任务创建关联范围回读；
- writeback 补偿；
- REST v1/v2 明确隔离。

## 3. 本轮新发现 Z7：增量同步没有真正使用 since

### 现象

`ZentaoAdapter.fetch_cases(product_id, since=...)` 已经支持：

- 列表分页；
- 按 lastEditedDate 过滤；
- 只对候选行补取详情。

但 `CaseService.sync()` 原来调用：

```python
zentao.fetch_cases(product_id)
```

没有传 `since`。

结果是：

- UI 名称叫“增量同步”；
- 本地 ingest 会判断 unchanged，最终数据没有被错误覆盖；
- 但远端列表缺 `steps` 时，仍可能对产品全部用例逐条 GET detail。

数据量大时会放大：

- 禅道 API 压力；
- 同步耗时；
- 认证/网络失败概率；
- 后台任务等待时间。

### 修复

新增按产品隔离的同步水位。

默认“增量同步”：

1. 读取 `source_sync_state(source='zentao', scope=<product_id>)`；
2. 使用该 watermark 作为 `since`；
3. 只补取水位之后的新增/修改用例详情；
4. ingest 仍二次比较 updated_at，保持幂等；
5. 完成后更新 watermark。

首次运行或旧库没有状态时，回落到该产品现有 dsl_case 的最大 `zentao_updated_at`。

## 4. 本轮新发现 Z8：0 变更时“上次同步”时间不更新

以前 UI 用：

```text
MAX(dsl_case.last_synced_at)
```

近似表示“上次同步”。

真正使用 `since` 后，如果远端 0 变化：

- 没有任何 case 被 ingest；
- 没有任何 case 的 last_synced_at 更新；
- 用户刚刚成功同步，UI 却仍显示旧时间。

这两个概念本来就不同：

- case.last_synced_at = 某条用例最后一次被同步/确认；
- source last sync = 产品源最后一次成功完成同步。

### 修复

新增：

```sql
source_sync_state(
  source,
  scope,
  watermark,
  synced_at,
  detail_json,
  PRIMARY KEY(source, scope)
)
```

禅道每次成功 sync 都写：

- `source='zentao'`；
- `scope=product_id`；
- `watermark`；
- `synced_at`；
- `detail.scanned/full`。

`case.list` 同时返回 `sync_state`。

桌面用例页优先显示 `sync_state.synced_at`；旧库没有状态时再回退用例行时间。

## 5. 增量同步与全量校准的 UI 语义

现在明确分成两个按钮：

### 增量同步

- 默认操作；
- 使用 product watermark；
- 只处理水位之后的候选；
- 适合日常使用。

### 全量校准

- 显式 `full=true`；
- 忽略 watermark；
- 完整扫描产品；
- 用于迁移、首次校准、怀疑远端时间戳异常/漏同步时；
- CLI 对应：

```powershell
testpilot case sync --full
```

这比把“是否全量”藏在实现里更容易排查问题。

## 6. REST 版本边界

当前仍**只支持 REST v1**。

适配器初始化时会拒绝 v2，避免把不同字段契约混用。

当前官方 v1 文档仍可用于确认：

- 单 testcase 详情：`GET /testcases/:id`
- 产品 testcase 列表：`GET /products/:productID/testcases`

禅道官方已经有 v2 文档，字段/接口契约与 v1 并不相同，因此不能通过“改 api_version 字符串”直接兼容。

未来若要支持 v2，建议新增独立 adapter：

```text
ZentaoPort
 ├─ ZentaoV1Adapter
 └─ ZentaoV2Adapter
```

而不是在当前 V1 类里铺版本 if/else。

参考：

- https://www.zentao.net/book/api/get-testcases-id-1080.html
- https://www.zentao.net/book/api/get-products-productid-testcases-1086.html
- https://www.zentao.net/book/api/post-products-productid-testcases-1088.html
- https://www.zentao.net/book/api/get-products-productid-testcases-2268.html

## 7. 仍保留的边界

当前代码不宣称“兼容所有禅道版本/二开实例”。

仍需在公司专用测试实例做环境验收：

1. 目标 v1 版本的列表/详情真实字段；
2. module endpoint；
3. build 的实际语义；
4. 测试单创建字段；
5. 创建时关联用例是否被实例接受；
6. task update 的必填字段；
7. desc/status 回读；
8. 分页与 lastEditedDate 格式；
9. 权限不足/Token 失效；
10. 大产品用例数量下增量性能。

这属于 ENV 验证，不阻塞当前功能开发。

## 8. 下一步建议

优先级：

1. 保持 v1 适配器稳定，不混入 v2；
2. 增加可选的同步统计（列表请求数 / 详情请求数 / 耗时）便于量化优化效果；
3. 真实公司禅道验收时记录版本与接口样例；
4. 若后续需要“SVN 变更生成用例后同步到禅道”，仍先落 TestPilot 本地 draft + 人审，审核后再设计显式发布动作，禁止 Agent 直接写禅道。

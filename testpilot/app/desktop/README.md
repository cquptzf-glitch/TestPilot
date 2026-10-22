# desktop —— Tauri 2 壳 + React 前端（M4-1 / M4-3）

```
testpilot/app/desktop/
├── src-tauri/              Rust 壳：sidecar 监督 + JSON-RPC 桥 + 资源白名单读取
│   ├── src/main.rs         入口：setup、命令表（rpc_call / engine_* / read_*）、退出清理
│   ├── src/engine.rs       sidecar 解析与监督（崩溃重启指数退避，上限 3 次）
│   ├── src/rpc.rs          WebSocket JSON-RPC 客户端（请求/通知分流、断线失败在途请求）
│   ├── tauri.conf.json     窗口、CSP、resources（sidecar onedir 目录）
│   └── capabilities/default.json  只授 core:default（业务全走自定义命令）
├── src/                    React 18 + TS（纯视图，不直连文件系统/网络）
│   ├── App.tsx             顶部导航（图标+快捷键）+ hash 路由 + 命令面板/主题挂载
│   ├── bridge.ts           唯一对外通道：RPC 方法表 + 壳命令（**跨语言契约门禁守它**）
│   ├── hooks.ts            useAsync / useRoute / useEngine / useEngineEvents
│   ├── theme.ts            浅/深主题（localStorage + prefers-color-scheme，首帧前由 index.html 定值）
│   ├── devMock.ts          仅 `npm run dev` 生效的壳仿真（样例数据；生产构建被剔除）
│   ├── components/         Bits / Ui / Telemetry / Icons / Palette（命令面板 + 快捷键速查）
│   └── pages/              Cases · Suites · Runs · RunDetail · KBReview · DslReview · Tasks · Settings
└── dist/                   vite 产物（前端构建输出）
```

## 架构边界（设计 2.5）

| 层 | 做什么 | 明确不做 |
|---|---|---|
| Rust 壳 | 拉起并监督 Python sidecar、转发 RPC、把引擎事件转成 Tauri 事件、白名单内读报告/截图 | 不含任何测试业务逻辑 |
| React 前端 | 报告钻取、人审队列、DSL 审阅、任务面板 | 不直连数据库/外网/文件系统 |
| Python 引擎 | 全部业务（执行/知识/存储/适配/LLM） | 不依赖壳存在（CLI 形态独立可跑） |

**IPC**：`ws://127.0.0.1:9301`，JSON-RPC 2.0。请求走壳命令 `rpc_call`；
引擎下行**通知**（`run.progress` / `case.finished` / `step.finished`）由壳转成
Tauri 事件 `engine://<event>`，前端订阅即可，无需轮询。

**前端可用的壳命令**（`src/bridge.ts` 里全部有封装）：

| 命令 | 用途 |
|---|---|
| `rpc_call(method, params)` | 唯一业务入口（方法表见下） |
| `engine_status()` / `engine_log(lines)` / `engine_restart()` | 状态徽章、日志抽屉、手动重启 |
| `sync_roots(paths)` | 用 `health.paths` 注册资源白名单根目录 |
| `read_text_file(path)` / `read_asset_data_url(path)` | 读报告 Markdown / 读截图（返回 data URL） |

## 引擎方法表（`RpcService.methods()`，43 个）

| 域 | 方法 | 页面 |
|---|---|---|
| 基础 | `health` `shutdown` | 状态徽章 / 设置 |
| 用例 | `case.pull` `case.sync` `case.list` `case.detail` `case.export` `case.import` `case.parse` | Cases |
| 方案 | `suite.list` `suite.detail` `suite.create` `suite.update` `suite.delete` | Suites |
| 任务 | `task.create` `task.run` `task.abort` `task.list` `task.detail` `task.syncPending` | Tasks |
| 报告钻取 | `run.list` `run.detail` `run.caseDetail` `run.stepTrace` `run.report` `run.shotDiff` `run.compare` | Runs / RunDetail |
| 人审 | `kb.pending` `kb.confirm` `kb.reject` `kb.setDisposition` | KBReview |
| 知识维护 | `kb.caseCtxList` `kb.caseCtxSave` `kb.caseCtxDelete` `kb.envCompat` | KBReview / RunDetail |
| DSL 审阅 | `dsl.review` `dsl.save` `dsl.accept` `dsl.reject` | DslReview |
| 设置 | `config.describe` `config.apply` `config.reset` `config.overrideText` | Settings |

> `run.detail` 的返回体同时携带 `stability`（每用例最近 10 次执行的稳定性，跨 run 派生视图）
> 与 `stats.drivers`（驱动降级统计），前端不需要为这两项额外发请求。
> 失败复跑的用例（`case_exec.attempts > 1`）在轨迹页提供「第几次执行」切换：
> `run.stepTrace` 的 `attempt` 参数指定看哪一次（缺省 = 最后一次）。

> 前端 `RPC_METHODS` 必须与上表完全一致 —— 由 `tests/test_desktop_contract.py`
> 跨语言守门：改一边不改另一边，pytest 直接失败。

## 打包形态（为什么不用 externalBin）

引擎是 **PyInstaller onedir** 产物（`testpilot-sidecar.exe` + `_internal/` 依赖）。
Tauri 的 `externalBin` 只搬运**单个**可执行文件，onedir 的依赖目录带不进安装包，
会出现"打包成功、运行即找不到 DLL"。因此：

1. `scripts/build-sidecar.ps1` 产出 `dist/testpilot-sidecar/`，
   并复制到 `src-tauri/binaries/sidecar/`；
2. `tauri.conf.json` 用 `bundle.resources = ["binaries/sidecar/*"]` 整目录打包；
3. 壳按顺序解析可执行文件：`TESTPILOT_SIDECAR` 环境变量 → 资源目录
   （`resource_dir/sidecar/` 或 `resource_dir/binaries/sidecar/`）→ 开发目录。

## 开发与构建

```powershell
# 一次性：装依赖
cd testpilot/app/desktop
npm install

# 开发态（壳 + 引擎一起起；需先 build-sidecar 或设 TESTPILOT_SIDECAR 指向 python.exe）
npm run shell:dev

# 只调前端（无引擎，页面会明确提示"不在 Tauri 壳内"）
npm run dev

# 仅类型检查 + 前端构建
npm run build

# 一体化打包（sidecar -> 前端 -> NSIS 安装包 -> 顶层 dist/bundle）
cd D:\MyCode\TestPilot
powershell -ExecutionPolicy Bypass -File scripts/build-shell.ps1
```

## 已知边界（明确记录，不含假实现）

| 项 | 现状 | 归属 |
|---|---|---|
| 安装包在**干净机器**上的安装与运行 | 本机已装开发环境，结论不可信；需无 Python/Node 的机器实测 | M4-1 尾项 |
| UIA 实机联调 / 多缩放 DPI 像素对齐 | 代码与门禁就绪，需被测软件适配与真机回归 | M1-5 / M3-4 尾项 |
| M5+ 探索的 agent 运行时 | `explore/` 只交付工具层 + MCP server，agent 由外部接入；产物落 `draft` 待人审 | 非承诺 |

已补齐的交互约定：`Esc` 关闭日志抽屉、RunDetail 一键复制可直开深链
（`#/run?runId&caseExecId&stepNo`）、`Ctrl+K` 命令面板（搜页面 / run / 用例 / 动作）、
`Alt+1~7` 直达页面、`?` 快捷键速查、轨迹页 `J/K` 步进、
浅/深主题切换（跟随系统，localStorage 持久化）。

> `npm run dev` 现在内置壳仿真（`src/devMock.ts`，仅 dev 构建生效）：无引擎也能以
> 样例数据完整渲染所有页面，供样式/交互开发与截图回归使用。

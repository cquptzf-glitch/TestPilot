# Qt 模拟被测软件与接口实验室

本实验室使用 **C++17 / Qt 6.8.3 / Qt Widgets / Qt Quick-QML / Qt Network** 编写被测软件，提供本地禅道 REST 和设备模拟器 WebSocket 服务，并通过 TestPilot 的实际 JSON-RPC、适配器和 Windows UIA 驱动执行验收。源代码位于 testpilot/simulation/。

## 启动与测试

在仓库根目录 D:/MyCode/TestPilot 打开 PowerShell：

```powershell
# 启动已编译的 Qt 软件，以及禅道、模拟器和离线解析服务
.venv\Scripts\python.exe -m testpilot.simulation serve --app-exe dist/simulation/qt-special/MockInstrument.exe

# 重跑完整接口及原生 Qt 界面测试；结果写到新的隔离目录
.venv\Scripts\python.exe -m testpilot.simulation test --gui --app-exe dist/simulation/qt-special/MockInstrument.exe

# 仅测试网络、状态和故障恢复，适用于无交互桌面的环境
.venv\Scripts\python.exe -m testpilot.simulation test

# 重新编译 Qt 软件并部署 Qt DLL/插件（需要 Qt 6 SDK、CMake、MSVC 2022）
.venv\Scripts\python.exe -m testpilot.simulation build-app --out dist/simulation/qt-special/MockInstrument.exe
```

运行 GUI 测试时请保持桌面解锁，测试会操作它自己启动的 Qt 窗口。测试目录默认使用时间戳，不覆盖已有验收结果；可用 --out 指定一个不存在的目录。Ctrl+C 停止模拟服务。--http-port 和 --ws-port 可更换监听端口；--data-dir 指定演示配置和导出目录。需要指定 Qt SDK 时使用 TESTPILOT_QMAKE 指向 qmake6.exe。

Qt 程序发布目录：dist/simulation/qt-special/。打包文件：dist/simulation/QtMockInstrument-Widgets-QML-win64.zip。运行时保留 DLL 及 platforms 等子目录；不要单独移动 exe。软件接收两个可选参数：HTTP 服务根地址、报告导出目录。

## 本地服务

| 服务 | 默认地址／协议 |
|---|---|
| 状态面板 | http://127.0.0.1:19380/ |
| 健康检查 | GET /health |
| 禅道登录 | POST /api.php/v1/tokens，账号密码 demo / demo |
| 产品用例及模块 | GET /api.php/v1/products/1/testcases、GET /api.php/v1/products/1/modules |
| 用例详情 | GET /api.php/v1/testcases/{id} |
| 创建测试任务 | POST /api.php/v1/testtasks |
| 回读／回写汇总 | GET / PUT /api.php/v1/testtasks/{id} |
| 模拟器 | ws://127.0.0.1:19381，协议版本 1.0 |
| 接口请求与设备记录 | GET /lab/snapshot |
| 故障注入 | POST /lab/faults，设置 create/writeback/readback/auth 的剩余失败次数 |
| Qt 软件设备通道 | GET /instrument/state；POST /instrument/measure、/instrument/export、/instrument/scene |
| 离线解析样例 | POST /v1/chat/completions，仅返回本实验室固定用例的 DSL，不调用真实模型 |

所有服务仅绑定 127.0.0.1。禅道模拟接口检查 Token 头，支持分页、401 重新登录、建任务回读和结果回写。模拟器命令为 hello、load_scene、query_state、history、start_measurement、export_report、inject_error；请求包含 id/cmd/args/ver，回执包含 id/ok/data 或 err/elapsed_ms。

serve 会打印隔离 config.json 的路径。其他 CLI 进程可使用此配置连接模拟接口；如果宿主环境设置了 HTTP 代理，请先为该 PowerShell 进程设置：

```powershell
$env:NO_PROXY = "127.0.0.1,localhost"
.venv\Scripts\python.exe -m testpilot --config .cache/simulation-live/config.json case sync --parse
```

## 内置场景和验收判据

| 用例 | 验收目标 |
|---|---|
| TC-91001 | 正常检测完成，数值 42.5，汇总成功回写禅道 |
| TC-91002 | wrong_value 场景返回 99.9，断言必须失败 |
| TC-91003 | slow 场景异步完成，状态轮询应成功 |
| TC-91004 | device_error 场景返回错误回执，用例必须失败 |
| TC-91005 | 不存在的前置场景必须阻塞执行 |
| TC-91006 | 附加 Qt 窗口、输入 UI-001、选择 Precision、点击检测、验证状态/数值及界面 Completed、导出真实 JSON 文件 |
| TC-91007 | 长等待可通过第二条鉴权 RPC 连接及时中止 |

此外测试连续回写失败后补偿、创建失败后补偿、创建成功但回读失败时保留远端 ID 且不重复建任务、登录失效后重放、RPC 历史查询与报告生成。

Qt 软件通过 QNetworkAccessManager 异步访问模拟器核心，每 200 ms 更新状态，不把接口请求放在阻塞 UI 的同步等待中；原生 QLineEdit、QComboBox、QPushButton、QLabel 带稳定 objectName 和可访问名称。服务的场景 generation 防止旧的延迟检测结果覆盖新场景。

## 本轮发现并修复的驱动问题

1. 按标题附加时，桌面级 UIA 遍历可能卡在其他应用的提供器中：先使用 Win32 定位目标窗口句柄，再进入对应 UIA 子树。
2. Qt QLineEdit 的 rich_text 是输入值，而 Locator.name 表示可访问名称：改用 UIA accessible name 精确匹配，并保存可读的定位诊断信息。
3. Qt QComboBox 不是原生 Win32 ComboBox，不支持 CB_GETCOUNT 等消息：展开后等待其异步生成的 UIA ListItem，按精确名称或合法序号选择可见选项，再等待当前值确认；弹窗期间不重新设置控件焦点。错误名称、越界序号、弹窗延迟及取消均有回归覆盖。
4. Qt 子控件可能没有独立 HWND，仅调用其 UIA SetFocus 不足以确保窗口位于前台：物理输入前先激活对应原生顶层窗口，避免点击落到遮挡窗口。

## 验收证据与边界

全量回归：799 项 Python 测试全部通过；101 个文件静态检查无问题。详细结果见 logs/verify-report.md。

2026-10-15 已完成实际 C++ Qt GUI 联调：**11 轮任务、29 项检查全部通过**（包含连续两轮 Qt GUI 操作）；Qt 软件导出的文件包含 sample_id=UI-001、mode=Precision、value=42.5。证据目录为 dist/simulation/evidence-qt-20261015-final/，概览见 acceptance.md，实际 Qt 截图为 dist/simulation/qt-measurement.png。

验收结果写入 acceptance.md / acceptance.json；service-evidence.json 保存模拟服务请求、任务回写及设备命令；reports/ 是真实引擎生成的报告，shots/ 是实际窗口截图，exports/ 是 Qt 软件实际导出的 JSON，exports/ui-events.jsonl 保存 Qt 收到的鼠标、按键和焦点事件。错误场景的 fail/blocked 是预期结果，不应解释为验收失败。

本轮 GUI 验收使用 C++ Qt 程序，模拟禅道、模拟器和解析服务；未验证真实禅道、真实设备或真实 LLM/VLM 的效果。无 Qt 界面测试时不装配交互驱动，不把接口结果冒充 UIA 验收。新增专项已验证 Qt Quick/QML 与带可访问性的自绘滑块/Canvas。干净机器安装、多 DPI、WebView、完全不暴露可访问树的自绘控件和真实 VLM 仍待验证。

2026-10-16 已重新构建 TestPilot 桌面端和内置 sidecar，并从桌面任务页面点击执行 57 步 Qt 专项，全部通过。完整路径、输入方式和证据见 [Qt 专项记录](qt-specialist-2026-10-16.md)。


## Widgets / QML 专项

```powershell
.venv\Scripts\python.exe -m testpilot.simulation test-qt --app-exe dist/simulation/qt-special/MockInstrument.exe --out .cache/qt-special-manual
```

专项使用同一个真实 RPC / TaskOrchestrator / Executor / UIADriver，执行 14 轮任务和 25 项检查；其中越界单元格、错误下拉选项预期失败。已连续两轮通过，证据位于 dist/simulation/evidence-qt-special-20261016/。

桌面层还从 TestPilot 任务页直接点击执行，由 Rust 桥连接打包 sidecar，验证同组 57 步。tools/prepare_desktop_qt.py 只准备数据，不调用 task.run；tools/verify_desktop_qt.py 只读取结果并校验 Qt 独立状态。

Qt 文本默认使用 uia.qt_text_input=value，通过可访问值接口输入，避免 VK_PACKET 与输入法转换；需要逐字键盘事件时显式选 keys 并配置文本断言。Qt 表格缺少滚动接口时使用键盘定位，可能改变当前选择，但不修改单元格数据。


2026-10-16 AI/禅道修复后，已用真实 pumen-vllm 完成模型验收，并执行 AI 生成 DSL → 真实 Qt → 禅道模拟服务回读的完整链路。现有固定模型响应仍只用于确定性故障注入回归，不代表真实推理；真实推理证据与桌面点击证据见 [AI 与禅道修复记录](fix-ai-zentao-2026-10-16.md)。

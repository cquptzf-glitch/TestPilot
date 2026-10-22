# Qt 专项适配与真实桌面验收

日期：2026-10-16。目标：修复已确认缺陷，扩展 Qt 功能适配，并区分引擎验收和桌面端验收。

## 修复

- 补偿回写同时核验运行记录和统计中的 completed 状态；中止、异常或未完成运行不能回写禅道 done。远端补偿创建后仍保留待同步状态，保存回读异常携带的远端 ID，避免重复创建。
- 文本断言每次定位使用剩余预算，不再重新获得完整超时；超过截止时间的成功结果按超时处理。状态查询的连接、握手和回执共用预算，等待回执时可响应取消。用真实延迟 WebSocket 服务覆盖超时与取消。
- 启动/附加重新绑定窗口与进程所有权；启动程序先按新 PID 找 Win32 HWND 再查询 UIA，避免激活旧窗口。物理点击检查控件中心是否位于当前窗口内。
- 首屏请求早于 RPC 连接时，在连接就绪后自动刷新数据和健康检查，清理旧错误提示。已用打包程序冷启动验证，无需点击重试。
- 任务列表批量附带每个任务最近一次运行，修复已完成显示“未执行”、运行中状态缺失的问题；任务详情按任务过滤后分页，避免其他任务的 200 条新记录遮掉旧任务历史。

## Qt 覆盖

| 功能 | 实现与验证 |
|---|---|
| 输入 | Qt 默认 Value 接口；中文、数字和 +^{} 等符号通过内容断言 |
| 复选框/单选 | ui.toggle 设置确定状态并校验；重复勾选保持幂等；单选实际状态校验 |
| 页签 | Qt QTabBar 暴露为 Tab，容器暴露为 Group，按实际可访问类型选择 |
| 数值/日期/滑块 | 正常键盘操作；独立文件校验 17、2027-10-16、7 |
| 表格 | row/column 从 0 起；Grid pattern 读取/编辑；第 80 行滚动定位；越界预期失败 |
| 树与列表 | 展开 Laboratory，选择 Chemistry / Beta，并校验实际选择 |
| 模态对话框 | 定位子窗口，输入确认码，确认/取消，再附加主窗口 |
| 多窗口 | 主/辅助窗口具有同名输入控件，验证内容相互独立 |
| 菜单 | 真实菜单项触发应用重置事件 |
| QML | TextField、CheckBox、ComboBox、Button、StaticText、可访问 Canvas |
| QML 下拉 | ExpandCollapse 在实机不打开弹层，改用实际点击；从窗口 overlay 找选项，并从内部 Edit 读取当前选中值 |
| 自绘 | 自绘 QSlider 保留可访问值接口；QML Canvas 提供可访问角色和名称，点击后的计数真实变化 |

新增 DSL 动作为 ui.toggle、ui.keypress；表格定位新增 row/column。解析动作白名单、语义审核、执行器、结构化步骤编辑器及模型提示已同步。

## 怎么测试

### 引擎与驱动

调用路径：simulation.qt_acceptance → EngineRpc → 实际鉴权 JSON-RPC → TaskOrchestrator / Executor → 本工程 UIADriver → C++ Qt 应用。

脚本通过 case.import、dsl.accept、task.create、task.run 执行。Qt 应用独立输出状态文件，验收脚本同时核对界面断言和应用实际状态，不能仅凭“驱动没有抛异常”判定成功。

- 两轮专项：每轮 14 个任务、25 项检查通过；包含 2 个预期失败用例。
- 接口及基础 Qt 联调：11 个任务、30 项检查通过，含中止后补偿不能标记 done。
- Python：761 项测试全部通过，99 个文件静态检查无问题。
- 前端：TypeScript 与 Vite 生产构建通过。

证据：
- ../dist/simulation/evidence-qt-special-20261016/qt-acceptance.md
- ../dist/simulation/evidence-qt-network-20261016/acceptance.md
- ../logs/verify-report.md

### 真实 TestPilot 桌面端

启动实际 release TestPilot，使用它随包的 sidecar。tools/prepare_desktop_qt.py 仅启动本地服务、导入审核用例、创建任务；不调用 task.run。

通过 computer-use 操作 TestPilot：测试执行 → 任务管理 → “Qt 专项：桌面端真实驱动验证” → 执行。调用路径为真实桌面 React 界面 → Tauri Rust 桥 → 随包 sidecar → Executor / UIADriver → Qt 应用。

已连续从界面完成 task #1 / run #1 和 task #2 / run #2：每轮 57 个步骤全部通过，其中 46 次 UIA 交互成功、0 次失败；Qt Widgets 和 QML 独立状态与期望一致，证据完整，无模型调用。第二轮由 dist/desktop/qt-20261016/testpilot-shell.exe 及其随包 sidecar 执行，用时 45.6 秒。这些记录与脚本 RPC 验收分开保存。

最终版本 dist/desktop/qt-20261016-final/testpilot-shell.exe 冷启动后，任务列表已显示 #1 / #2 已完成、#3 未执行，符合实际记录；截图 desktop-task-history-final.jpg。#3 是便于现场观察而新建的同组 57 步任务，未计入已通过运行次数。

证据：[第二轮桌面验收](../dist/simulation/evidence-desktop-qt-20261016/desktop-acceptance.md)、[实际结果截图](../dist/simulation/evidence-desktop-qt-20261016/desktop-result.jpg)、[实际运行报告](../dist/simulation/evidence-desktop-qt-20261016/reports/2/report.md)、exports/。第一轮保存在 ../dist/simulation/evidence-desktop-qt-20261016-first/。

## 复现

```powershell
.venv\Scripts\python.exe tools\verify.py
.venv\Scripts\python.exe -m testpilot.simulation test-qt --app-exe dist/simulation/qt-special/MockInstrument.exe --out .cache/qt-special-manual
.venv\Scripts\python.exe -m testpilot.simulation test --gui --app-exe dist/simulation/qt-special/MockInstrument.exe --out .cache/qt-network-manual
```

桌面成品：dist/desktop/qt-20261016-final/testpilot-shell.exe（连同 binaries 目录使用）；安装包：dist/bundle/qt-20261016/TestPilot_0.1.0_x64-setup.exe（58,006,076 字节，SHA256：7a4df748c5642ce8478fdb3f8b5801d14b7a936b044d148d81422b6436c455c0）。完整制品校验值见 dist/bundle/qt-20261016/artifacts.json。

手动观察桌面驱动（此准备命令不执行任务）：

```powershell
.venv\Scripts\python.exe tools\prepare_desktop_qt.py --source dist/simulation/evidence-qt-special-20261016 --root .cache/desktop-qt-manual --shell dist/desktop/qt-20261016-final/testpilot-shell.exe --app-exe dist/simulation/qt-special/MockInstrument.exe
```

在打开的 TestPilot 中选择“测试执行 → 任务管理”，点击“Qt 专项：桌面端真实驱动验证”的“执行”。保持命令进程运行以提供本地禅道/模拟器服务。可在“轨迹回放”中查看每步截图，Qt 窗口会实际发生操作。

Qt 运行包：../dist/simulation/QtMockInstrument-Widgets-QML-win64.zip。需要完整解压，保留 DLL、platforms、qml 等目录。构建需要 Qt 6 Widgets/Quick/Qml/Network、CMake、MSVC。

## 限制

- Value 接口输入属于功能操作，不模拟逐字符键盘事件。keys 模式受 Qt 版本、键盘布局与输入法影响，必须设置内容断言。不要把本次结果描述为所有键盘事件路径通过。
- Qt 表格提供器会将屏外单元格报告为可见，且可能没有 Scroll pattern；键盘滚动回退会改变当前选择。大规模虚拟表格和超长下拉列表未验收。
- 自绘控件必须暴露可访问角色/状态；完全没有可访问性的画布仍需模板或真实 VLM，未做真实模型验收。
- 真实业务 Qt 软件、真实禅道/设备、WebView、干净机器、多 DPI、锁屏或长期无人值守不在本次通过范围。

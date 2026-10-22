引擎运行日志与验证报告的输出目录（`config/default.yaml` 的 `log.dir`）。

本目录必须存在（Python 包内不便附带空目录），因此保留此占位文件。

- `testpilot.log`：结构化 JSON 日志（stdout 只留命令结果，日志走 stderr）
- `verify-report.md`：`python tools/verify.py` 生成的验证报告
- `pytest.log` / `pytest.xml` / `ruff.txt`：验证过程的原始产物

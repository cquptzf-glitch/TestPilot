"""PyInstaller 打包入口 —— 桌面壳拉起的 Python 引擎进程。

职责刻意保持极简：**只调用 CLI**（`testpilot serve`），不复制任何业务逻辑。
真正的能力都在 `testpilot/` 包里，因此 sidecar 与 CLI 形态行为完全一致。

本地等价用法（无需打包）：
    python packaging/run_sidecar.py serve
"""
import os
import sys
from pathlib import Path

# pydantic 的插件发现会遍历**所有**已安装分发包的 entry_points 元数据，
# 任何一份元数据不可读（被安全软件加密 / 装包中断残留 / AV 隔离）都会抛
# UnicodeDecodeError，而它发生在 `model_rebuild` 期间 —— 结果是
# **连 core/dsl.py 都 import 不进来，整个引擎起不来**。
#
# 本工程不使用 pydantic 插件（无 entry_points 声明 `pydantic` 组），
# 因此在打包入口里显式关闭该发现流程：功能零损失，换来"一份坏元数据不再致命"。
# 这符合设计 1.3 的核心原则 —— 单点异常不得打断执行主干。
#
# 用 setdefault：运维若确有插件需求（`PYDANTIC_DISABLE_PLUGINS` 显式设为空串/插件名），
# 仍可自行覆盖。
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "__all__")

# 允许在未安装（未 pip install -e）的源码树里直接运行
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from testpilot.app.cli import main  # noqa: E402  （路径注入之后再导入）

if __name__ == "__main__":
    sys.exit(main())

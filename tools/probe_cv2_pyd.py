"""探测：能否**绕过 cv2 的 Python 引导文件**，直接加载原生扩展 cv2.pyd。

动因：`cv2/__init__.py` 等 Python 引导文件在部分环境下不可读（被安全软件加密、
装包残留损坏），此时 `import cv2` 抛 `source code string cannot contain null bytes`，
视觉驱动整个不可用 —— 而真正干活的 `cv2.pyd`（二进制）是好的。

本脚本验证 fallback 的可行性：用 importlib 直接按 .pyd 路径装载，
检查 API 是否完整（__version__ / imread / matchTemplate / imencode）。

用法：.venv\\Scripts\\python.exe tools/probe_cv2_pyd.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(r"D:\MyCode\TestPilot")

CANDIDATES = [
    ROOT / "dist/testpilot-sidecar/_internal/cv2/cv2.pyd",
    ROOT / ".venv/Lib/site-packages/cv2/cv2.pyd",
    ROOT / ".venv/Lib/site-packages/cv2.pyd",
]

APIS = ["__version__", "imread", "imdecode", "imencode", "matchTemplate",
        "minMaxLoc", "cvtColor", "TM_CCOEFF_NORMED", "IMREAD_COLOR"]


def main() -> int:
    print("--- 1. 常规 import cv2 ---")
    try:
        import cv2
        print(f"    OK  版本={getattr(cv2, '__version__', '?')}")
    except Exception as exc:
        print(f"    失败 {type(exc).__name__}: {exc}")

    print("\n--- 2. 直接加载 .pyd（fallback 路径） ---")
    for pyd in CANDIDATES:
        if not pyd.exists():
            print(f"    跳过（不存在）{pyd}")
            continue
        print(f"    尝试 {pyd}")
        # 先清掉可能已导入的 cv2，避免相互污染
        for name in [n for n in sys.modules if n == "cv2" or n.startswith("cv2.")]:
            del sys.modules[name]
        try:
            loader = importlib.machinery.ExtensionFileLoader("cv2", str(pyd))
            spec = importlib.util.spec_from_loader("cv2", loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules["cv2"] = module
            loader.exec_module(module)
        except Exception as exc:
            print(f"    失败 {type(exc).__name__}: {exc}")
            continue
        missing = [a for a in APIS if not hasattr(module, a)]
        print(f"    装载成功 版本={getattr(module, '__version__', '?')}")
        print(f"    API 缺失: {missing or '无'}")

        # 真跑一次编解码，确认不是"能 import 但不能用"
        try:
            import numpy as np
            img = np.zeros((8, 8, 3), dtype=np.uint8)
            ok, buf = module.imencode(".png", img)
            back = module.imdecode(buf, module.IMREAD_COLOR)
            print(f"    编解码可用: imencode={ok} imdecode.shape={back.shape}")
        except Exception as exc:
            print(f"    编解码失败: {type(exc).__name__}: {exc}")
        return 0 if not missing else 1

    print("\n未找到可用的 cv2.pyd")
    return 1


if __name__ == "__main__":
    sys.exit(main())

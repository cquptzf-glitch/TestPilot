"""判定 DLP 密文是「落盘加密」还是「按进程过滤读取」。

方法：白名单进程（python）读同一文件，比较两个量：
  - `os.path.getsize()`      → 文件系统报告的大小（磁盘真实字节数）
  - `len(open('rb').read())` → 白名单进程读到的字节数（透明解密后的）

若 磁盘大小 > 明文长度 => 磁盘上是**密文**（多出密文头/填充），
  则产物**不能分发给无 DLP 的机器**（对方拿到的是一堆二进制垃圾）。
若两者相等 => 磁盘上是明文，密文只是"非白名单进程的读取视图"，
  本机把 sidecar 命名为白名单名即可，产物可直接分发。

只读，不修改任何文件。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(r"D:\MyCode\TestPilot")
MAGIC = b"\x62\x14\x23\x65"

PROBES = [
    ("dist cv2/__init__.py", ROOT / "dist/testpilot-sidecar/_internal/cv2/__init__.py"),
    ("dist cv2/config.py", ROOT / "dist/testpilot-sidecar/_internal/cv2/config.py"),
    ("dist numpy entry_points", ROOT / "dist/testpilot-sidecar/_internal/numpy-2.5.3.dist-info/entry_points.txt"),
    ("dist default.yaml", ROOT / "dist/testpilot-sidecar/_internal/testpilot/config/default.yaml"),
    ("dist cv2.pyd (对照·二进制)", ROOT / "dist/testpilot-sidecar/_internal/cv2/cv2.pyd"),
    ("venv cv2/__init__.py (对照)", ROOT / ".venv/Lib/site-packages/cv2/__init__.py"),
    ("venv numpy entry_points (对照)", ROOT / ".venv/Lib/site-packages/numpy-2.5.3.dist-info/entry_points.txt"),
    ("源码 default.yaml (对照)", ROOT / "testpilot/config/default.yaml"),
]


def probe(label: str, path: Path) -> None:
    if not path.exists():
        print(f"{label}: 不存在")
        return
    size = os.path.getsize(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"{label}: 读取失败 {exc}")
        return
    n = len(data)
    starts_enc = data[:4] == MAGIC
    has_nul = b"\x00" in data[:256]
    verdict = "密文(磁盘)" if size != n else ("明文(磁盘)" if not starts_enc else "? 疑似密文但等长")
    print(f"{label}")
    print(f"    getsize={size}  read_len={n}  delta={size - n}  "
          f"head={data[:4].hex(' ')}  前256含NUL={has_nul}")
    print(f"    判定：{verdict}")


for label, path in PROBES:
    probe(label, path)

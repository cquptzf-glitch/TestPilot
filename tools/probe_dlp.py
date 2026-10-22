"""DLP 探测：确认哪些文件在"非白名单进程"眼里是密文。

背景：本机装了 E-SafeNet DLP，按**进程可执行文件名**放行（`Code.exe` 放行，
其它一律返回密文，头部 `62 14 23 65`）。打包出的 sidecar 文件名不在白名单里，
于是 PyInstaller 复制进产物的某些文件本身可能就是密文 —— 这会让安装版
在运行时读配置/模块时抛 `UnicodeDecodeError`。

本脚本从 python 进程视角（= 打包进程与 sidecar 进程同类）逐个探测关键文件，
输出「明文 / 密文」，用来界定问题范围。**只读，不改任何文件。**
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"D:\MyCode\TestPilot")
MAGIC = b"\x62\x14\x23\x65"

PROBES = [
    # (标签, 路径)
    ("venv: numpy entry_points", ROOT / ".venv/Lib/site-packages/numpy-2.5.3.dist-info/entry_points.txt"),
    ("venv: cv2/__init__.py", ROOT / ".venv/Lib/site-packages/cv2/__init__.py"),
    ("venv: cv2/config.py", ROOT / ".venv/Lib/site-packages/cv2/config.py"),
    ("venv: websockets entry_points", ROOT / ".venv/Lib/site-packages/websockets-17.1.dist-info/entry_points.txt"),
    ("venv: pydantic dist-info 目录", ROOT / ".venv/Lib/site-packages/pydantic-2.13.5.dist-info"),
    ("src: default.yaml", ROOT / "testpilot/config/default.yaml"),
    ("dist: numpy entry_points", ROOT / "dist/testpilot-sidecar/_internal/numpy-2.5.3.dist-info/entry_points.txt"),
    ("dist: cv2/__init__.py", ROOT / "dist/testpilot-sidecar/_internal/cv2/__init__.py"),
    ("dist: default.yaml", ROOT / "dist/testpilot-sidecar/_internal/testpilot/config/default.yaml"),
]


def head(path: Path, n: int = 8) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(n)
    except OSError as exc:
        return f"<ERR {exc}>".encode()


def main() -> int:
    encrypted: list[str] = []
    for label, path in PROBES:
        if path.is_dir():
            files = sorted(p for p in path.iterdir() if p.is_file())
            bad = [p.name for p in files if head(p, 4) == MAGIC]
            print(f"{label}: {len(files)} 个文件，密文 {len(bad)} 个 {bad}")
            encrypted += [f"{label}/{n}" for n in bad]
            continue
        if not path.exists():
            print(f"{label}: 路径不存在 {path}")
            continue
        raw = head(path, 16)
        is_enc = raw[:4] == MAGIC
        print(f"{label}: {'密文' if is_enc else '明文'}  head={raw.hex(' ')}")
        if is_enc:
            encrypted.append(label)

    print()
    print(f"密文项合计：{len(encrypted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

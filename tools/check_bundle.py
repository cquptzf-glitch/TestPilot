"""打包产物自检 —— 验证 sidecar 的 onedir 产物里**隐式依赖是否真的进了包**。

为什么需要它：`--version` 这类自检只走到 argparse，**不触发惰性导入**（设计决策 D5：
GUI 依赖一律方法内惰性导入）。于是"缺 DLL/缺模块"要到实机联调点开视觉驱动才炸 ——
典型症状是"开发能跑、打包跑不起来"。本脚本直接读 PyInstaller 的 TOC 与产物目录清单，
在打包**当下**就把缺失顶出来。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools/check_bundle.py
    .venv\\Scripts\\python.exe tools/check_bundle.py --dist dist/testpilot-sidecar
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Windows CI / 英文系统的默认控制台可能是 cp1252。这个工具会输出中文诊断，
# 不显式切到 UTF-8 会在“产物已经打好”之后因为 print() 编码失败而误判打包失败。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

# 必进包的模块（= packaging/sidecar.spec 的 _REQUIRED）：动态导入，静态分析看不到，
# 漏一个就是"某个驱动/某个 RPC 方法在安装版里必炸"。
REQUIRED_MODULES = [
    "websockets", "websockets.legacy", "websockets.legacy.server",
    "websockets.asyncio", "websockets.asyncio.server",
    "pywinauto", "pywinauto.uia_element_info", "pywinauto.controls.uiawrapper",
    "comtypes", "comtypes.gen",
    "mss", "cv2", "numpy", "pyperclip",
    "win32api", "win32con", "win32gui",
]

# 工程自身模块：漏了说明入口/包发现有问题。
# 注意：驱动三个模块是**动态加载**的（DriverManifest.entry 是字符串，走
# importlib.import_module），静态分析看不见 → 必须显式列在这里。
# config.service / infra.imaging 是函数内惰性导入（rpc.py / verifier.py），
# 静态分析能找到，但仍然列出——它们是本阶段新加的运行时模块，
# 一旦哪天改成真动态导入，这里能拓住。
INTERNAL_MODULES = [
    "testpilot.app.cli", "testpilot.rpc", "testpilot.drivers.uia_driver",
    "testpilot.drivers.vision_driver", "testpilot.drivers.emulator_adapter",
    "testpilot.engine.orchestrator", "testpilot.storage.db",
    "testpilot.config.service", "testpilot.infra.imaging",
]

# 可选模块：装了才要求进包（打包机没装就不该报错）
OPTIONAL_MODULES = {"PIL", "PIL.ImageGrab"}

# C 扩展模块不走 PYZ 归档，而是以 .pyd / 包目录落在产物里 —— 必须另走文件系统核对。
# 只查 PYZ 会误报「cv2 缺失」，而 cv2 恰恰是最容易漏掉的那个（视觉驱动全靠它）。
EXTENSION_PROBES = {
    "cv2": ["cv2/cv2*.pyd", "cv2.pyd", "cv2/__init__.pyc"],
    "win32api": ["win32/win32api*.pyd", "win32api*.pyd"],
    "win32gui": ["win32/win32gui*.pyd", "win32gui*.pyd"],
    "win32con": ["win32/win32con*.pyd", "win32con.pyc", "win32con.py"],
    "numpy": ["numpy/__init__.pyc", "numpy/_core/_multiarray_umath*.pyd"],
}


def _probe_extension(dist: Path, mod: str) -> str | None:
    """在产物里找扩展模块的实体文件。

    onedir 布局是 ``dist/testpilot-sidecar/{exe, _internal/}``，依赖全在 ``_internal``
    下（PyInstaller 6+），所以两个根都要试 —— 只查 dist 根会误报「cv2 缺失」，
    而 cv2 恰恰是视觉驱动的命脉。
    """
    for root in (dist / "_internal", dist):
        if not root.is_dir():
            continue
        for pattern in EXTENSION_PROBES.get(mod, []):
            hits = sorted(root.glob(pattern))
            if hits:
                return hits[0].relative_to(dist).as_posix()
    return None


# 运行期按 ``Path(__file__).parent / <名>`` 读的非 Python 资源（= spec 的 _DATAS）。
# 漏掉的现象是安装版一启动就 FileNotFoundError，开发态永远复现不出来。
REQUIRED_DATA = {
    "testpilot/config/default.yaml",
}


def _check_datas(dist: Path, problems: list[str]) -> None:
    """核对非 Python 资源是否落进产物（_DATAS 只查 PYZ 是查不到的）。"""
    for rel in sorted(REQUIRED_DATA):
        for root in (dist / "_internal", dist):
            if (root / rel).is_file():
                print(f"  [OK]   {rel}（资源）")
                break
        else:
            print(f"  [MISS] {rel}（资源）")
            problems.append(rel)


def _load_toc(path: Path) -> set[str]:
    """读 PyInstaller 的 *.toc 取模块名集合。

    格式为 ``('归档路径', [(名字, 源路径, 类型), ...])`` —— 是**整体一个字面量元组**，
    所以要从第一个 ``(`` 起切（只切 ``[`` 会漏掉左括号，`literal_eval` 报
    `unmatched ')'`）。第三个字段是类型码，`PYMODULE`/`EXTENSION` 都算命中。
    """
    text = path.read_text(encoding="utf-8", errors="replace").lstrip()
    data = ast.literal_eval(text[text.index("("):])
    entries = data[1] if isinstance(data, tuple) and len(data) > 1 else data
    return {entry[0] for entry in entries if isinstance(entry, (tuple, list)) and entry}


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 PyInstaller 产物完整性")
    ap.add_argument("--dist", default="dist/testpilot-sidecar")
    ap.add_argument("--workpath", default="build/pyinstaller/sidecar")
    args = ap.parse_args()

    dist = ROOT / args.dist
    work = ROOT / args.workpath
    problems: list[str] = []

    if not (dist / "testpilot-sidecar.exe").exists():
        print(f"[FAIL] 未找到产物 {dist / 'testpilot-sidecar.exe'}")
        return 1

    pyz = work / "PYZ-00.pyz"
    toc = work / "PYZ-00.toc"
    modules: set[str] = set()
    if toc.exists():
        modules = _load_toc(toc)
    elif pyz.exists():
        import zipfile
        with zipfile.ZipFile(pyz) as zf:
            modules = {n[:-4].replace("/", ".") for n in zf.namelist() if n.endswith(".pyc")}
    else:
        print(f"[FAIL] 既无 {toc.name} 也无 {pyz.name}，无法核对模块")
        return 1
    print(f"PYZ 归档模块数：{len(modules)}")

    # 已安装的可选依赖才算"必进包"
    try:
        import PIL  # noqa: F401
        installed_pil = True
    except ImportError:
        installed_pil = False

    expected = list(REQUIRED_MODULES) + list(INTERNAL_MODULES)
    if installed_pil:
        expected += sorted(OPTIONAL_MODULES)

    for mod in expected:
        if mod in modules:
            print(f"  [OK]   {mod}")
            continue
        landed = _probe_extension(dist, mod)
        if landed:
            print(f"  [OK]   {mod}（扩展形态：{landed}）")
            continue
        print(f"  [MISS] {mod}")
        problems.append(mod)

    for mod in sorted(OPTIONAL_MODULES):
        if not installed_pil and mod not in modules:
            print(f"  [SKIP] {mod}（打包机未安装该依赖，不要求进包）")

    _check_datas(dist, problems)

    if problems:
        print(f"\n缺失 {len(problems)} 个模块：{problems}")
        print("修法：把模块名加进 packaging/sidecar.spec 的 _REQUIRED 与 "
              "scripts/build-sidecar.ps1 的 $hidden，再重跑 build-sidecar.ps1 -Clean")
        return 1
    print(f"\n产物完整：{len(expected)} 个模块 + {len(REQUIRED_DATA)} 个资源全部命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())

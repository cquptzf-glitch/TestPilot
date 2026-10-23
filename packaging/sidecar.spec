# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格 —— TestPilot 引擎 sidecar（**onedir**，默认形态）。

用法（仓库根，激活 venv）：
    pyinstaller packaging/sidecar.spec --noconfirm --distpath dist --workpath build/pyinstaller

产物：``dist/testpilot-sidecar/testpilot-sidecar.exe`` + ``_internal/``（依赖）。
随后由 ``scripts/build-sidecar.ps1`` 复制到
``testpilot/app/desktop/src-tauri/binaries/sidecar/``，由 Tauri 作为 resources 打进安装包。

为什么 onedir 而不是 onefile：
- 运行时依赖很重（opencv / numpy / pywin32 / pywinauto），onefile 每次启动都要解包
  数十 MB 到临时目录，冷启动 3~5s 起步；
- onedir 便于排查（缺哪个 DLL 一眼可见）。快速联调用
  ``scripts/build-sidecar.ps1 -Onefile``。

**隐式依赖清单为什么内联在代码里**：本机安全软件会把部分文本文件落盘加密，
非白名单进程（含某些场景的 PyInstaller）读到的是密文乱码 —— 清单读坏会让打包
**静默丢掉隐式依赖**（现象是"开发能跑、打包跑不起来"）。因此：
``_REQUIRED`` 是权威来源；``packaging/hiddenimports.txt`` 仅作人读文档，
读取结果必须通过模块名正则校验，不达标就退回内联清单。

``scripts/build-sidecar.ps1`` 的 -Onefile 模式内嵌了同一份清单，
由 ``scripts/preflight.ps1`` 做一致性校验（两处漂移直接报错）。
"""
import re
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent          # 仓库根（spec 在 packaging/ 下）

# 运行期按 ``Path(__file__).parent / <名>`` 读取的**非 Python 资源**。
# 必须显式带上：PyInstaller 只收集 .py/.pyd/DLL，YAML 一律不管。
# 漏掉 default.yaml 的现象是安装版一启动就 FileNotFoundError（引擎连配置都读不到），
# 而开发态永远复现不出来 —— 已验证并写成门禁 test_sidecar_spec_ships_runtime_yaml。
_DATAS = [
    (str(ROOT / 'testpilot' / 'config' / 'default.yaml'), 'testpilot/config'),
    # Pi 只加载 TestPilot 自定义 RPC proxy 工具；安装版若漏此文件，Agent 直接不可用。
    (str(ROOT / 'testpilot' / 'agent' / 'pi_rpc_tools.mjs'), 'testpilot/agent'),
]

# 权威隐式依赖清单（运行时动态导入，静态分析看不到）
_REQUIRED = [
    # RPC 服务（壳形态必需）
    'websockets',
    'websockets.legacy',
    'websockets.legacy.server',
    'websockets.asyncio',
    'websockets.asyncio.server',
    # UIA 驱动（comtypes 动态生成 COM 包装）
    'pywinauto',
    'pywinauto.uia_element_info',
    'pywinauto.controls.uiawrapper',
    'comtypes',
    'comtypes.gen',
    # 视觉 / 输入 / 平台
    'mss',
    'cv2',
    'numpy',
    'PIL',
    'PIL.ImageGrab',
    'pyperclip',
    'win32api',
    'win32con',
    'win32gui',
]

_MODULE_RE = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$')


def _hidden_imports() -> list[str]:
    """读 hiddenimports.txt；**内容不健康时退回内联清单**（防 DLP 加密乱码）。"""
    listing = ROOT / 'packaging' / 'hiddenimports.txt'
    if listing.exists():
        try:
            text = listing.read_text(encoding='utf-8')
            candidates = [line.split('#', 1)[0].strip()
                          for line in text.splitlines()]
            good = [c for c in candidates if c and _MODULE_RE.match(c)]
            if len(good) >= len(_REQUIRED):
                return good
            print(f'[sidecar.spec] hiddenimports.txt 只解析出 {len(good)} 个合法模块名'
                  f'（期望 >= {len(_REQUIRED)}），退回内联清单', file=sys.stderr)
        except Exception as exc:                # 编码异常/权限问题都不该阻断打包
            print(f'[sidecar.spec] hiddenimports.txt 读取失败({exc})，退回内联清单',
                  file=sys.stderr)
    return list(_REQUIRED)


a = Analysis(
    [str(ROOT / 'packaging' / 'run_sidecar.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_DATAS,
    hiddenimports=_hidden_imports(),
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PySide6', 'IPython'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='testpilot-sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 壳按行读取 stdout/stderr 进日志抽屉，不需要窗口
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='testpilot-sidecar',
)

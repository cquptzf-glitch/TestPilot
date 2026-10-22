"""OpenCV 装载入口 —— 引导文件不可读时降级为直接装载原生扩展。

**为什么需要这个模块**：``import cv2`` 实际分两步：
    1. ``cv2/__init__.py``（纯 Python 引导）先设置 DLL 搜索路径；
    2. 再由它导入真正干活的二进制 ``cv2.pyd``。

第 1 步依赖一个**文本文件可读**。企业环境里这个前提并不总成立 —— 实测本机
DLP 会让非白名单进程读到密文（报 `source code string cannot contain null bytes`），
装包中断、AV 误隔离也会有类似效果。此时**能用的二进制就在旁边**，却因为一个
文本文件读不了而让「视觉定位 / 视觉断言」整条能力线消失，且报错信息（null bytes）
完全指不到根因。

因此这里提供一条降级链：``load_cv2()`` 先常规导入，失败则在 ``cv2/`` 目录里
直接按路径装载 ``cv2.pyd``（跳过引导文件）。两条都失败才抛 ``ImportError``，
调用方按「视觉不可用」处理 —— 符合设计 1.3：单点异常不得打断执行主干。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("testpilot.opencv")

_cached: object | None = None
_cached_error: Exception | None = None


def _clear_broken_cv2() -> None:
    """清掉半途失败的 cv2 模块缓存，否则降级装载会被旧的空壳挡住。"""
    for name in [n for n in list(sys.modules) if n == "cv2" or n.startswith("cv2.")]:
        sys.modules.pop(name, None)


def _candidate_pyds() -> list[Path]:
    """候选 ``cv2.pyd`` 路径：先问 import 系统要包目录，再扫 sys.path。"""
    found: list[Path] = []

    try:
        spec = importlib.util.find_spec("cv2")
    except (ImportError, ValueError, AttributeError):
        spec = None
    if spec and spec.origin and spec.origin not in ("built-in", "frozen"):
        origin = Path(spec.origin)
        base = origin if origin.is_dir() else origin.parent
        found.append(base / "cv2.pyd")
    if spec and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            found.append(Path(loc) / "cv2.pyd")

    for entry in sys.path:
        if not entry:
            continue
        # sys.path 里可能有相对项（含空串之外的），必须转绝对：
        # 相对路径会随 cwd 变化，装载到不该装载的文件。
        base = Path(entry)
        if not base.is_absolute():
            try:
                base = base.resolve()
            except OSError:
                continue
        found.append(base / "cv2" / "cv2.pyd")
        found.append(base / "cv2.pyd")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        if not path.is_absolute():
            continue
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _load_pyd_directly(pyd: Path):
    """按路径装载扩展模块（跳过 Python 引导文件）。"""
    pyd_dir = str(pyd.parent)
    # 引导文件本来负责把包目录加进 DLL 搜索路径；这里等价补上，
    # 否则 cv2.pyd 依赖同目录 / 上级 x64 目录里的 DLL 时会 126/127 报错。
    handles = []
    if hasattr(os, "add_dll_directory"):
        for dll_dir in (pyd.parent / "..", pyd_dir):
            try:
                resolved = Path(dll_dir).resolve()
            except OSError:
                continue
            if resolved.is_dir():
                try:
                    handles.append(os.add_dll_directory(str(resolved)))
                except OSError as e:
                    log.warning(f"add_dll_directory({resolved}) 失败: {e}")
    # 句柄需保持存活到扩展加载完成；提前释放会撤掉搜索路径
    _ = handles

    _clear_broken_cv2()
    loader = importlib.machinery.ExtensionFileLoader("cv2", str(pyd))
    spec = importlib.util.spec_from_loader("cv2", loader)
    if spec is None:
        raise ImportError(f"无法为 {pyd} 构造 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cv2"] = module          # 扩展模块在 exec 前必须已注册
    loader.exec_module(module)
    return module


def load_cv2():
    """返回可用的 cv2 模块；常规导入失败时降级为直接装载 ``cv2.pyd``。

    结果（成功或失败）都会缓存，避免每个步骤重复走一遍失败路径与日志。
    """
    global _cached, _cached_error

    if _cached is not None:
        return _cached
    if _cached_error is not None:
        raise _cached_error

    try:
        import cv2
        _cached = cv2
        return cv2
    except Exception as exc:            # DLP 密文 / 缺 DLL / 引导文件损坏
        first_error = exc

    for pyd in _candidate_pyds():
        if not pyd.exists():
            continue
        try:
            module = _load_pyd_directly(pyd)
        except Exception as exc:
            log.warning(f"直接装载 {pyd} 失败: {exc}")
            continue
        log.warning(
            f"cv2 引导文件不可用（{type(first_error).__name__}: {first_error}），"
            f"已降级为直接装载原生扩展 {pyd}；"
            f"版本 {getattr(module, '__version__', '?')}")
        _cached = module
        return module

    _cached_error = ImportError(
        f"OpenCV 不可用：常规导入失败（{type(first_error).__name__}: {first_error}），"
        f"降级装载也未找到可用的 cv2.pyd")
    raise _cached_error


def reset_cache() -> None:
    """清空缓存（单测用：模拟"先坏后好"或反复探测）。"""
    global _cached, _cached_error
    _cached = None
    _cached_error = None

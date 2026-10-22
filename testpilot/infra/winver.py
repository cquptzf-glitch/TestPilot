"""Windows 文件版本读取 — 被测程序版本探测（设计 M2-5，归因可比性字段）。

只依赖 ``pywin32``（惰性导入）；任何失败都返回 ``None`` 而不是抛异常 ——
版本探测属于"归因元数据"，绝不能因为它让回归跑不起来。
"""
import logging

log = logging.getLogger("testpilot.winver")


def file_version(path: str) -> str | None:
    """读取 exe/dll 的资源版本号，形如 ``1.2.3.4``；不可用时返回 None。"""
    if not path:
        return None
    try:
        import win32api
    except Exception as e:          # 非 Windows / 未装 pywin32
        log.debug(f"版本探测不可用（pywin32 缺失）: {e}")
        return None
    try:
        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    except Exception as e:          # 文件不存在 / 无版本资源
        log.debug(f"版本探测失败: {path} {e}")
        return None
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"

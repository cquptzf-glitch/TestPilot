"""被测程序版本探测单测（M2-5，设计 env_snapshot 归因可比性字段）。"""
import sys
import types

from testpilot.infra.winver import file_version


class FakeWin32Api:
    def __init__(self, info):
        self.info = info
        self.calls: list[tuple] = []

    def GetFileVersionInfo(self, path, what):
        self.calls.append((path, what))
        if isinstance(self.info, Exception):
            raise self.info
        return self.info


def _install(monkeypatch, info):
    api = FakeWin32Api(info)
    module = types.ModuleType("win32api")
    module.GetFileVersionInfo = api.GetFileVersionInfo
    monkeypatch.setitem(sys.modules, "win32api", module)
    return api


def test_reads_major_minor_build_revision(monkeypatch):
    api = _install(monkeypatch, {"FileVersionMS": (1 << 16) | 2,
                                 "FileVersionLS": (3 << 16) | 4})
    assert file_version(r"C:\app\target.exe") == "1.2.3.4"
    assert api.calls == [(r"C:\app\target.exe", "\\")]


def test_high_bit_values_are_unsigned(monkeypatch):
    _install(monkeypatch, {"FileVersionMS": 0xFFFF0000,
                           "FileVersionLS": 0x0001FFFF})
    assert file_version("a.exe") == "65535.0.1.65535"


def test_missing_pywin32_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32api", None)      # import 抛 ImportError
    assert file_version("a.exe") is None


def test_no_version_resource_returns_none(monkeypatch):
    _install(monkeypatch, OSError("no version info"))
    assert file_version("a.exe") is None


def test_missing_key_returns_none(monkeypatch):
    _install(monkeypatch, {"FileVersionMS": 1})             # 缺 FileVersionLS
    assert file_version("a.exe") is None


def test_empty_path_returns_none(monkeypatch):
    api = _install(monkeypatch, {"FileVersionMS": 0, "FileVersionLS": 0})
    assert file_version("") is None
    assert api.calls == []                                  # 未调用系统接口

"""驱动注册表单测（设计 2.4 / 7.3）。"""
import pytest

from testpilot.core.errors import TestPilotError
from testpilot.core.registry import DriverManifest, DriverRegistry


class _FakeDriver:
    manifest = None

    def __init__(self, cfg):
        self.cfg = cfg


def _manifest(name="fake", priority=10, kind="interaction", entry=""):
    return DriverManifest(kind=kind, name=name, version="1.0",
                          capabilities=frozenset({"click"}),
                          entry=entry or "tests.test_registry:fake_factory",
                          priority=priority)


def fake_factory(cfg):
    return _FakeDriver(cfg)


def test_register_and_duplicate_rejected():
    reg = DriverRegistry()
    reg.register(_manifest())
    with pytest.raises(TestPilotError):
        reg.register(_manifest())


def test_bind_factory_and_create():
    reg = DriverRegistry()
    reg.register(_manifest())
    assert reg.has_factory("fake") is False
    reg.bind_factory("fake", fake_factory)
    assert reg.has_factory("fake") is True
    drv = reg.create("fake", {"backend": "uia"})
    assert drv.cfg == {"backend": "uia"}
    assert reg.names(kind="interaction") == ["fake"]
    assert "click" in reg.capabilities("fake")


def test_create_lazy_import_from_entry():
    """未绑定工厂时按 manifest.entry 懒加载。"""
    reg = DriverRegistry()
    reg.register(_manifest(entry="tests.test_registry:fake_factory"))
    drv = reg.create("fake", {"x": 1})
    assert drv.cfg == {"x": 1}


def test_create_unknown_driver_raises():
    reg = DriverRegistry()
    with pytest.raises(TestPilotError):
        reg.create("nope", {})


def test_names_sorted_by_priority_and_filtered_by_kind():
    reg = DriverRegistry()
    reg.register(_manifest("b", priority=20))
    reg.register(_manifest("a", priority=10))
    reg.register(_manifest("emu", priority=1, kind="device"))
    assert reg.names() == ["emu", "a", "b"]
    assert reg.names(kind="interaction") == ["a", "b"]
    assert reg.names(kind="device") == ["emu"]


def test_capabilities_returns_frozenset():
    reg = DriverRegistry()
    reg.register(_manifest())
    caps = reg.capabilities("fake")
    assert isinstance(caps, frozenset)

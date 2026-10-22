"""驱动一致性门禁（DriverConformanceTest，设计 2.4 / 计划 M2 门禁）。

门禁内容（任何新驱动注册前必须通过）：
1. manifest 字段完整：kind/name/version/capabilities/entry/config_schema；
2. ``entry`` 指向的工厂函数真实存在且可调用；
3. manifest.capabilities 声明的能力在驱动类上都有对应方法；
4. ``Driver`` 协议要求的全部方法都存在；
5. 能力语义自洽：声明不支持的能力被调用时必须抛 ``ActionError``，
   而不是静默成功或抛 AttributeError（设计 5.1 语义约定）。

注意：本测试**不实例化 GUI 驱动**（不需要 pywinauto 运行时），
只做声明与实际实现的一致性校验——这正是驱动插拔式注册表的信任基础。
"""
import importlib
import inspect

import pytest

from testpilot.core.contracts import EmulatorPort
from testpilot.core.errors import ActionError
from testpilot.core.registry import DriverRegistry
from testpilot.drivers.emulator_adapter import MANIFEST as EMU_MANIFEST
from testpilot.drivers.emulator_adapter import EmulatorAdapter
from testpilot.drivers.uia_driver import MANIFEST as UIA_MANIFEST
from testpilot.drivers.uia_driver import UIADriver
from testpilot.drivers.vision_driver import MANIFEST as VISION_MANIFEST
from testpilot.drivers.vision_driver import VisionDriver

INTERACTION_MANIFESTS = (UIA_MANIFEST, VISION_MANIFEST)
ALL_MANIFESTS = (UIA_MANIFEST, VISION_MANIFEST, EMU_MANIFEST)

# Driver 协议要求实现的方法（设计 5.1）
DRIVER_PROTOCOL_METHODS = (
    "health", "locate", "click", "input_text", "select", "grab",
    "wait_ready", "wait_gone",
)


@pytest.mark.parametrize("manifest", ALL_MANIFESTS, ids=lambda m: m.name)
def test_manifest_fields_are_complete(manifest):
    assert manifest.kind in ("interaction", "device", "external")
    assert manifest.name and manifest.name.islower()
    assert manifest.version
    assert isinstance(manifest.capabilities, frozenset) and manifest.capabilities
    assert manifest.config_schema.get("type") == "object"
    assert ":" in manifest.entry, f"{manifest.name} 的 entry 必须形如 'module:func'"
    assert isinstance(manifest.priority, int)


@pytest.mark.parametrize("manifest", ALL_MANIFESTS, ids=lambda m: m.name)
def test_manifest_entry_is_importable_callable(manifest):
    module_path, fn_name = manifest.entry.rsplit(":", 1)
    module = importlib.import_module(module_path)
    factory = getattr(module, fn_name, None)
    assert callable(factory), f"{manifest.entry} 不可调用"
    # 工厂至少要接受一个位置参数（cfg）
    params = list(inspect.signature(factory).parameters.values())
    assert params, "工厂必须接受 cfg 参数"


@pytest.mark.parametrize("manifest", INTERACTION_MANIFESTS, ids=lambda m: m.name)
def test_capabilities_have_backing_methods(manifest):
    cls = {"uia": UIADriver, "vision": VisionDriver}[manifest.name]
    for capability in manifest.capabilities:
        method_name = {
            "input": "input_text",
            "locate_template": "locate",
            "locate_visual": "locate",
        }.get(capability, capability)
        assert callable(getattr(cls, method_name, None)), \
            f"{manifest.name} 声明了能力 {capability} 但缺少方法 {method_name}"


def test_device_manifest_capabilities_have_backing_methods():
    """设备类驱动的能力同样必须有实现方法（否则 manifest 名不副实）。"""
    for capability in EMU_MANIFEST.capabilities:
        assert callable(getattr(EmulatorAdapter, capability, None)), \
            f"emulator 声明了能力 {capability} 但缺少同名方法"


def test_build_registry_imports_every_builtin_manifest():
    """构建注册表是 CLI/serve 的第一动作：任一 manifest 缺失都必须在此暴露。"""
    from testpilot.app.cli import build_registry

    reg = build_registry()
    assert reg.names() == ["uia", "vision", "emulator"]      # 按 priority 升序
    for manifest in ALL_MANIFESTS:
        assert manifest.name in reg.names()


@pytest.mark.parametrize("manifest", INTERACTION_MANIFESTS, ids=lambda m: m.name)
def test_driver_protocol_methods_present(manifest):
    cls = {"uia": UIADriver, "vision": VisionDriver}[manifest.name]
    for name in DRIVER_PROTOCOL_METHODS:
        assert callable(getattr(cls, name, None)), \
            f"{manifest.name} 缺少 Driver 协议方法: {name}"


def test_manifests_never_declare_unsupported_methods():
    """声明不支持的能力必须落在 capabilities 之外（否则门禁名不副实）。"""
    assert "select" not in VISION_MANIFEST.capabilities
    assert "wait_gone" not in VISION_MANIFEST.capabilities
    assert "launch" not in VISION_MANIFEST.capabilities
    assert "launch" in UIA_MANIFEST.capabilities
    assert "close" in UIA_MANIFEST.capabilities


def test_vision_unsupported_operations_raise_action_error(tmp_path):
    """视觉驱动被要求做 UIA 专属操作时必须抛 ActionError（可解释的 DSL 修正提示）。"""
    driver = VisionDriver({"template_dir": str(tmp_path)}, llm=None)
    from testpilot.core.contracts import ExecContext, LocatedElement, DriverKind

    el = LocatedElement(driver=DriverKind.TEMPLATE, handle=(0, 0, 10, 10),
                        rect=(0, 0, 10, 10))
    ctx = ExecContext(run_id=1, case_exec_id=1)
    for call in (lambda: driver.select(el, "A", ctx),
                 lambda: driver.wait_gone(el, ctx, 100),
                 lambda: driver.read_text(el)):
        with pytest.raises(ActionError):
            call()
    assert driver.wait_ready(ctx, 100) is True       # 视觉驱动无就绪概念 -> 视为就绪


def test_emulator_adapter_satisfies_port():
    for name in ("call", "load_scene", "query_state", "history"):
        assert callable(getattr(EmulatorAdapter, name, None))
    for name in ("connected", "capabilities", "protocol_version", "close"):
        assert hasattr(EmulatorAdapter, name)
    assert isinstance(EmulatorAdapter({"endpoint": "ws://127.0.0.1:9300"}),
                      EmulatorPort)


def test_real_drivers_satisfy_driver_protocol_structurally():
    """类层面的协议形状校验（不实例化，避免引入 GUI 运行时依赖）。"""
    for cls in (UIADriver, VisionDriver):
        for name in DRIVER_PROTOCOL_METHODS:
            assert name in vars(cls) or hasattr(cls, name)
        assert hasattr(cls, "manifest")


def test_registry_registers_all_builtin_manifests():
    reg = DriverRegistry()
    for manifest in ALL_MANIFESTS:
        reg.register(manifest)
    assert reg.names(kind="interaction") == ["uia", "vision"]   # 按 priority 升序
    assert reg.names(kind="device") == ["emulator"]
    assert "click" in reg.capabilities("uia")
    assert "grab" in reg.capabilities("vision")


def test_uia_manifest_declares_launch_and_close():
    assert {"launch", "close"} <= set(UIA_MANIFEST.capabilities)
    assert callable(UIADriver.launch) and callable(UIADriver.close)


def test_stub_driver_from_tests_satisfies_protocol():
    """测试替身本身也要满足协议 —— 否则单测覆盖的是"假协议"。

    注：``Driver`` 含数据成员（``manifest``），Python 3.12+ 的
    ``isinstance(x, Driver)`` 会直接抛 TypeError，因此这里做结构化校验。
    """
    from tests.conftest import FakeDriver

    driver = FakeDriver()
    for name in DRIVER_PROTOCOL_METHODS:
        assert callable(getattr(driver, name, None)), f"替身缺少 {name}"
    assert hasattr(driver, "manifest")

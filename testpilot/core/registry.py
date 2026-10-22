"""驱动注册表 — manifest 自描述 + 工厂创建，扩驱动不改引擎（设计文档 7.3）。"""
import importlib
from typing import Callable

from testpilot.core.contracts import DriverManifest
from testpilot.core.errors import TestPilotError

__all__ = ["DriverManifest", "DriverRegistry"]


class DriverRegistry:
    def __init__(self) -> None:
        self._manifests: dict[str, DriverManifest] = {}
        self._factories: dict[str, Callable[[dict], object]] = {}

    def register(self, manifest: DriverManifest) -> None:
        if manifest.name in self._manifests:
            raise TestPilotError(f"驱动已注册: {manifest.name}")
        self._manifests[manifest.name] = manifest

    def bind_factory(self, name: str, factory: Callable[[dict], object]) -> None:
        """绑定已加载的工厂；未绑定时按 manifest.entry 懒加载。"""
        self._factories[name] = factory

    def has_factory(self, name: str) -> bool:
        return name in self._factories

    def create(self, name: str, cfg: dict):
        if name not in self._manifests:
            raise TestPilotError(f"未知驱动: {name}")
        factory = self._factories.get(name)
        if factory is None:
            entry = self._manifests[name].entry or ""
            mod_path, sep, fn = entry.rpartition(":")
            if not sep or not mod_path or not fn:
                raise TestPilotError(
                    f"驱动 {name} 的 entry 非法（应为 '模块:工厂'）: {entry!r}")
            factory = getattr(importlib.import_module(mod_path), fn)
            self._factories[name] = factory
        return factory(cfg)

    def names(self, kind: str | None = None) -> list[str]:
        ms = [m for m in self._manifests.values() if kind is None or m.kind == kind]
        return [m.name for m in sorted(ms, key=lambda m: m.priority)]

    def capabilities(self, name: str) -> frozenset[str]:
        return self._manifests[name].capabilities

"""Plugin manager for Bub framework using filesystem scanning."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pluggy
from loguru import logger

from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub.tools import REGISTRY


class PluginStatusCode(Enum):
    LOADED = "loaded"
    REMOVED = "removed"
    FAILED = "failed"


@dataclass(frozen=True)
class PluginStatus:
    ok: bool
    code: PluginStatusCode
    detail: str = ""


@dataclass
class PluginSpec:
    name: str
    entry: str
    path: Path
    version: str = ""
    description: str = ""
    permanent: bool = False


@dataclass
class PluginState:
    spec: PluginSpec
    instance: object
    tools: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)


class PluginManager:
    """Manages Bub plugins discovered from filesystem directories."""

    def __init__(self, framework: object, plugin_dirs: list[Path]) -> None:
        self.framework = framework
        self.plugin_dirs = plugin_dirs
        self._pluggy_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._pluggy_manager.add_hookspecs(BubHookSpecs)
        self._plugins: dict[str, PluginState] = {}
        self._status: dict[str, PluginStatus] = {}

    @property
    def pluggy_manager(self) -> pluggy.PluginManager:
        return self._pluggy_manager

    def scan(self) -> list[PluginSpec]:
        """Scan plugin_dirs for subdirectories containing bub.toml."""
        specs: list[PluginSpec] = []
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.is_dir():
                continue
            for subdir in plugin_dir.iterdir():
                if not subdir.is_dir():
                    continue
                manifest = subdir / "bub.toml"
                if not manifest.is_file():
                    continue
                try:
                    import tomllib

                    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
                except Exception:  # noqa: S112
                    continue
                if not isinstance(data, dict):
                    continue
                plugin_data = data.get("plugin", {})
                if not isinstance(plugin_data, dict):
                    continue
                name = plugin_data.get("name", subdir.name)
                entry = plugin_data.get("entry", "")
                if not entry:
                    continue
                specs.append(
                    PluginSpec(
                        name=name,
                        entry=entry,
                        path=subdir,
                        version=plugin_data.get("version", ""),
                        description=plugin_data.get("description", ""),
                    )
                )
        return specs

    def _load(self, spec: PluginSpec) -> PluginState:
        """Load a single plugin from spec."""
        module_name, _, attr = spec.entry.partition(":")
        if not module_name or not attr:
            raise ValueError(f"Invalid entry '{spec.entry}' for plugin '{spec.name}'")

        plugin_path = spec.path
        if str(plugin_path) not in sys.path:
            sys.path.insert(0, str(plugin_path))

        pre_registry = set(REGISTRY.keys())
        pre_modules = set(sys.modules.keys())

        module = importlib.import_module(module_name)
        plugin_cls = getattr(module, attr)
        instance = plugin_cls(self.framework) if callable(plugin_cls) else plugin_cls

        self._pluggy_manager.register(instance, name=spec.name)

        post_registry = set(REGISTRY.keys())
        post_modules = set(sys.modules.keys())

        tools = post_registry - pre_registry
        modules = post_modules - pre_modules

        state = PluginState(spec=spec, instance=instance, tools=tools, modules=modules)
        self._plugins[spec.name] = state
        self._status[spec.name] = PluginStatus(ok=True, code=PluginStatusCode.LOADED)
        return state

    def _unload(self, name: str) -> PluginState:
        """Unload a plugin by name. Returns the old PluginState."""
        import shutil

        state = self._plugins.get(name)
        if state is None:
            raise KeyError(f"Plugin '{name}' not found")
        if state.spec.permanent:
            raise RuntimeError(f"Cannot unload permanent plugin '{name}'")

        self._pluggy_manager.unregister(name=name)

        for tool_name in state.tools:
            REGISTRY.pop(tool_name, None)

        for mod_name in state.modules:
            sys.modules.pop(mod_name, None)

        cache_dir = state.spec.path / "__pycache__"
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)

        del self._plugins[name]
        return state

    def load_builtin(self) -> PluginStatus:
        """Load the builtin plugin."""
        import bub.builtin

        path = Path(bub.builtin.__file__).parent
        spec = PluginSpec(
            name="builtin",
            entry="bub.builtin.hook_impl:BuiltinImpl",
            path=path,
            permanent=True,
        )
        try:
            state = self._load(spec)
            state.spec.permanent = True
        except Exception as exc:
            self._status["builtin"] = PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail=str(exc))
        return self._status["builtin"]

    def load_all_external(self) -> dict[str, PluginStatus]:
        """Scan and load all external plugins."""
        statuses: dict[str, PluginStatus] = {}
        for spec in self.scan():
            if spec.name in self._plugins:
                continue
            try:
                self._load(spec)
            except Exception as exc:
                self._status[spec.name] = PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail=str(exc))
            statuses[spec.name] = self._status[spec.name]
        return statuses

    def _reload_existing(self, name: str, spec: PluginSpec) -> PluginStatus:
        old_state = self._plugins[name]
        try:
            self._unload(name)
            self._load(spec)
            return PluginStatus(ok=True, code=PluginStatusCode.LOADED)
        except Exception as exc:
            logger.warning(f"Failed to reload plugin '{name}': {exc}; rolling back")
            try:
                self._pluggy_manager.register(old_state.instance, name=name)
                self._plugins[name] = old_state
            except Exception as rollback_exc:
                return PluginStatus(
                    ok=False,
                    code=PluginStatusCode.FAILED,
                    detail=f"{exc}; rollback also failed: {rollback_exc}",
                )
            return PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail=str(exc))

    def reload_all(self) -> dict[str, PluginStatus]:
        """Full reload: carry permanent, detect removed/added/existing, rollback on failure."""
        new_specs = {spec.name: spec for spec in self.scan()}
        old_names = set(self._plugins.keys())
        new_names = set(new_specs.keys())

        removed = old_names - new_names - {"builtin"}
        added = new_names - old_names
        existing = old_names & new_names - {"builtin"}

        new_status: dict[str, PluginStatus] = {}

        # Carry builtin forward
        if "builtin" in self._plugins:
            new_status["builtin"] = self._status.get("builtin", PluginStatus(ok=True, code=PluginStatusCode.LOADED))

        # Unload removed plugins
        for name in removed:
            if name == "builtin":
                continue
            try:
                self._unload(name)
                new_status[name] = PluginStatus(ok=True, code=PluginStatusCode.REMOVED)
            except Exception as exc:
                new_status[name] = PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail=str(exc))

        # Load added plugins
        for name in added:
            try:
                state = self._load(new_specs[name])
                self._plugins[name] = state
                new_status[name] = PluginStatus(ok=True, code=PluginStatusCode.LOADED)
            except Exception as exc:
                new_status[name] = PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail=str(exc))

        # Reload existing plugins
        for name in existing:
            new_status[name] = self._reload_existing(name, new_specs[name])

        self._status = new_status
        return dict(self._status)

    def get_status(self) -> dict[str, PluginStatus]:
        return dict(self._status)

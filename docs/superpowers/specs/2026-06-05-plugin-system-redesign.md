# Plugin System Redesign: Filesystem-Based Hot Reload

## Problem

The current plugin system uses `importlib.metadata.entry_points(group="bub")` for plugin discovery. This mechanism caches results aggressively within a running Python process. When a plugin is uninstalled (folder deleted + `pip uninstall`), the entry_points cache still reports the old plugin, causing `reload_plugins` to give false "2 ok" status. The `importlib.invalidate_caches()` and `site.addsitedir()` calls in the current `reload_hooks()` are insufficient to clear this cache reliably.

Additional issues with the current system:

- `sys.modules` eviction uses the entry point value's module prefix, which may miss modules or evict wrong ones.
- Tool tracking via `REGISTRY` key snapshots is fragile during partial failures.
- The reload logic is deeply coupled to `BubFramework`, making it hard to test and evolve.

## Goals

1. **Correctness**: `reload_plugins` always reflects the true filesystem state.
2. **Simplicity**: No dependency on pip metadata caching for plugin discovery.
3. **Isolation**: Each plugin's modules and tools are tracked independently for clean unload/reload.
4. **Testability**: Plugin lifecycle can be tested with temporary directories.
5. **Uniformity**: Builtin and external plugins share the same lifecycle. One manager, one path.

## Non-Goals

- Redesigning the `@tool` decorator or global `REGISTRY`.
- Changing hook specifications (`hookspecs.py`) or hook execution (`hook_runtime.py`).
- Subprocess isolation for plugins.
- Remote/PyPI plugin distribution.
- Changing `install`/`uninstall` CLI commands (out of scope).

## Design

### Plugin Discovery: Filesystem Scanning

Plugins are discovered by scanning directories for subdirectories containing a `bub.toml` manifest file.

**Scan paths** (in priority order):

1. `{workspace}/.bub/plugins/` — project-level plugins
2. `~/.bub/plugins/` — user-level plugins

Both directories are scanned and all valid plugins found are loaded. Plugin names are unique within a single directory (enforced by the filesystem). The priority order only matters for debugging or documentation purposes; there is no "first match wins" conflict resolution because a plugin name cannot appear twice in the same directory.

### Plugin Manifest: bub.toml

Each plugin directory contains a `bub.toml`:

```toml
[plugin]
name = "echo"
entry = "echo_plugin:EchoPlugin"
version = "0.1.0"
description = "Echo demo plugin"
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique plugin identifier |
| `entry` | Yes | Entry point in `module:attribute` format |
| `version` | No | Plugin version string |
| `description` | No | Human-readable description |

**Entry format**: `module:attribute` where `module` is a Python module name (relative to the plugin directory) and `attribute` is the plugin class/object name within that module. If `attribute` is omitted (e.g., `entry = "echo_plugin"`), the module itself is used as the plugin instance.

### Plugin Directory Structure

```
~/.bub/plugins/
  echo/
    bub.toml
    echo_plugin.py
```

The plugin directory is added to `sys.path` before importing, so the entry module resolves correctly.

### PluginManager Class

A new `PluginManager` class (in `src/bub/plugin_manager.py`) is the **single owner** of all plugin lifecycle — including builtin. It owns the pluggy `PluginManager` instance (previously `BubFramework._plugin_manager`), which is exposed as a property for `HookRuntime` and other consumers.

```python
@dataclass
class PluginSpec:
    name: str
    entry: str
    path: Path
    version: str = ""
    description: str = ""
    permanent: bool = False  # True for builtin

@dataclass
class PluginState:
    spec: PluginSpec
    instance: object
    tools: set[str]       # REGISTRY keys owned by this plugin
    modules: set[str]     # sys.modules keys owned by this plugin

class PluginManager:
    def __init__(self, framework: BubFramework, plugin_dirs: list[Path]):
        self._framework = framework
        self._plugin_dirs = plugin_dirs
        self._pm = pluggy.PluginManager("bub")
        self._pm.add_hookspecs(BubHookSpecs)
        self._plugins: dict[str, PluginState] = {}
        self._status: dict[str, PluginStatus] = {}

    @property
    def pluggy_manager(self) -> pluggy.PluginManager: ...

    def load_builtin(self) -> None
    def load_all_external(self) -> None
    def scan(self) -> list[PluginSpec]
    def reload_all(self) -> dict[str, PluginStatus]
    def get_status(self) -> dict[str, PluginStatus]
```

**Builtin as a plugin**: `load_builtin()` constructs a `PluginSpec` with `permanent=True` and `path` pointing to the bub package's own `builtin/` directory, then calls the same internal `_load()` method used for external plugins. The builtin plugin's tools (registered into `REGISTRY` by `@tool` at import time) and modules are tracked identically. The `permanent` flag prevents `reload_all()` from unloading or reloading it.

### PluginStatus

```python
from enum import Enum
from dataclasses import dataclass

class PluginStatusCode(Enum):
    LOADED = "loaded"
    REMOVED = "removed"
    FAILED = "failed"

@dataclass(frozen=True)
class PluginStatus:
    ok: bool
    code: PluginStatusCode
    detail: str = ""
```

`reload_all()` returns `dict[str, PluginStatus]`. The `reload_plugins` tool formats this into a human-readable report (one line per plugin, with status icon and detail).

### Load Flow

The same `_load(spec)` method is used for both builtin and external plugins:

1. Add the spec's `path` to `sys.path` (if not already present). For external plugins this is the plugin directory; for builtin it is the bub package root.
2. Snapshot `REGISTRY.keys()` and `sys.modules.keys()`.
3. Parse `spec.entry` into `module_name` and `attribute_name`.
4. Import the module via `importlib.import_module(module_name)`.
5. Get the attribute from the module.
6. If callable, instantiate with `instance = cls(framework)`.
7. Register the instance with pluggy: `self._pm.register(instance, name=spec.name)`.
8. Record `REGISTRY.keys() - snapshot` as the plugin's tools.
9. Record `sys.modules.keys() - snapshot` as the plugin's modules.
10. Store `PluginState` in `self._plugins[spec.name]`.

### Unload Flow

Refuses if `spec.permanent` is True.

1. Unregister from pluggy: `self._pm.unregister(name=spec.name)`.
2. Pop all tracked tools from `REGISTRY`.
3. Remove all tracked modules from `sys.modules`.
4. Remove from `self._plugins`.
5. Return the old `PluginState` (for potential rollback).

### Reload Flow

`reload_all()`:

1. Call `scan()` to get the current set of external plugins on disk.
2. Compare with currently loaded non-permanent plugins:
    - **Removed** (loaded but not on disk): unload + clean up. Status: `PluginStatus(ok=True, code=REMOVED, detail="removed")`.
    - **Added** (on disk but not loaded): load. Status: `PluginStatus(ok=True, code=LOADED)` or `PluginStatus(ok=False, code=FAILED, detail="...")`.
    - **Existing** (on disk and currently loaded): always unload old, evict modules, load new. This avoids any change-detection complexity and ensures correctness. On failure: rollback (restore old plugin state). Status: `PluginStatus(ok=True, code=LOADED)` or `PluginStatus(ok=False, code=FAILED, detail="...")`.
3. Permanent plugins (builtin) are never touched — their status is carried forward unchanged.
4. Return `dict[str, PluginStatus]`.

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Plugin directory deleted | Detected by scan. Unload + clean up. Status: `PluginStatus(ok=True, code=REMOVED, detail="removed")`. |
| `bub.toml` missing or malformed | Skip plugin. Status: `PluginStatus(ok=False, code=FAILED, detail="...")`. |
| Module import fails | Rollback: restore old tools to REGISTRY, re-register old plugin with pluggy. Status: `PluginStatus(ok=False, code=FAILED, detail="...")`. |
| Plugin instantiation raises | Same rollback as import failure. |
| Pluggy registration fails | Pop newly added tools from REGISTRY, evict new modules. Status: `PluginStatus(ok=False, code=FAILED, detail="...")`. |

**Load failure handling**: If an external plugin fails to load during `load_all_external()` or `reload_all()`, the error is recorded in `PluginStatus` and the framework continues loading other plugins. The failure is visible in the `reload_plugins` tool output.

### Integration with BubFramework

`BubFramework` delegates all plugin management to `PluginManager`. The pluggy `PluginManager` instance and `HookRuntime` are now created by `PluginManager` and accessed via properties:

```python
class BubFramework:
    def __init__(self, ...):
        self._plugin_mgr = PluginManager(self, plugin_dirs)
        self._hook_runtime = HookRuntime(self._plugin_mgr.pluggy_manager)

    def load_hooks(self):
        self._plugin_mgr.load_builtin()
        self._plugin_mgr.load_all_external()

    def reload_hooks(self):
        return self._plugin_mgr.reload_all()
```

The old `_load_builtin_hooks()`, `_unload_external_plugins()`, `_restore_plugin()`, `_reload_plugin_from_entry_point()`, `_drop_tools()`, `_handle_plugin_*()`, and the module-level `_clear_plugin_modules()` are all removed from `framework.py`. Their logic lives in `PluginManager`.

Builtin tools still register into `REGISTRY` at import time via `@tool` — no change to `builtin/tools.py` or the `@tool` decorator. `PluginManager` tracks them the same way it tracks external plugin tools (REGISTRY key snapshots).

### Integration with HookRuntime

`HookRuntime` currently receives a `pluggy.PluginManager` in its constructor. This doesn't change — `BubFramework` passes `self._plugin_mgr.pluggy_manager` instead of creating its own pluggy manager. `HookRuntime` code is untouched.

### Integration with reload.plugins Tool

The `reload.plugins` tool in `builtin/tools.py` calls `framework.reload_hooks()` as before. The return type (`dict[str, PluginStatus]`) is unchanged, so the tool's formatting logic needs no modification.

### Impact on Existing Code

| File | Change |
|------|--------|
| `src/bub/plugin_manager.py` | **New file**: `PluginSpec`, `PluginState`, `PluginManager` |
| `src/bub/framework.py` | Remove `_load_builtin_hooks`, `load_hooks` plugin loop, `reload_hooks`, all `_plugin_*` helpers, and module-level `_clear_plugin_modules`. Create `PluginManager` in `__init__`, delegate to it. |
| `src/bub/hook_runtime.py` | No change (still receives `pluggy.PluginManager`). |
| `src/bub/tools.py` | No change (`REGISTRY`, `@tool` untouched). |
| `src/bub/builtin/tools.py` | No change (`reload.plugins` calls `framework.reload_hooks()` which delegates). |
| `src/bub/builtin/hook_impl.py` | No change (`BuiltinImpl` class untouched). |
| `src/bub/builtin/cli.py` | No change (`install`/`uninstall` out of scope). |
| `src/bub/__main__.py` | No change (calls `framework.load_hooks()` as before). |
| `tests/test_reload_plugins.py` | Rewrite to use filesystem-based discovery with tmp directories. |

### Test Strategy

All tests use `tmp_path` to create temporary plugin directories.

| Test | What it verifies |
|------|-----------------|
| `test_scan_discovers_plugins` | `scan()` finds plugins with valid bub.toml |
| `test_scan_skips_dirs_without_manifest` | Directories without bub.toml are ignored |
| `test_scan_handles_malformed_manifest` | Bad bub.toml is reported, not crashed |
| `test_load_registers_plugin` | Plugin tools appear in REGISTRY, hooks in pluggy |
| `test_unload_removes_plugin` | Tools removed from REGISTRY, hooks unregistered, modules evicted |
| `test_unload_refuses_permanent` | Builtin plugin cannot be unloaded |
| `test_reload_detects_new_plugin` | New plugin directory is loaded on reload |
| `test_reload_detects_removed_plugin` | Deleted plugin directory triggers clean unload |
| `test_reload_handles_changed_plugin` | Modified plugin is unloaded and reloaded |
| `test_reload_rolls_back_on_failure` | Failed reload restores old plugin state |
| `test_reload_skips_permanent` | Builtin plugin is not touched during reload |
| `test_reload_multiple_directories` | Project and user plugin dirs are both scanned |
| `test_reload_plugins_tool_report` | Tool returns correct formatted output |
| `test_module_eviction_is_scoped` | Only the plugin's modules are evicted, not framework's |
| `test_builtin_tracked_as_plugin` | Builtin appears in `_plugins` and `_status` with `permanent=True` |

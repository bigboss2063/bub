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

## Non-Goals

- Redesigning the `@tool` decorator or global `REGISTRY`.
- Changing hook specifications (`hookspecs.py`) or hook execution (`hook_runtime.py`).
- Subprocess isolation for plugins.
- Remote/PyPI plugin distribution.

## Design

### Plugin Discovery: Filesystem Scanning

Plugins are discovered by scanning directories for subdirectories containing a `bub.toml` manifest file.

**Scan paths** (in priority order, first match wins):

1. `{workspace}/.bub/plugins/` — project-level plugins
2. `~/.bub/plugins/` — user-level plugins

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

A new `PluginManager` class (in `src/bub/plugin_manager.py`) extracts plugin lifecycle management from `BubFramework`. It owns the pluggy `PluginManager` instance (previously `BubFramework._plugin_manager`), which is now exposed as a property for `HookRuntime` and other consumers.

```python
@dataclass
class PluginSpec:
    name: str
    entry: str
    path: Path
    version: str = ""
    description: str = ""

@dataclass
class PluginState:
    spec: PluginSpec
    instance: object
    tools: set[str]
    modules: set[str]

class PluginManager:
    def __init__(self, framework, plugin_dirs):
        ...

    def scan(self) -> list[PluginSpec]
    def load_all(self) -> None
    def reload_all(self) -> dict[str, PluginStatus]
    def get_plugin_status(self) -> dict[str, PluginStatus]
```

### Load Flow

For each plugin:

1. Add plugin directory to `sys.path` (if not already present).
2. Snapshot `REGISTRY.keys()` and `sys.modules.keys()`.
3. Import the entry module via `importlib.import_module()`.
4. Get the entry attribute from the module.
5. If callable, instantiate with `instance = cls(framework)`.
6. Register the instance with pluggy: `plugin_manager.register(instance, name=spec.name)`.
7. Record new `REGISTRY` keys as the plugin's tools.
8. Record new `sys.modules` keys as the plugin's modules.

### Unload Flow

1. Unregister from pluggy: `plugin_manager.unregister(name=spec.name)`.
2. Pop all tracked tools from `REGISTRY`.
3. Remove all tracked modules from `sys.modules`.
4. Return the old `PluginState` (for potential rollback).

### Reload Flow

`reload_all()`:

1. Call `scan()` to get the current set of plugins on disk.
2. Compare with currently loaded plugins:
   - **Removed** (loaded but not on disk): unload + clean up. Status: success with `detail="removed"`.
   - **Added** (on disk but not loaded): load. Status: success or failure.
   - **Existing** (on disk and currently loaded): always unload old, evict modules, load new. This avoids any change-detection complexity and ensures correctness. On failure: rollback (restore old plugin state).
3. Return `dict[str, PluginStatus]`.

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Plugin directory deleted | Detected by scan. Unload + clean up. Status: `"removed"` (success). |
| `bub.toml` missing or malformed | Skip plugin. Status: failure with detail. |
| Module import fails | Rollback: restore old tools to REGISTRY, re-register old plugin with pluggy. Status: failure. |
| Plugin instantiation raises | Same rollback as import failure. |
| Pluggy registration fails | Pop newly added tools from REGISTRY, evict new modules. Status: failure. |

### Integration with BubFramework

`BubFramework` delegates plugin management to `PluginManager`:

```python
class BubFramework:
    def __init__(self, ...):
        self._plugin_mgr = PluginManager(self, plugin_dirs)

    def load_hooks(self):
        self._load_builtin_hooks()
        self._plugin_mgr.load_all()

    def reload_hooks(self):
        return self._plugin_mgr.reload_all()
```

The builtin hooks loading (`_load_builtin_hooks`) remains unchanged. Builtin tools continue to register into `REGISTRY` at import time via `@tool`. `PluginManager` only manages external plugins.

### Integration with reload.plugins Tool

The `reload.plugins` tool in `builtin/tools.py` calls `framework.reload_hooks()` as before. The return type (`dict[str, PluginStatus]`) is unchanged, so the tool's formatting logic needs no modification.

### Impact on Existing Code

| File | Change |
|------|--------|
| `src/bub/plugin_manager.py` | **New file**: `PluginSpec`, `PluginState`, `PluginManager` |
| `src/bub/framework.py` | Remove `load_hooks` plugin loop, `reload_hooks`, and all `_plugin_*` helpers. Delegate to `PluginManager`. |
| `src/bub/builtin/tools.py` | No change to `reload.plugins` tool (it calls `framework.reload_hooks()` which now delegates). |
| `src/bub/builtin/cli.py` | `install`/`uninstall` commands currently use pip. Updating them to manage local plugin directories is out of scope for this redesign. The reload system works regardless of how plugins arrive in the directory. |
| `tests/test_reload_plugins.py` | Rewrite tests to use filesystem-based discovery (tmp directories with bub.toml). |

### Test Strategy

All tests use `tmp_path` to create temporary plugin directories.

| Test | What it verifies |
|------|-----------------|
| `test_scan_discovers_plugins` | `scan()` finds plugins with valid bub.toml |
| `test_scan_skips_dirs_without_manifest` | Directories without bub.toml are ignored |
| `test_scan_handles_malformed_manifest` | Bad bub.toml is reported, not crashed |
| `test_load_registers_plugin` | Plugin tools appear in REGISTRY, hooks in pluggy |
| `test_unload_removes_plugin` | Tools removed from REGISTRY, hooks unregistered, modules evicted |
| `test_reload_detects_new_plugin` | New plugin directory is loaded on reload |
| `test_reload_detects_removed_plugin` | Deleted plugin directory triggers clean unload |
| `test_reload_handles_changed_plugin` | Modified plugin is unloaded and reloaded |
| `test_reload_rolls_back_on_failure` | Failed reload restores old plugin state |
| `test_reload_multiple_directories` | Project and user plugin dirs are both scanned |
| `test_reload_plugins_tool_report` | Tool returns correct formatted output |
| `test_module_eviction_is_scoped` | Only the plugin's modules are evicted, not framework's |

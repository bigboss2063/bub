---
name: plugin-creator
description: "Guide for creating Bub plugins via filesystem discovery (bub.toml) and performing hot-reload. Use when the user wants to write a new plugin, extend Bub with custom hooks or tools, or reload plugins without restarting."
---

# Plugin Creator

Create Bub plugins as filesystem directories with `bub.toml` manifests.

## Concepts

- **Plugin**: a directory containing a `bub.toml` manifest, placed under `.bub/plugins/` (workspace or home).
- **Hook**: a method on your class decorated with `@hookimpl` (from `bub.hookspecs`).
- **Tool**: a function decorated with `bub.tools.tool`; auto-registers into the global `REGISTRY`.
- **Hot-reload**: triggered by the `reload.plugins` tool; unloads plugin modules from `sys.modules`, cleans `__pycache__`, and re-imports. Only external plugins reload; `builtin` never unloads.

## Plugin Directories

Bub scans two directories for plugins:

1. `<workspace>/.bub/plugins/` — project-local plugins
2. `~/.bub/plugins/` — user-global plugins

Each subdirectory with a valid `bub.toml` is discovered as a plugin.

## Plugin Skeleton

```
.bub/plugins/
  my-plugin/
    bub.toml
    my_plugin.py      # entry point target: MyPlugin class
    hooks.py          # optional: hook implementations
    tools.py          # optional: @tool-decorated functions
```

### bub.toml

```toml
[plugin]
name = "my-plugin"
entry = "my_plugin:MyPlugin"
version = "0.1.0"
description = "My custom Bub plugin"
```

- `name`: unique plugin identity for reload and status reporting.
- `entry`: `<module>:<class>` — the module is imported from the plugin directory (auto-added to `sys.path`), and the class is instantiated with the `BubFramework` instance.

### Plugin Class

```python
# .bub/plugins/my-plugin/my_plugin.py
from __future__ import annotations
from typing import Any
from bub.hookspecs import hookimpl
from bub.types import Envelope, State

class MyPlugin:
    """Receives the BubFramework instance on init."""

    def __init__(self, framework: Any) -> None:
        self.framework = framework

    @hookimpl
    def system_prompt(self, prompt: str | list[dict], state: State) -> str | None:
        return "Additional system instructions from my-plugin."
```

Import your tools module inside the plugin class or at module level so they register at load time:

```python
from my_plugin import tools as _tools  # noqa: F401 — side-effect registration
```

## Available Hooks

See `references/hooks-reference.md` for the full list with signatures and semantics.

Quick summary of the most commonly used:

- `system_prompt(prompt, state)` → return a string appended to the system prompt.
- `register_cli_commands(app)` → `app` is a `typer.Typer`; add subcommands.
- `provide_channels(message_handler)` → return a list of `Channel` instances.
- `before_tool_call(tool_name, arguments, session_id)` → return `{"block": True, "reason": "..."}` to block.
- `after_tool_call(tool_name, arguments, result, session_id, is_error)` → return `{"content": ..., "is_error": True, "terminate": True}` to override.
- `load_state(message, session_id)` → return a dict merged into turn state.
- `save_state(session_id, state, message, model_output)` → persist after turn.
- `dispatch_outbound(message)` → send outbound to external channel.
- `onboard_config(current_config)` → return a dict fragment for `bub install`.

Hooks marked `firstresult=True` stop at the first non-None return; others collect all results.

## Registering Tools

```python
# .bub/plugins/my-plugin/tools.py
from bub.tools import tool
from republic import ToolContext

@tool(name="my_plugin.greet")
async def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"

@tool(name="my_plugin.data", context=True)
async def fetch_data(*, context: ToolContext) -> str:
    """Fetch data using session context."""
    session_id = context.state.get("session_id")
    return f"Data for {session_id}"
```

- `name` uses `.` as namespace separator; model-facing names replace `.` with `_`.
- `context=True` injects a `ToolContext` with `.state` (session state dict) and `.tape`.
- Tools auto-register into `REGISTRY` at import time.
- Failed plugin load: `PluginManager` records the failure in status; tools added before the failure are cleaned up.

## Hot-Reload

After editing plugin code:

```
reload.plugins
```

What happens (`PluginManager.reload_all()`):

1. Scan plugin directories for current filesystem state.
2. **Removed** plugins (on disk but not loaded): unload — unregister hooks, remove tools from `REGISTRY`, evict modules from `sys.modules`, clean `__pycache__`.
3. **Added** plugins (on disk but not yet loaded): load — add path to `sys.path`, import module, instantiate class, register with pluggy, record tool/module deltas.
4. **Existing** plugins (changed on disk): unload old → load new. On failure, rollback to the old plugin instance.
5. **Permanent** plugins (e.g. `builtin`): carried forward unchanged.

## Workflow

1. Create plugin directory under `.bub/plugins/my-plugin/`.
2. Add `bub.toml` with `name` and `entry`.
3. Implement hooks with `@hookimpl` and/or tools with `@tool`.
4. Start Bub: `uv run bub chat` — plugin loads on startup via filesystem scan.
5. After code changes, run `reload.plugins` tool — no reinstall or restart needed.
6. Iterate.

## Tips

- Plugin `__init__` receives `BubFramework`; store it for later access.
- Import tools module as a side effect so tools register before the plugin is registered with pluggy.
- Keep plugin stateless or persist via `load_state`/`save_state` hooks — hot-reload creates new instances.
- The builtin plugin (`bub.builtin`) implements all hooks as defaults; your plugin overrides or supplements them.
- Plugin name (in `bub.toml`) must be unique across all discovered plugins.
- The plugin directory is added to `sys.path`, so imports resolve relative to it.

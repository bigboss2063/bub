---
name: plugin-creator
description: "Guide for creating Bub plugins (code extensions via entry_points) and performing hot-reload. Use when the user wants to write a new plugin, extend Bub with custom hooks or tools, or reload plugins without restarting."
---

# Plugin Creator

Create Bub plugins as Python packages with entry_points registration.

## Concepts

- **Plugin**: a Python package declaring `[project.entry-points."bub"]` in its pyproject.toml.
- **Hook**: a method on your class decorated with `@hookimpl` (from `bub.hookspecs`).
- **Tool**: a function decorated with `bub.tools.tool`; auto-registers into the global `REGISTRY`.
- **Hot-reload**: triggered by the `reload.plugins` tool; removes plugin modules from `sys.modules` and re-imports. Only external plugins reload; `builtin` never unloads.

## Plugin Skeleton

```
my-plugin/
  pyproject.toml
  src/
    my_plugin/
      __init__.py      # entry point target: MyPlugin class
      hooks.py         # optional: hook implementations
      tools.py         # optional: @tool-decorated functions
```

### pyproject.toml

```toml
[project]
name = "bub-my-plugin"
version = "0.1.0"
dependencies = ["bub"]   # or the runtime subset you need

[project.entry-points."bub"]
my-plugin = "my_plugin:MyPlugin"
```

The entry point name (`my-plugin`) becomes the plugin identity for reload and status reporting.

### Plugin Class

```python
# src/my_plugin/__init__.py
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

Import your tools module inside `__init__.py` so they register at load time:

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
# src/my_plugin/tools.py
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
- Failed plugin load: framework removes all tools added by that plugin from `REGISTRY`.

## Hot-Reload

After editing plugin code and reinstalling (`pip install -e .` or `uv sync`):

```
reload.plugins
```

What happens (`framework.reload_hooks()`):

1. Unload all non-builtin plugins; unregister hooks, save old plugin objects and tools.
2. For each entry point:
   - `_clear_plugin_modules()`: remove root module + submodules from `sys.modules`.
   - `importlib.invalidate_caches()`.
   - `entry_point.load()`: fresh import.
   - Re-register with plugin manager.
3. On failure: `_restore_plugin()` rolls back to the previous working version.

## Workflow

1. Scaffold plugin package with pyproject.toml entry point.
2. Implement hooks with `@hookimpl` and/or tools with `@tool`.
3. Install in editable mode: `pip install -e .` or `uv sync`.
4. Start Bub: `uv run bub chat` — plugin loads on startup.
5. After code changes, reinstall and run `reload.plugins` tool — no restart needed.
6. Iterate.

## Tips

- Plugin `__init__` receives `BubFramework`; store it for later access.
- Import tools module as a side effect so tools register before the plugin is registered.
- Keep plugin stateless or persist via `load_state`/`save_state` hooks — hot-reload creates new instances.
- The builtin plugin (`bub.builtin`) implements all hooks as defaults; your plugin overrides or supplements them.
- Entry point name must be unique across installed plugins.

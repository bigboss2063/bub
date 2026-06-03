# Tool Lifecycle Hooks (before_tool_call / after_tool_call)

## Problem

Bub has no interception points around individual tool execution. Tools are dispatched
directly from `REGISTRY` through republic's `ToolExecutor` with no way for plugins to
inspect, block, or modify tool calls.

## Design

Add two new pluggy hook specs: `before_tool_call` (pre-execution, can block) and
`after_tool_call` (post-execution, can override result). Hooks fire per individual
tool call, parallel-safe, with no changes to republic.

### Hook Signatures

```python
@hookspec
def before_tool_call(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str,
) -> dict[str, Any] | None:

@hookspec
def after_tool_call(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    session_id: str,
    is_error: bool,
) -> dict[str, Any] | None:
```

- `before_tool_call` returns `{"block": True, "reason": "..."}` to prevent execution.
- `after_tool_call` returns override fields: `{"content": ..., "is_error": ..., "terminate": ...}`.
- Neither uses `firstresult=True`. All registered plugins are notified.

### Implementation

#### Tool Wrapping (`Agent._wrap_tools_with_hooks`)

In `Agent._run_once()`, each `Tool` handler is wrapped with a closure that:

1. Calls `hook_runtime.call_many("before_tool_call", ...)`
2. If any result has `block: True`, raises `RepublicError(ErrorKind.TOOL, reason)`
   → caught by republic and recorded as a tool error result
3. Calls the original handler (async-safe)
4. Calls `hook_runtime.call_many("after_tool_call", ...)`
5. Applies overrides to result / error flag / terminate

The wrapped tool is created via `dataclasses.replace(tool, handler=wrapped_handler)`
and passes through `model_tools()` unchanged (which uses `replace` as well, preserving
the wrapped handler).

#### Terminate Handling

A local `_AgentState` dataclass captures the terminate signal from `after_tool_call`:

```python
@dataclass
class _AgentState:
    tools_terminated: bool = False
```

Closure-captured by the wrapper, checked in `_run_tools_with_auto_handoff` /
`_stream_events_with_auto_handoff` after `_run_once` returns. Non-streaming path
short-circuits the loop; streaming path emits `StreamEvent("final", ...)`.

#### HookRuntime

No new methods. Uses existing `HookRuntime.call_many()` — fires all plugins, collects
results, caller interprets them.

### Error Handling

| Scenario | Behavior |
|----------|----------|
| `before_tool_call` blocks | RepublicError → tool error result, LLM sees reason |
| Hook plugin raises exception | Caught by _invoke_impl_async log; value is _SKIP_VALUE |
| `after_tool_call` errors | Override skipped, original result preserved |

### Scope Notes

- **`_run_command` (`,` prefix) intentionally excluded** — bypasses republic's tool
  machinery entirely, hooks would add complexity for no benefit.
- **`after_tool_call` content override** — replaces the entire result value (string
  replaced by string, dict replaced by dict). No deep merge.
- **`after_tool_call` `is_error` override** — when set to `True`, the result passed
  back to the LLM is marked as an error result regardless of the handler's actual
  outcome.

### Files Changed

| File | Change |
|------|--------|
| `src/bub/hookspecs.py` | Add `before_tool_call`, `after_tool_call` specs |
| `src/bub/builtin/agent.py` | Add `_wrap_tools_with_hooks`, `_AgentState`, integrate in loops |
| `tests/test_hook_tool_lifecycle.py` | New test file |

### Testing

- `test_before_tool_call_blocks_tool` — hook returns `block: True`, tool not executed
- `test_before_tool_call_does_not_block` — hook returns None, tool executes normally
- `test_after_tool_call_overrides_result` — hook returns `{"content": "overridden"}`
- `test_after_tool_call_terminate` — hook returns `{"terminate": True}`, loop stops
- `test_before_tool_call_multiple_plugins` — two plugins, first blocks
- `test_tool_hooks_with_streaming` — same hooks fire in stream_output path
- `test_tool_hooks_parallel_safety` — concurrent tool calls each get hooks
- `test_before_tool_call_plugin_error` — hook plugin raises, tool still runs

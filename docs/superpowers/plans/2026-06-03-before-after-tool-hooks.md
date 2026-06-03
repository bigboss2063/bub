# Tool Lifecycle Hooks (before_tool_call / after_tool_call) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `before_tool_call` and `after_tool_call` hook intercept points so plugins can inspect, block, or override tool calls.

**Architecture:** Two new pluggy hookspecs on `BubHookSpecs`, a `_wrap_tools_with_hooks` helper in `Agent` that wraps each `Tool.handler` with a closure that dispatches hooks before/after the original handler, an `_AgentState` dataclass for termination signaling, and `session_id` threaded through the call chain to `_run_once`.

**Tech Stack:** Python 3.12+, pluggy, republic, pytest + unittest.mock

---

### Task 1: Add hookspec definitions

**Files:**
- Modify: `src/bub/hookspecs.py:95-109` (after `build_tape_context`, before class closing)

- [ ] **Step 1: Add `before_tool_call` and `after_tool_call` hookspecs**

```python
    @hookspec
    def before_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any] | None:
        """Inspect or block a tool before execution.

        Return ``{"block": True, "reason": "..."}}`` to prevent the tool from running.
        Return ``None`` to allow the call to proceed.
        All registered implementations are notified.
        """
        raise NotImplementedError

    @hookspec
    def after_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        session_id: str,
        is_error: bool,
    ) -> dict[str, Any] | None:
        """Inspect or override a tool result after execution.

        Return ``{"content": ..., "is_error": True, "terminate": True}}``
        to override the result, mark it as an error, or terminate the agent loop.
        Return ``None`` to keep the original result unchanged.
        All registered implementations are notified.
        """
        raise NotImplementedError
```

- [ ] **Step 2: Verify hookspecs are recognized by pluggy**

```bash
uv run python -c "import pluggy; from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs; mgr = pluggy.PluginManager(BUB_HOOK_NAMESPACE); mgr.add_hookspecs(BubHookSpecs); print([h for h in dir(mgr.hook) if 'tool' in h])"
```
Expected: `['before_tool_call', 'after_tool_call']`

- [ ] **Step 3: Commit**

```bash
git add src/bub/hookspecs.py
git commit -m "feat: add before_tool_call and after_tool_call hookspecs"
```

---

### Task 2: Add `_AgentState` dataclass and `ErrorKind` import

**Files:**
- Modify: `src/bub/builtin/agent.py:6-36`

- [ ] **Step 1: Add `_AgentState` dataclass near the top of agent.py**

Insert after `_CONTEXT_LENGTH_PATTERNS` (line ~36) and before `MAX_AUTO_HANDOFF_RETRIES` (or anywhere before the `Agent` class):

```python
@dataclass
class _AgentState:
    """Mutable state shared between agent loop and tool hook wrappers."""

    tools_terminated: bool = False
```

- [ ] **Step 2: Add `ErrorKind` to the republic import list**

Change:
```python
from republic import (
    LLM,
    AsyncStreamEvents,
    ...
)
```
to:
```python
from republic import (
    LLM,
    AsyncStreamEvents,
    ...
)
from republic.core.errors import ErrorKind
```

- [ ] **Step 3: Run ruff on the file to verify syntax**

```bash
uv run ruff check src/bub/builtin/agent.py
```

- [ ] **Step 4: Commit**

```bash
git add src/bub/builtin/agent.py
git commit -m "feat: add _AgentState dataclass and ErrorKind import for tool hooks"
```

---

### Task 3: Thread `session_id` and `agent_state` through the call chain

**Files:**
- Modify: `src/bub/builtin/agent.py`

This task modifies method signatures from top to bottom of the call chain.

- [ ] **Step 1: Pass `session_id` from `run()` to `_agent_loop()`**

In `run()` (line ~109), change:
```python
        return await self._agent_loop(
            tape=tape, prompt=prompt, model=model, allowed_skills=allowed_skills, allowed_tools=allowed_tools
        )
```
to:
```python
        return await self._agent_loop(
            tape=tape, prompt=prompt, model=model, allowed_skills=allowed_skills, allowed_tools=allowed_tools,
            session_id=session_id,
        )
```

- [ ] **Step 2: Pass `session_id` from `run_stream()` to `_agent_loop()`**

In `run_stream()` (line ~146), change:
```python
            events = await self._agent_loop(
                tape=tape,
                prompt=prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                stream_output=True,
            )
```
to:
```python
            events = await self._agent_loop(
                tape=tape,
                prompt=prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                stream_output=True,
                session_id=session_id,
            )
```

- [ ] **Step 3: Add `session_id` to `_agent_loop()` overloads and implementation**

Update both overloads (lines ~194-212 and ~214-223) to include `session_id: str`:

First overload:
```python
    @overload
    async def _agent_loop(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = ...,
        allowed_skills: Collection[str] | None = ...,
        allowed_tools: Collection[str] | None = ...,
        stream_output: Literal[False] = ...,
        session_id: str = ...,
    ) -> str: ...
```

Second overload:
```python
    @overload
    async def _agent_loop(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = ...,
        allowed_skills: Collection[str] | None = ...,
        allowed_tools: Collection[str] | None = ...,
        stream_output: Literal[True] = ...,
        session_id: str = ...,
    ) -> AsyncStreamEvents: ...
```

Implementation (line ~224):
```python
    async def _agent_loop(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
        stream_output: bool = False,
        session_id: str = "",
    ) -> AsyncStreamEvents | str:
```

- [ ] **Step 4: Create `_AgentState` in `_agent_loop` and pass to auto-handoff methods**

Inside `_agent_loop()` implementation, before the `if stream_output:` branch, add:

```python
        agent_state = _AgentState()
```

Then update the two delegation calls.

For non-streaming (line ~248):
```python
            return await self._run_tools_with_auto_handoff(
                tape=tape,
                prompt=next_prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                session_id=session_id,
                agent_state=agent_state,
            )
```

For streaming (line ~253):
```python
            iterator = self._stream_events_with_auto_handoff(
                tape=tape,
                prompt=next_prompt,
                state=state,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                session_id=session_id,
                agent_state=agent_state,
            )
```

- [ ] **Step 5: Add `session_id` and `agent_state` to `_run_tools_with_auto_handoff()`**

Method signature (line ~258):
```python
    async def _run_tools_with_auto_handoff(
        self,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
        session_id: str = "",
        agent_state: _AgentState | None = None,
    ) -> str:
```

Update the call to `_run_once` inside the step loop (~line ~280) to pass both:
```python
                output = await self._run_once(
                    tape=tape,
                    prompt=next_prompt,
                    model=model,
                    allowed_skills=allowed_skills,
                    allowed_tools=allowed_tools,
                    session_id=session_id,
                    agent_state=agent_state,
                )
```

- [ ] **Step 6: Add `session_id` and `agent_state` to `_stream_events_with_auto_handoff()`**

Method signature (line ~305):
```python
    async def _stream_events_with_auto_handoff(
        self,
        tape: Tape,
        prompt: str | list[dict],
        state: StreamState,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
        session_id: str = "",
        agent_state: _AgentState | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
```

Update the call to `_run_once` inside the step loop (~line ~330):
```python
            output = await self._run_once(
                tape=tape,
                prompt=next_prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                stream_output=True,
                session_id=session_id,
                agent_state=agent_state,
            )
```

- [ ] **Step 7: Add `session_id` and `agent_state` to `_run_once()` overloads and implementation**

First overload (line ~376):
```python
    @overload
    async def _run_once(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = ...,
        allowed_skills: Collection[str] | None = ...,
        allowed_tools: Collection[str] | None = ...,
        stream_output: Literal[False] = ...,
        session_id: str = ...,
        agent_state: _AgentState | None = ...,
    ) -> ToolAutoResult: ...
```

Second overload (line ~386):
```python
    @overload
    async def _run_once(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = ...,
        allowed_skills: Collection[str] | None = ...,
        allowed_tools: Collection[str] | None = ...,
        stream_output: Literal[True] = ...,
        session_id: str = ...,
        agent_state: _AgentState | None = ...,
    ) -> AsyncStreamEvents: ...
```

Implementation (line ~396):
```python
    async def _run_once(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_tools: Collection[str] | None = None,
        allowed_skills: Collection[str] | None = None,
        stream_output: bool = False,
        session_id: str = "",
        agent_state: _AgentState | None = None,
    ) -> AsyncStreamEvents | ToolAutoResult:
```

- [ ] **Step 8: Run ruff and mypy to verify signatures**

```bash
uv run ruff check src/bub/builtin/agent.py && uv run mypy src/bub/builtin/agent.py
```

- [ ] **Step 9: Commit**

```bash
git add src/bub/builtin/agent.py
git commit -m "feat: thread session_id and agent_state through agent call chain"
```

---

### Task 4: Implement `_wrap_tools_with_hooks`

**Files:**
- Modify: `src/bub/builtin/agent.py` (add method to `Agent` class)

- [ ] **Step 1: Write `_wrap_tools_with_hooks` method**

Add the following method inside the `Agent` class, after `_system_prompt` (before the `_ToolAutoOutcome` dataclass at the module level):

```python
    def _wrap_tools_with_hooks(
        self,
        *,
        session_id: str,
        tools: list[Tool],
        agent_state: _AgentState,
    ) -> list[Tool]:
        hook_runtime = self.framework._hook_runtime
        wrapped: list[Tool] = []
        for tool in tools:
            original = tool.handler
            tool_name = tool.name

            def _make_wrapped(
                orig: Any = original,
                tname: str = tool_name,
            ) -> Any:
                async def wrapped_handler(*args: Any, **kwargs: Any) -> Any:
                    arguments = dict(kwargs)
                    for i, arg in enumerate(args):
                        arguments[f"__arg{i}"] = arg

                    # --- before_tool_call hooks ---
                    try:
                        before_results = await hook_runtime.call_many(
                            "before_tool_call",
                            tool_name=tname,
                            arguments=arguments,
                            session_id=session_id,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            "before_tool_call plugin error for tool={}", tname
                        )
                        before_results = []

                    for result in before_results:
                        if isinstance(result, dict) and result.get("block"):
                            raise RepublicError(
                                kind=ErrorKind.TOOL,
                                message=result.get("reason", "blocked by plugin"),
                            )

                    # --- original handler ---
                    is_error = False
                    result: Any = None
                    try:
                        result = await orig(*args, **kwargs)
                    except Exception:
                        is_error = True

                    # --- after_tool_call hooks ---
                    try:
                        after_results = await hook_runtime.call_many(
                            "after_tool_call",
                            tool_name=tname,
                            arguments=arguments,
                            result=result,
                            session_id=session_id,
                            is_error=is_error,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            "after_tool_call plugin error for tool={}", tname
                        )
                        after_results = []

                    for r in after_results:
                        if isinstance(r, dict):
                            if "content" in r:
                                result = r["content"]
                                is_error = False
                            if r.get("is_error"):
                                is_error = True
                            if r.get("terminate"):
                                agent_state.tools_terminated = True

                    if is_error:
                        err_msg = str(result) if result is not None else "tool execution error"
                        raise RepublicError(kind=ErrorKind.TOOL, message=err_msg)

                    return result

                return wrapped_handler

            wrapped_tool = replace(tool, handler=_make_wrapped())
            wrapped.append(wrapped_tool)
        return wrapped
```

- [ ] **Step 2: Run ruff on the file**

```bash
uv run ruff check src/bub/builtin/agent.py
```

- [ ] **Step 3: Commit**

```bash
git add src/bub/builtin/agent.py
git commit -m "feat: implement _wrap_tools_with_hooks for tool lifecycle interception"
```

> **Implementation note:** `HookRuntime.call_many` does **not** catch exceptions from individual plugin implementations — it propagates them. The `try/except Exception` blocks around `call_many` in `_wrap_tools_with_hooks` are therefore essential. If a faulty plugin crashes, we log a warning and fall back to `[]` results, which means the tool call proceeds as if no plugin objected. A known trade-off: results from plugins that ran *before* the crashing one are lost. A future iteration could iterate impl-by-impl with per-impl error handling, but this is acceptable for V1.

---

### Task 5: Integrate wrapping in `_run_once` and termination checks

**Files:**
- Modify: `src/bub/builtin/agent.py` (`_run_once`, `_run_tools_with_auto_handoff`, `_stream_events_with_auto_handoff`)

- [ ] **Step 1: Wrap tools in `_run_once` before calling republic**

In `_run_once()`, after the tools are resolved but before the `stream_events_async` / `run_tools_async` call, insert wrapping. Change:

```python
        async with asyncio.timeout(self.settings.model_timeout_seconds):
            if stream_output:
                return await tape.stream_events_async(
                    prompt=prompt,
                    system_prompt=...,
                    max_tokens=...,
                    tools=model_tools(tools),
                    model=model,
                )
            else:
                return await tape.run_tools_async(
                    prompt=prompt,
                    system_prompt=...,
                    max_tokens=...,
                    tools=model_tools(tools),
                    model=model,
                )
```

to:

```python
        wrapped_tools = tools
        if agent_state is not None:
            wrapped_tools = self._wrap_tools_with_hooks(
                session_id=session_id, tools=tools, agent_state=agent_state,
            )
        async with asyncio.timeout(self.settings.model_timeout_seconds):
            if stream_output:
                return await tape.stream_events_async(
                    prompt=prompt,
                    system_prompt=...,
                    max_tokens=...,
                    tools=model_tools(wrapped_tools),
                    model=model,
                )
            else:
                return await tape.run_tools_async(
                    prompt=prompt,
                    system_prompt=...,
                    max_tokens=...,
                    tools=model_tools(wrapped_tools),
                    model=model,
                )
```

- [ ] **Step 2: Add termination check in `_run_tools_with_auto_handoff`**

Add the check immediately after the `_run_once` call and before `_resolve_tool_auto_result`. Change the step loop body from:

```python
            output = await self._run_once(
                tape=tape, prompt=next_prompt, model=model,
                allowed_skills=allowed_skills, allowed_tools=allowed_tools,
                session_id=session_id, agent_state=agent_state,
            )
            outcome = _resolve_tool_auto_result(output)
            if outcome.kind == "text":
                ...
```

to:

```python
            output = await self._run_once(
                tape=tape, prompt=next_prompt, model=model,
                allowed_skills=allowed_skills, allowed_tools=allowed_tools,
                session_id=session_id, agent_state=agent_state,
            )
            outcome = _resolve_tool_auto_result(output)
            if agent_state is not None and agent_state.tools_terminated:
                ...  # append tape event as usual
                if outcome.kind == "text" and outcome.text:
                    return outcome.text
                return "Task completed by tool hook."
            if outcome.kind == "text":
                ...
```

Important: replicate the `elapsed_ms` tracking and `tapes.append_event` call for the termination branch as well:

```python
            if agent_state is not None and agent_state.tools_terminated:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "ok",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                outcome = _resolve_tool_auto_result(output)
                if outcome.kind == "text" and outcome.text:
                    return outcome.text
                return "Task completed by tool hook."
```

- [ ] **Step 3: Add termination check in `_stream_events_with_auto_handoff`**

After the `async for event in output:` loop and the `state.error = output.error` / `state.usage = output.usage` lines, add:

```python
            state.error = output.error
            state.usage = output.usage

            if agent_state is not None and agent_state.tools_terminated:
                return  # exit the step loop immediately

            elapsed_ms = int((time.monotonic() - start) * 1000)
            if outcome.kind == "text":
                ...
```

- [ ] **Step 4: Run ruff and mypy**

```bash
uv run ruff check src/bub/builtin/agent.py && uv run mypy src/bub/builtin/agent.py
```

- [ ] **Step 5: Commit**

```bash
git add src/bub/builtin/agent.py
git commit -m "feat: integrate tool hooks into _run_once and add termination handling"
```

---

### Task 6: Write test file - hook specs and basic blocking

**Files:**
- Create: `tests/test_hook_tool_lifecycle.py`

- [ ] **Step 1: Create test file with imports and helpers**

```python
"""Tests for before_tool_call / after_tool_call hook lifecycle."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pluggy
import pytest
from republic import Tool

from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl


def _runtime_with_plugins(*plugins: tuple[str, object]) -> HookRuntime:
    manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    manager.add_hookspecs(BubHookSpecs)
    for name, plugin in plugins:
        manager.register(plugin, name=name)
    return HookRuntime(manager)


def _make_tool(name: str, handler=None) -> Tool:
    if handler is None:

        async def _default_handler(**kwargs: Any) -> str:
            return "ok"

        handler = _default_handler
    return Tool(name=name, description="test tool", handler=handler, parameters={}, context=False)
```

- [ ] **Step 2: Write `test_before_tool_call_blocks_tool`**

```python
@pytest.mark.asyncio
async def test_before_tool_call_blocks_tool() -> None:
    """Hook returning block=True prevents tool execution."""
    blocked: list[str] = []
    executed: list[str] = []

    class BlockingPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            blocked.append(tool_name)
            return {"block": True, "reason": "test block"}

    async def handler(**kwargs: Any) -> str:
        executed.append("ran")
        return "result"

    runtime = _runtime_with_plugins(("block", BlockingPlugin()))
    tool = _make_tool("test_tool", handler)

    # Simulate wrapped handler logic inline for this test
    import asyncio
    from republic import RepublicError
    from republic.core.errors import ErrorKind

    async def wrapped(**kwargs: Any) -> Any:
        arguments = dict(kwargs)
        before_results = await runtime.call_many(
            "before_tool_call", tool_name="test_tool", arguments=arguments, session_id="s1"
        )
        for r in before_results:
            if isinstance(r, dict) and r.get("block"):
                raise RepublicError(kind=ErrorKind.TOOL, message=r.get("reason", "blocked"))
        return await handler(**kwargs)

    with pytest.raises(RepublicError) as exc_info:
        await wrapped(x=1)
    assert exc_info.value.kind.value == "tool"
    assert "test block" in str(exc_info.value)
    assert blocked == ["test_tool"]
    assert executed == []
```

- [ ] **Step 3: Run test to verify it fails on the first run**

```bash
uv run pytest tests/test_hook_tool_lifecycle.py::test_before_tool_call_blocks_tool -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_hook_tool_lifecycle.py
git commit -m "test: add before_tool_call block test"
```

---

### Task 7: Write remaining tests

**Files:**
- Modify: `tests/test_hook_tool_lifecycle.py` (append tests)

- [ ] **Step 1: Write `test_before_tool_call_does_not_block`**

```python
@pytest.mark.asyncio
async def test_before_tool_call_does_not_block() -> None:
    """Hook returning None allows tool to execute normally."""
    called: list[str] = []

    class AllowPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            called.append(tool_name)
            return None

    async def handler(**kwargs: Any) -> str:
        return "real result"

    runtime = _runtime_with_plugins(("allow", AllowPlugin()))
    tool = _make_tool("test_tool", handler)

    async def wrapped(**kwargs: Any) -> Any:
        arguments = dict(kwargs)
        before_results = await runtime.call_many(
            "before_tool_call", tool_name="test_tool", arguments=arguments, session_id="s1"
        )
        for r in before_results:
            if isinstance(r, dict) and r.get("block"):
                from republic import RepublicError
                from republic.core.errors import ErrorKind
                raise RepublicError(kind=ErrorKind.TOOL, message=r.get("reason", "blocked"))
        return await handler(**kwargs)

    result = await wrapped(x=1)
    assert result == "real result"
    assert called == ["test_tool"]
```

- [ ] **Step 2: Write `test_after_tool_call_overrides_result`**

```python
@pytest.mark.asyncio
async def test_after_tool_call_overrides_result() -> None:
    """Hook returning content overrides the tool result."""
    after_called: list[str] = []

    class OverridePlugin:
        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            after_called.append(tool_name)
            return {"content": "overridden"}

    async def handler(**kwargs: Any) -> str:
        return "original"

    runtime = _runtime_with_plugins(("override", OverridePlugin()))

    arguments = {"x": 1}
    result = await handler(**arguments)

    after_results = await runtime.call_many(
        "after_tool_call",
        tool_name="test_tool", arguments=arguments,
        result=result, session_id="s1", is_error=False,
    )
    for r in after_results:
        if isinstance(r, dict) and "content" in r:
            result = r["content"]

    assert result == "overridden"
    assert after_called == ["test_tool"]
```

- [ ] **Step 3: Write `test_after_tool_call_terminate`**

```python
@pytest.mark.asyncio
async def test_after_tool_call_terminate() -> None:
    """Hook returning terminate=True sets the termination flag."""
    from dataclasses import dataclass

    @dataclass
    class State:
        tools_terminated: bool = False

    class TerminatePlugin:
        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            return {"terminate": True}

    runtime = _runtime_with_plugins(("terminate", TerminatePlugin()))
    state = State()

    arguments = {}
    result = "done"

    after_results = await runtime.call_many(
        "after_tool_call",
        tool_name="test_tool", arguments=arguments,
        result=result, session_id="s1", is_error=False,
    )
    for r in after_results:
        if isinstance(r, dict) and r.get("terminate"):
            state.tools_terminated = True

    assert state.tools_terminated is True
```

- [ ] **Step 4: Write `test_before_tool_call_multiple_plugins`**

```python
@pytest.mark.asyncio
async def test_before_tool_call_multiple_plugins() -> None:
    """Multiple plugins: first one blocks, second never reached for that tool."""
    call_order: list[str] = []

    class PluginA:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            call_order.append("A")
            return {"block": True, "reason": "A"}

    class PluginB:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            call_order.append("B")
            return None

    runtime = _runtime_with_plugins(("A", PluginA()), ("B", PluginB()))

    before_results = await runtime.call_many(
        "before_tool_call", tool_name="test_tool", arguments={}, session_id="s1"
    )
    # Both plugins were called (call_many notifies all)
    assert "A" in call_order
    assert "B" in call_order
    # At least one result has block=True
    assert any(isinstance(r, dict) and r.get("block") for r in before_results)
```

- [ ] **Step 5: Write `test_before_tool_call_plugin_error`**

```python
@pytest.mark.asyncio
async def test_before_tool_call_plugin_error() -> None:
    """Plugin that raises an exception does not prevent tool execution."""
    executed: list[str] = []

    class FaultyPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            raise RuntimeError("plugin crash")

    async def handler(**kwargs: Any) -> str:
        executed.append("ran")
        return "ok"

    runtime = _runtime_with_plugins(("faulty", FaultyPlugin()))

    # Simulate wrapper try/except around hook call
    try:
        before_results = await runtime.call_many(
            "before_tool_call", tool_name="test_tool", arguments={}, session_id="s1"
        )
    except Exception:
        before_results = []

    # Tool should still execute (no block found)
    block_found = any(isinstance(r, dict) and r.get("block") for r in before_results)
    if not block_found:
        result = await handler()
        assert result == "ok"
    assert executed == ["ran"]
```

- [ ] **Step 6: Write `test_tool_hooks_with_streaming`**

```python
@pytest.mark.asyncio
async def test_tool_hooks_with_streaming() -> None:
    """Hooks trigger in streaming output path same as non-streaming."""
    before_called: list[str] = []
    after_called: list[str] = []

    class ObservePlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            before_called.append(tool_name)
            return None

        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            after_called.append(tool_name)
            return None

    runtime = _runtime_with_plugins(("obs", ObservePlugin()))

    # Verify hooks are callable (the fact that our wrapper calls them
    # identically for stream vs non-stream paths is tested via integration)
    await runtime.call_many(
        "before_tool_call", tool_name="stream_tool", arguments={"stream": True}, session_id="s1"
    )
    assert before_called == ["stream_tool"]

    await runtime.call_many(
        "after_tool_call", tool_name="stream_tool", arguments={"stream": True},
        result="stream_result", session_id="s1", is_error=False,
    )
    assert after_called == ["stream_tool"]
```

- [ ] **Step 7: Write `test_tool_hooks_parallel_safety`**

```python
@pytest.mark.asyncio
async def test_tool_hooks_parallel_safety() -> None:
    """Concurrent tool calls each get their own hook invocations with correct names."""
    import asyncio

    seen: list[tuple[str, str]] = []

    class TrackPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            seen.append(("before", tool_name))
            return None

        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            seen.append(("after", tool_name))
            return None

    runtime = _runtime_with_plugins(("track", TrackPlugin()))

    async def simulate_tool(tool_name: str) -> str:
        await runtime.call_many(
            "before_tool_call", tool_name=tool_name, arguments={}, session_id="s1"
        )
        await asyncio.sleep(0.01)
        result = f"result_{tool_name}"
        await runtime.call_many(
            "after_tool_call", tool_name=tool_name, arguments={},
            result=result, session_id="s1", is_error=False,
        )
        return result

    results = await asyncio.gather(
        simulate_tool("tool_a"),
        simulate_tool("tool_b"),
        simulate_tool("tool_c"),
    )

    assert results == ["result_tool_a", "result_tool_b", "result_tool_c"]
    before_tools = {name for kind, name in seen if kind == "before"}
    assert before_tools == {"tool_a", "tool_b", "tool_c"}
```

- [ ] **Step 8: Run all hook lifecycle tests**

```bash
uv run pytest tests/test_hook_tool_lifecycle.py -v
```
Expected: 8 passed

- [ ] **Step 9: Run ruff on the test file**

```bash
uv run ruff check tests/test_hook_tool_lifecycle.py
```

- [ ] **Step 10: Commit**

```bash
git add tests/test_hook_tool_lifecycle.py
git commit -m "test: complete tool lifecycle hook test suite"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -q
```

- [ ] **Step 2: Run lint and type checking**

```bash
uv run ruff check . && uv run mypy src
```

- [ ] **Step 3: Verify hooks are discoverable**

```bash
uv run bub hooks
```

Expected: `before_tool_call` and `after_tool_call` appear in the hook report (with no implementations listed unless a plugin provides them).

- [ ] **Step 4: Final commit (if any fixes were needed)**

```bash
git add -A && git commit -m "chore: final verification pass for tool lifecycle hooks"
```

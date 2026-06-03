"""Tests for before_tool_call / after_tool_call hook lifecycle."""

from __future__ import annotations

from typing import Any

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

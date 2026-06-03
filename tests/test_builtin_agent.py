from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pluggy
import pytest
import republic.auth.openai_codex as openai_codex
from republic import AsyncStreamEvents, RepublicError, StreamEvent, TapeContext, Tool

import bub.builtin.agent as agent_module
from bub.builtin.agent import Agent, _AgentState
from bub.builtin.settings import AgentSettings
from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl
from bub.tools import REGISTRY, tool


def test_build_llm_passes_codex_resolver_to_republic(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    resolver = object()

    class FakeLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(agent_module, "LLM", FakeLLM)
    monkeypatch.setattr(openai_codex, "openai_codex_oauth_resolver", lambda: resolver)

    settings = AgentSettings.model_construct(
        model="openai:gpt-5-codex",
        api_key=None,
        api_base=None,
        client_args={"extra_headers": {"HTTP-Referer": "https://openclaw.ai", "X-Title": "OpenClaw"}},
    )
    tape_store = object()

    agent_module._build_llm(settings, tape_store, "ctx")

    assert captured["args"] == ("openai:gpt-5-codex",)
    assert captured["kwargs"]["api_key"] is None
    assert captured["kwargs"]["api_base"] is None
    assert captured["kwargs"]["client_args"] == {
        "extra_headers": {"HTTP-Referer": "https://openclaw.ai", "X-Title": "OpenClaw"},
    }
    assert captured["kwargs"]["api_key_resolver"] is resolver
    assert captured["kwargs"]["tape_store"] is tape_store
    assert captured["kwargs"]["context"] == "ctx"


# ---------------------------------------------------------------------------
# Agent.run() tests: merge_back logic and model passthrough
# ---------------------------------------------------------------------------


def _make_agent() -> Agent:
    """Build an Agent with a mocked framework, bypassing real LLM/tape init."""
    framework = MagicMock()
    framework.get_tape_store.return_value = None
    framework.get_system_prompt.return_value = ""

    with patch.object(Agent, "__init__", lambda self, fw: None):
        agent = Agent.__new__(Agent)

    agent.settings = AgentSettings.model_construct(model="test:model", api_key="k", api_base="b")
    agent.framework = framework
    return agent


class _ForkCapture:
    """Captures the merge_back kwarg passed to fork_tape."""

    def __init__(self) -> None:
        self.merge_back_values: list[bool] = []

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        self.merge_back_values.append(merge_back)
        yield


class _FakeTapeService:
    """Minimal TapeService stand-in for testing Agent.run()."""

    def __init__(self, fork_capture: _ForkCapture) -> None:
        self._fork = fork_capture
        self.run_tools_model: str | None = None
        self.stream_kwargs: dict[str, Any] | None = None

    def session_tape(self, session_id: str, workspace: Any) -> MagicMock:
        tape = MagicMock()
        tape.name = "test-tape"
        tape.context = TapeContext(state={})

        async def fake_stream_events_async(**kwargs: Any) -> AsyncStreamEvents:
            self.run_tools_model = kwargs.get("model")
            self.stream_kwargs = kwargs

            async def iterator():
                yield StreamEvent("final", {"text": "done"})

            return AsyncStreamEvents(iterator())

        tape.stream_events_async = fake_stream_events_async
        return tape

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        pass

    async def append_event(self, tape_name: str, name: str, payload: dict) -> None:
        pass

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._fork.fork_tape(tape_name, merge_back=merge_back):
            yield


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back() -> None:
    """A regular (non-temp) session should merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    assert fork_capture.merge_back_values == [True]


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back() -> None:
    """A temp/ session should NOT merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    result = await agent.run_stream(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    assert fork_capture.merge_back_values == [False]


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm() -> None:
    """The model parameter should be forwarded to stream_events_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        model="openai:gpt-4o",
    )
    [event async for event in result]

    assert fake_tapes.run_tools_model == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_agent_run_empty_prompt_returns_error() -> None:
    agent = _make_agent()
    agent.tapes = MagicMock()  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/s1", prompt="", state={})
    events = [event async for event in result]

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "error: empty prompt"}),
        ("final", {"ok": False, "text": "error: empty prompt"}),
    ]


@pytest.mark.asyncio
async def test_agent_run_model_defaults_to_none() -> None:
    """When model is not specified, None should be passed to run_tools_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    assert fake_tapes.run_tools_model is None


@pytest.mark.asyncio
async def test_agent_run_resolves_allowed_tool_aliases_and_limits_prompt() -> None:
    allowed_name = "tests.allowed_agent_tool"
    denied_name = "tests.denied_agent_tool"
    REGISTRY.pop(allowed_name, None)
    REGISTRY.pop(denied_name, None)

    @tool(name=allowed_name, description="Allowed tool")
    def allowed_agent_tool() -> str:
        return "allowed"

    @tool(name=denied_name, description="Denied tool")
    def denied_agent_tool() -> str:
        return "denied"

    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=[" tests_allowed_agent_tool "],
    )
    [event async for event in result]

    assert fake_tapes.stream_kwargs is not None
    assert [tool.name for tool in fake_tapes.stream_kwargs["tools"]] == ["tests_allowed_agent_tool"]
    system_prompt = fake_tapes.stream_kwargs["system_prompt"]
    assert "- tests_allowed_agent_tool(): Allowed tool" in system_prompt
    assert "tests_denied_agent_tool" not in system_prompt


@pytest.mark.asyncio
async def test_agent_run_rejects_unknown_allowed_tools() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    stream = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=["tests_missing_agent_tool"],
    )

    with pytest.raises(ValueError, match="tests_missing_agent_tool"):
        [event async for event in stream]


# ---------------------------------------------------------------------------
# Agent._wrap_tools_with_hooks integration tests
# ---------------------------------------------------------------------------


def _make_agent_with_hook_runtime(*plugins: tuple[str, object]) -> Agent:
    pm = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    pm.add_hookspecs(BubHookSpecs)
    for name, plugin in plugins:
        pm.register(plugin, name=name)
    hook_runtime = HookRuntime(pm)

    framework = MagicMock()
    framework._hook_runtime = hook_runtime
    framework.get_system_prompt.return_value = ""
    framework.get_tape_store.return_value = None

    agent = Agent.__new__(Agent)
    agent.settings = AgentSettings.model_construct(model="test:model", api_key="k", api_base="b")
    agent.framework = framework
    return agent


@pytest.mark.asyncio
async def test_wrap_tools_before_hook_blocks_tool() -> None:
    blocked: list[str] = []
    executed: list[str] = []

    class BlockPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            blocked.append(tool_name)
            return {"block": True, "reason": "blocked by test"}

    async def handler(**kwargs: Any) -> str:
        executed.append("ran")
        return "result"

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("block", BlockPlugin()))
    agent_state = _AgentState()
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=agent_state)

    with pytest.raises(RepublicError) as exc_info:
        await wrapped[0].handler(x=1)
    assert exc_info.value.kind.value == "tool"
    assert "blocked by test" in str(exc_info.value)
    assert blocked == ["test_tool"]
    assert executed == []


@pytest.mark.asyncio
async def test_wrap_tools_before_hook_allows_tool() -> None:
    called: list[str] = []

    class AllowPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            called.append(tool_name)
            return None

    async def handler(**kwargs: Any) -> str:
        return "real result"

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("allow", AllowPlugin()))
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=_AgentState())

    result = await wrapped[0].handler(x=1)
    assert result == "real result"
    assert called == ["test_tool"]


@pytest.mark.asyncio
async def test_wrap_tools_after_hook_overrides_result() -> None:
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

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("override", OverridePlugin()))
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=_AgentState())

    result = await wrapped[0].handler(x=1)
    assert result == "overridden"
    assert after_called == ["test_tool"]


@pytest.mark.asyncio
async def test_wrap_tools_after_hook_terminate() -> None:
    class TermPlugin:
        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            return {"terminate": True}

    async def handler(**kwargs: Any) -> str:
        return "done"

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("term", TermPlugin()))
    agent_state = _AgentState()

    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=agent_state)
    result = await wrapped[0].handler()
    assert result == "done"
    assert agent_state.tools_terminated is True


@pytest.mark.asyncio
async def test_wrap_tools_preserves_tool_exception_message() -> None:
    async def handler(**kwargs: Any) -> str:
        raise ValueError("specific tool failure")

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime()
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=_AgentState())

    with pytest.raises(RepublicError) as exc_info:
        await wrapped[0].handler()
    assert "specific tool failure" in str(exc_info.value)
    assert "tool execution error" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_wrap_tools_before_hook_exception_does_not_block() -> None:
    executed: list[str] = []

    class FaultyPlugin:
        @hookimpl
        def before_tool_call(self, tool_name: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any] | None:
            raise RuntimeError("plugin crash")

    async def handler(**kwargs: Any) -> str:
        executed.append("ran")
        return "ok"

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("faulty", FaultyPlugin()))
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=_AgentState())

    result = await wrapped[0].handler()
    assert result == "ok"
    assert executed == ["ran"]


@pytest.mark.asyncio
async def test_wrap_tools_after_hook_exception_keeps_original_result() -> None:
    class FaultyPlugin:
        @hookimpl
        def after_tool_call(
            self, tool_name: str, arguments: dict[str, Any],
            result: Any, session_id: str, is_error: bool,
        ) -> dict[str, Any] | None:
            raise RuntimeError("after crash")

    async def handler(**kwargs: Any) -> str:
        return "original"

    test_tool = Tool(name="test_tool", description="test", handler=handler, parameters={}, context=False)
    agent = _make_agent_with_hook_runtime(("faulty", FaultyPlugin()))
    wrapped = agent._wrap_tools_with_hooks(session_id="s1", tools=[test_tool], agent_state=_AgentState())

    result = await wrapped[0].handler()
    assert result == "original"


# ---------------------------------------------------------------------------
# _run_command hook bypass test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_skips_tool_hooks() -> None:
    before_called: list[str] = []
    after_called: list[str] = []

    class SpyPlugin:
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

    cmd_tool_name = "tests.cmd_skip_tool"
    REGISTRY.pop(cmd_tool_name, None)

    @tool(name=cmd_tool_name, description="Test command tool")
    def cmd_skip_tool() -> str:
        return "command executed"

    agent = _make_agent_with_hook_runtime(("spy", SpyPlugin()))
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/s1", prompt=f",{cmd_tool_name}", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    assert before_called == []
    assert after_called == []

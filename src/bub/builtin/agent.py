"""Republic-driven runtime engine to process prompts."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Coroutine, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, overload

from loguru import logger
from republic import (
    LLM,
    AsyncStreamEvents,
    AsyncTapeStore,
    RepublicError,
    StreamEvent,
    StreamState,
    TapeContext,
    Tool,
    ToolAutoResult,
    ToolContext,
)
from republic.core.errors import ErrorKind
from republic.tape import InMemoryTapeStore, Tape

from bub import ensure_config
from bub.builtin.compaction.core import should_compact
from bub.builtin.compaction.types import CompactionSettings
from bub.builtin.settings import AgentSettings, load_settings
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService
from bub.framework import BubFramework
from bub.skills import discover_skills, render_skills_prompt
from bub.tools import REGISTRY, model_tools, render_tools_prompt, resolve_tool_names
from bub.types import State
from bub.utils import workspace_from_state

CONTINUE_PROMPT = "Continue the task until all targets are completed."
HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
_CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)
MAX_AUTO_HANDOFF_RETRIES = 1
COMPACT_COOLDOWN_STEPS = 2


@dataclass
class _AgentState:
    """Mutable state shared between agent loop and tool hook wrappers."""

    tools_terminated: bool = False


class Agent:
    """Agent that processes prompts using hooks and tools. Backed by republic."""

    def __init__(self, framework: BubFramework) -> None:
        self.settings = load_settings()
        self.framework = framework

    @cached_property
    def tapes(self) -> TapeService:
        import bub

        tape_store = self.framework.get_tape_store()
        if tape_store is None:
            tape_store = InMemoryTapeStore()
        tape_store = ForkTapeStore(tape_store)
        llm = _build_llm(self.settings, tape_store, self.framework.build_tape_context())
        return TapeService(llm, bub.home / "tapes", tape_store)

    @staticmethod
    def _events_from_iterable(iterable: Iterable) -> AsyncStreamEvents:
        async def generator() -> AsyncIterator:
            for item in iterable:
                yield item

        return AsyncStreamEvents(generator())

    @staticmethod
    def _events_with_callback(
        events: AsyncStreamEvents, callback: Callable[[], Coroutine[Any, Any, Any]]
    ) -> AsyncStreamEvents:
        async def generator() -> AsyncIterator[StreamEvent]:
            async for event in events:
                yield event
            await callback()

        return AsyncStreamEvents(generator(), state=events._state)

    async def run(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: State,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> str:
        if not prompt:
            return "error: empty prompt"
        tape = self.tapes.session_tape(session_id, workspace_from_state(state))
        tape.context = replace(tape.context, state=state)
        merge_back = not session_id.startswith("temp/")
        async with self.tapes.fork_tape(tape.name, merge_back=merge_back):
            await self.tapes.ensure_bootstrap_anchor(tape.name)
            if isinstance(prompt, str) and prompt.strip().startswith(","):
                return await self._run_command(tape=tape, line=prompt.strip())
            return await self._agent_loop(
                tape=tape, prompt=prompt, model=model, allowed_skills=allowed_skills, allowed_tools=allowed_tools,
                session_id=session_id,
            )

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: State,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> AsyncStreamEvents:
        if not prompt:
            events = [
                StreamEvent("text", {"delta": "error: empty prompt"}),
                StreamEvent("final", {"text": "error: empty prompt", "ok": False}),
            ]
            return self._events_from_iterable(events)

        tape = self.tapes.session_tape(session_id, workspace_from_state(state))
        tape.context = replace(tape.context, state=state)
        merge_back = not session_id.startswith("temp/")
        stack = AsyncExitStack()
        # the fork_tape context manager must not be exited until the last chunk of the stream is consumed.
        # So we use an AsyncExitStack and inject a callback to the iterator.
        await stack.enter_async_context(self.tapes.fork_tape(tape.name, merge_back=merge_back))
        await self.tapes.ensure_bootstrap_anchor(tape.name)
        if isinstance(prompt, str) and prompt.strip().startswith(","):
            result = await self._run_command(tape=tape, line=prompt.strip())
            events = self._events_from_iterable([
                StreamEvent("text", {"delta": result}),
                StreamEvent("final", {"text": result, "ok": True}),
            ])
        else:
            events = await self._agent_loop(
                tape=tape,
                prompt=prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                stream_output=True,
                session_id=session_id,
            )
        return self._events_with_callback(events, callback=stack.aclose)

    async def _run_command(self, tape: Tape, *, line: str) -> str:
        line = line[1:].strip()
        if not line:
            raise ValueError("empty command")

        name, arg_tokens = _parse_internal_command(line)
        start = time.monotonic()
        context = ToolContext(tape=tape.name, run_id="run_command", state=tape.context.state)
        output = ""
        status = "ok"
        try:
            if name not in REGISTRY:
                output = await REGISTRY["bash"].run(context=context, cmd=line)
            else:
                args = _parse_args(arg_tokens)
                if REGISTRY[name].context:
                    args.kwargs["context"] = context
                output = REGISTRY[name].run(*args.positional, **args.kwargs)
                if inspect.isawaitable(output):
                    output = await output
        except Exception as exc:
            status = "error"
            output = f"{exc!s}"
            raise
        else:
            return output if isinstance(output, str) else str(output)
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            output_text = output if isinstance(output, str) else str(output)

            event_payload = {
                "raw": line,
                "name": name,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "output": output_text,
                "date": datetime.now(UTC).isoformat(),
            }
            await self.tapes.append_event(tape.name, "command", event_payload)

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
        next_prompt: str | list[dict] = prompt
        display_model = model or self.settings.model
        await self.tapes.append_event(
            tape.name,
            "loop.start",
            {
                "model": display_model,
                "prompt": prompt,
                "allowed_skills": list(allowed_skills) if allowed_skills else None,
                "allowed_tools": list(allowed_tools) if allowed_tools else None,
            },
        )
        agent_state = _AgentState()
        if stream_output:
            state = StreamState()
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
            return AsyncStreamEvents(iterator, state=state)
        else:
            return await self._run_tools_with_auto_handoff(
                tape=tape,
                prompt=next_prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
                session_id=session_id,
                agent_state=agent_state,
            )

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
        auto_handoff_remaining = MAX_AUTO_HANDOFF_RETRIES
        compact_cooldown = 0
        display_model = model or self.settings.model
        next_prompt = prompt
        for step in range(1, self.settings.max_steps + 1):
            start = time.monotonic()
            logger.info("loop.step step={} tape={} model={}", step, tape.name, display_model)
            await self.tapes.append_event(tape.name, "loop.step.start", {"step": step, "prompt": next_prompt})
            try:
                output = await self._run_once(
                    tape=tape,
                    prompt=next_prompt,
                    model=model,
                    allowed_skills=allowed_skills,
                    allowed_tools=allowed_tools,
                    session_id=session_id,
                    agent_state=agent_state,
                )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "error",
                        "error": f"{exc!s}",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                raise

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
                if output.kind == "text" and output.text:
                    return str(output.text)
                return "Task completed by tool hook."
            outcome = _resolve_tool_auto_result(output)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if outcome.kind == "text":
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
                return outcome.text
            if outcome.kind == "continue":
                if "context" in tape.context.state:
                    next_prompt = f"{CONTINUE_PROMPT} [context: {tape.context.state['context']}]"
                else:
                    next_prompt = CONTINUE_PROMPT
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "continue",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                continue

            usage = getattr(output, "usage", None)
            total_tokens = getattr(usage, "total_tokens", None) if usage else None
            should_retry, compact_cooldown = await self._run_compaction_checks(
                tape, step, elapsed_ms, total_tokens, outcome.error,
                can_overflow_compact=auto_handoff_remaining > 0,
                compact_cooldown=compact_cooldown,
            )
            if should_retry:
                auto_handoff_remaining -= 1
                next_prompt = prompt
                continue

            await self.tapes.append_event(
                tape.name,
                "loop.step",
                {
                    "step": step,
                    "elapsed_ms": elapsed_ms,
                    "status": "error",
                    "error": outcome.error,
                    "date": datetime.now(UTC).isoformat(),
                },
            )
            raise RuntimeError(outcome.error)

        raise RuntimeError(f"max_steps_reached={self.settings.max_steps}")

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
        auto_handoff_remaining = MAX_AUTO_HANDOFF_RETRIES
        compact_cooldown = 0
        display_model = model or self.settings.model
        next_prompt = prompt
        for step in range(1, self.settings.max_steps + 1):
            start = time.monotonic()
            outcome = _ToolAutoOutcome(kind="text", text="", error="")
            logger.info("loop.step step={} tape={} model={}", step, tape.name, display_model)
            await self.tapes.append_event(tape.name, "loop.step.start", {"step": step, "prompt": next_prompt})
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
            async for event in output:
                yield event
                if event.kind == "error":
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    await self.tapes.append_event(
                        tape.name,
                        "loop.step",
                        {
                            "step": step,
                            "elapsed_ms": elapsed_ms,
                            "status": "error",
                            "error": event.data.get("message", ""),
                            "date": datetime.now(UTC).isoformat(),
                        },
                    )
                elif event.kind == "final":
                    outcome = _resolve_final_data(event.data, output.error)

            state.error = output.error
            state.usage = output.usage

            if agent_state is not None and agent_state.tools_terminated:
                return

            elapsed_ms = int((time.monotonic() - start) * 1000)
            if outcome.kind == "text":
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
                return
            if outcome.kind == "continue":
                if "context" in tape.context.state:
                    next_prompt = f"{CONTINUE_PROMPT} [context: {tape.context.state['context']}]"
                else:
                    next_prompt = CONTINUE_PROMPT
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "continue",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                continue

            total_tokens = getattr(state.usage, "total_tokens", None) if state.usage else None
            should_retry, compact_cooldown = await self._run_compaction_checks(
                tape, step, elapsed_ms, total_tokens, outcome.error,
                can_overflow_compact=auto_handoff_remaining > 0,
                compact_cooldown=compact_cooldown,
            )
            if should_retry:
                auto_handoff_remaining -= 1
                next_prompt = prompt
                continue

            await self.tapes.append_event(
                tape.name,
                "loop.step",
                {
                    "step": step,
                    "elapsed_ms": elapsed_ms,
                    "status": "error",
                    "error": outcome.error,
                    "date": datetime.now(UTC).isoformat(),
                },
            )
            raise RuntimeError(outcome.error)

        raise RuntimeError(f"max_steps_reached={self.settings.max_steps}")

    async def _run_compaction_checks(
        self,
        tape: Tape,
        step: int,
        elapsed_ms: int,
        total_tokens: int | None,
        error: str,
        can_overflow_compact: bool,
        compact_cooldown: int,
    ) -> tuple[bool, int]:
        in_cooldown = compact_cooldown > 0
        if in_cooldown:
            compact_cooldown -= 1

        if not in_cooldown and total_tokens:
            settings = ensure_config(CompactionSettings)
            if settings.enabled and should_compact(total_tokens, settings.context_window, settings):
                logger.info("compaction: threshold reached, triggering proactive compaction. tape={}", tape.name)
                await self.tapes.compact(tape.name, reason="threshold")
                compact_cooldown = COMPACT_COOLDOWN_STEPS

        if can_overflow_compact and _is_context_length_error(error):
            logger.warning("compaction: context overflow, triggering compaction. tape={} step={}", tape.name, step)
            await self.tapes.compact(tape.name, reason="overflow")
            await self.tapes.append_event(tape.name, "loop.step", {
                "step": step,
                "elapsed_ms": elapsed_ms,
                "status": "compaction_overflow",
                "error": error,
                "date": datetime.now(UTC).isoformat(),
            })
            return True, compact_cooldown

        return False, compact_cooldown

    def _load_skills_prompt(self, prompt: str, workspace: Path, allowed_skills: set[str] | None = None) -> str:
        skill_index = {
            skill.name.casefold(): skill
            for skill in discover_skills(workspace)
            if allowed_skills is None or skill.name.casefold() in allowed_skills
        }
        expanded_skills = set(HINT_RE.findall(prompt)) & set(skill_index.keys())
        return render_skills_prompt(list(skill_index.values()), expanded_skills=expanded_skills)

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
        prompt_text = prompt if isinstance(prompt, str) else _extract_text_from_parts(prompt)
        if allowed_tools is not None:
            allowed_tools = resolve_tool_names(allowed_tools)
        if allowed_skills is not None:
            allowed_skills = {name.casefold() for name in allowed_skills}
            tape.context.state["allowed_skills"] = list(allowed_skills)
        if allowed_tools is not None:
            tools = [tool for tool in REGISTRY.values() if tool.name in allowed_tools]
        else:
            tools = list(REGISTRY.values())
        wrapped_tools = tools
        if agent_state is not None:
            wrapped_tools = self._wrap_tools_with_hooks(
                session_id=session_id, tools=tools, agent_state=agent_state,
            )
        async with asyncio.timeout(self.settings.model_timeout_seconds):
            if stream_output:
                return await tape.stream_events_async(
                    prompt=prompt,
                    system_prompt=self._system_prompt(
                        prompt_text, state=tape.context.state, allowed_skills=allowed_skills, tools=tools
                    ),
                    max_tokens=self.settings.max_tokens,
                    tools=model_tools(wrapped_tools),
                    model=model,
                )
            else:
                return await tape.run_tools_async(
                    prompt=prompt,
                    system_prompt=self._system_prompt(
                        prompt_text, state=tape.context.state, allowed_skills=allowed_skills, tools=tools
                    ),
                    max_tokens=self.settings.max_tokens,
                    tools=model_tools(wrapped_tools),
                    model=model,
                )

    def _build_guidelines(self, tools: Iterable[Tool]) -> str:
        tool_names = {t.name for t in tools}
        lines: list[str] = []
        if "bash" in tool_names and "read" in tool_names:
            lines.append("Prefer read tool over bash for reading files")
        if "bash" in tool_names and "write" in tool_names:
            lines.append("Prefer write/edit tools over bash for file modifications")
        lines.append("Be concise in your responses")
        lines.append("Show file paths clearly when working with files")
        return "<guidelines>\n" + "\n".join(f"- {line}" for line in lines) + "\n</guidelines>"

    def _system_prompt(
        self, prompt: str, state: State, allowed_skills: set[str] | None = None, tools: Iterable[Tool] | None = None
    ) -> str:
        resolved_tools = list(tools if tools is not None else REGISTRY.values())
        blocks: list[str] = []
        if result := self.framework.get_system_prompt(prompt=prompt, state=state):
            blocks.append(result)
        blocks.append(self._build_guidelines(resolved_tools))
        tools_prompt = render_tools_prompt(resolved_tools)
        if tools_prompt:
            blocks.append(tools_prompt)
        workspace = workspace_from_state(state)
        if skills_prompt := self._load_skills_prompt(prompt, workspace, allowed_skills):
            blocks.append(skills_prompt)
        return "\n\n".join(blocks)

    def _wrap_tools_with_hooks(  # noqa: C901
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

            def _make_wrapped(  # noqa: C901
                orig: Any = original,
                tname: str = tool_name,
            ) -> Any:
                async def wrapped_handler(*args: Any, **kwargs: Any) -> Any:  # noqa: C901
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

                    for r in before_results:
                        if isinstance(r, dict) and r.get("block"):
                            raise RepublicError(
                                kind=ErrorKind.TOOL,
                                message=r.get("reason", "blocked by plugin"),
                            )

                    # --- original handler ---
                    is_error = False
                    result: Any = None
                    try:
                        result = await orig(*args, **kwargs)
                    except Exception as exc:
                        is_error = True
                        result = exc

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


@dataclass(frozen=True)
class _ToolAutoOutcome:
    kind: str
    text: str = ""
    error: str = ""


def _resolve_final_data(final_data: dict[str, Any], error: RepublicError | None) -> _ToolAutoOutcome:
    if final_data.get("tool_calls") or final_data.get("tool_results"):
        return _ToolAutoOutcome(kind="continue")
    if (text := final_data.get("text")) is not None:
        return _ToolAutoOutcome(kind="text", text=text)
    error_message = error.message if error else ""
    return _ToolAutoOutcome(kind="error", error=error_message or "unknown error")


def _resolve_tool_auto_result(output: ToolAutoResult) -> _ToolAutoOutcome:
    if output.kind == "text":
        return _ToolAutoOutcome(kind="text", text=output.text or "")
    if output.kind == "tools" or output.tool_calls or output.tool_results:
        return _ToolAutoOutcome(kind="continue")
    if output.error is None:
        return _ToolAutoOutcome(kind="error", error="tool_auto_error: unknown")
    error_kind = getattr(output.error.kind, "value", str(output.error.kind))
    return _ToolAutoOutcome(kind="error", error=f"{error_kind}: {output.error.message}")


def _build_llm(settings: AgentSettings, tape_store: AsyncTapeStore, tape_context: TapeContext) -> LLM:
    from republic.auth.openai_codex import openai_codex_oauth_resolver

    return LLM(
        settings.model,
        api_key=settings.api_key,
        api_base=settings.api_base,
        fallback_models=settings.fallback_models,
        api_key_resolver=openai_codex_oauth_resolver(),
        tape_store=tape_store,
        client_args=settings.client_args,
        api_format=settings.api_format,
        context=tape_context,
        verbose=settings.verbose,
    )


@dataclass(frozen=True)
class Args:
    positional: list[str]
    kwargs: dict[str, Any]


def _parse_internal_command(line: str) -> tuple[str, list[str]]:
    body = line.strip()
    words = shlex.split(body)
    if not words:
        return "", []
    return words[0], words[1:]


def _parse_args(args_tokens: list[str]) -> Args:
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    first_kwarg = False
    for token in args_tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key] = value
            first_kwarg = True
        elif first_kwarg:
            raise ValueError(f"positional argument '{token}' cannot appear after keyword arguments")
        else:
            positional.append(token)
    return Args(positional=positional, kwargs=kwargs)


def _is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(_CONTEXT_LENGTH_PATTERNS.search(error_msg))


def _extract_text_from_parts(parts: list[dict]) -> str:
    """Extract text content from multimodal content parts."""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")

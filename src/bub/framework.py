"""Hook-first Bub framework runtime."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
from dotenv import load_dotenv
from loguru import logger
from republic import AsyncTapeStore, RepublicError, TapeContext
from republic.core.errors import ErrorKind
from republic.tape import TapeStore

from bub import configure
from bub.envelope import content_of, field_of, unpack_batch
from bub.hook_runtime import _SKIP_VALUE, HookRuntime
from bub.plugin_manager import PluginManager, PluginStatus
from bub.turn_admission import AdmitDecision, SteeringBuffer, TurnSnapshot
from bub.types import Envelope, MessageHandler, OutboundChannelRouter, TurnResult

if TYPE_CHECKING:
    from bub.channels.base import Channel


load_dotenv()
DEFAULT_HOME = Path.home() / ".bub"
DEFAULT_CONFIG_FILE = (DEFAULT_HOME / "config.yml").resolve()


class BubFramework:
    """Minimal framework core. Everything grows from hook skills."""

    def __init__(self, config_file: Path = DEFAULT_CONFIG_FILE) -> None:
        self.workspace = Path.cwd().resolve()
        self.config_file = config_file.resolve()

        plugin_dirs = [
            self.workspace / ".bub" / "plugins",
        ]
        try:
            plugin_dirs.append(Path.home() / ".bub" / "plugins")
        except RuntimeError:
            pass

        self._plugin_mgr = PluginManager(self, plugin_dirs)
        self._hook_runtime = HookRuntime(self._plugin_mgr.pluggy_manager)
        self._outbound_router: OutboundChannelRouter | None = None
        self._steering_buffers: dict[str, SteeringBuffer] = {}
        self._tape_store: TapeStore | AsyncTapeStore | None = None
        configure.load(self.config_file)

    def load_hooks(self) -> None:
        self._plugin_mgr.load_builtin()
        self._plugin_mgr.load_all_external()

    def reload_hooks(self) -> dict[str, PluginStatus]:
        return self._plugin_mgr.reload_all()

    @property
    def _plugin_manager(self):
        return self._plugin_mgr.pluggy_manager

    @property
    def _plugin_status(self) -> dict[str, PluginStatus]:
        return self._plugin_mgr.get_status()

    def create_cli_app(self) -> typer.Typer:
        """Create CLI app by collecting commands from hooks. Can be used for custom CLI entry point."""
        app = typer.Typer(name="bub", help="Batteries-included, hook-first AI framework", add_completion=False)

        @app.callback(invoke_without_command=True)
        def _main(
            ctx: typer.Context,
            workspace: str | None = typer.Option(None, "--workspace", "-w", help="Path to the workspace"),
        ) -> None:
            if workspace:
                self.workspace = Path(workspace).resolve()
            ctx.obj = self

        self._hook_runtime.call_many_sync("register_cli_commands", app=app)
        return app

    async def process_inbound(self, inbound: Envelope, stream_output: bool = False) -> TurnResult:
        """Run one inbound message through hooks and return turn result."""

        try:
            session_id = await self.resolve_session(inbound)
            if isinstance(inbound, dict):
                inbound.setdefault("session_id", session_id)
            state = {"_runtime_workspace": str(self.workspace), "_runtime_steering": self.steering(session_id)}
            for hook_state in reversed(
                await self._hook_runtime.call_many("load_state", message=inbound, session_id=session_id)
            ):
                if isinstance(hook_state, dict):
                    state.update(hook_state)
            prompt = await self._hook_runtime.call_first(
                "build_prompt", message=inbound, session_id=session_id, state=state
            )
            if not prompt:
                prompt = content_of(inbound)
            model_output = ""
            try:
                model_output = await self._run_model(inbound, prompt, session_id, state, stream_output)
            finally:
                await self._hook_runtime.call_many(
                    "save_state",
                    session_id=session_id,
                    state=state,
                    message=inbound,
                    model_output=model_output,
                )

            outbounds = await self._collect_outbounds(inbound, session_id, state, model_output)
            for outbound in outbounds:
                await self._hook_runtime.call_many("dispatch_outbound", message=outbound)
            return TurnResult(session_id=session_id, prompt=prompt, model_output=model_output, outbounds=outbounds)
        except Exception as exc:
            logger.exception("Error processing inbound message")
            await self._hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            raise

    async def resolve_session(self, message: Envelope) -> str:
        """Resolve the canonical session id for a message."""

        resolved = await self._hook_runtime.call_first("resolve_session", message=message)
        return str(resolved or self._default_session_id(message))

    async def _run_model(
        self,
        inbound: Envelope,
        prompt: str | list[dict],
        session_id: str,
        state: dict[str, Any],
        stream_output: bool,
    ) -> str:
        if not stream_output:
            output = await self._hook_runtime.run_model(prompt=prompt, session_id=session_id, state=state)
            if output is None:
                await self._hook_runtime.notify_error(
                    stage="run_model",
                    error=RuntimeError("no model skill returned output"),
                    message=inbound,
                )
                return prompt if isinstance(prompt, str) else content_of(inbound)
            return output
        stream = await self._hook_runtime.run_model_stream(prompt=prompt, session_id=session_id, state=state)
        if stream is None:
            await self._hook_runtime.notify_error(
                stage="run_model",
                error=RuntimeError("no model skill returned output"),
                message=inbound,
            )
            return prompt if isinstance(prompt, str) else content_of(inbound)
        else:
            parts: list[str] = []
            if self._outbound_router is not None:
                stream = self._outbound_router.wrap_stream(inbound, stream)
            async for event in stream:
                if event.kind == "text":
                    parts.append(str(event.data.get("delta", "")))
                elif event.kind == "error":
                    # Turn "kind" to enum type otherwise the RepublicError's __str__ won't work well
                    data = {
                        **event.data,
                        "kind": ErrorKind(event.data.get("kind", "unknown")),
                    }
                    await self._hook_runtime.notify_error(
                        stage="run_model", error=RepublicError(**data), message=inbound
                    )
            return "".join(parts)

    def hook_report(self) -> dict[str, list[str]]:
        """Return hook implementation summary for diagnostics."""

        return self._hook_runtime.hook_report()

    def bind_outbound_router(self, router: OutboundChannelRouter | None) -> None:
        self._outbound_router = router

    async def dispatch_via_router(self, message: Envelope) -> bool:
        if self._outbound_router is None:
            return False
        return await self._outbound_router.dispatch_output(message)

    async def quit_via_router(self, session_id: str) -> None:
        if self._outbound_router is not None:
            await self._outbound_router.quit(session_id)

    async def admit_message(self, *, session_id: str, message: Envelope, turn: TurnSnapshot) -> AdmitDecision | None:
        return cast(
            "AdmitDecision | None",
            await self._hook_runtime.call_first(
                "admit_message",
                session_id=session_id,
                message=message,
                turn=turn,
            ),
        )

    def steering(self, session_id: str) -> SteeringBuffer:
        buffer = self._steering_buffers.get(session_id)
        if buffer is None:
            buffer = SteeringBuffer(session_id=session_id)
            self._steering_buffers[session_id] = buffer
        return buffer

    def clear_steering(self, session_id: str) -> None:
        self._steering_buffers.pop(session_id, None)

    @staticmethod
    def _default_session_id(message: Envelope) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None:
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    async def _collect_outbounds(
        self,
        message: Envelope,
        session_id: str,
        state: dict[str, Any],
        model_output: str,
    ) -> list[Envelope]:
        batches = await self._hook_runtime.call_many(
            "render_outbound",
            message=message,
            session_id=session_id,
            state=state,
            model_output=model_output,
        )
        outbounds: list[Envelope] = []
        for batch in batches:
            outbounds.extend(unpack_batch(batch))
        if outbounds:
            return outbounds

        fallback: dict[str, Any] = {
            "content": model_output,
            "session_id": session_id,
        }
        channel = field_of(message, "channel")
        chat_id = field_of(message, "chat_id")
        if channel is not None:
            fallback["channel"] = channel
        if chat_id is not None:
            fallback["chat_id"] = chat_id
        return [fallback]

    def get_channels(self, message_handler: MessageHandler) -> dict[str, Channel]:
        channels: dict[str, Channel] = {}
        for result in self._hook_runtime.call_many_sync("provide_channels", message_handler=message_handler):
            for channel in result:
                if channel.name not in channels:
                    channels[channel.name] = channel
        return channels

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator[contextlib.AsyncExitStack, None]:
        async with contextlib.AsyncExitStack() as stack:
            tape_store = self._hook_runtime.call_first_sync("provide_tape_store")
            # Allow plugins to return either TapeStore/AsyncTapeStore instances or context managers for them
            # This benefits plugins that need to initialize and clean up resources with the tape store.
            if isinstance(tape_store, AsyncIterator):
                tape_store = await stack.enter_async_context(contextlib.asynccontextmanager(lambda: tape_store)())
            elif isinstance(tape_store, Iterator):
                tape_store = stack.enter_context(contextlib.contextmanager(lambda: tape_store)())
            self._tape_store = tape_store
            try:
                yield stack
            finally:
                self._tape_store = None

    def get_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        return self._tape_store

    def get_system_prompt(self, prompt: str | list[dict], state: dict[str, Any]) -> str:
        return "\n\n".join(
            result
            for result in reversed(self._hook_runtime.call_many_sync("system_prompt", prompt=prompt, state=state))
            if result
        )

    def build_tape_context(self) -> TapeContext:
        return self._hook_runtime.call_first_sync("build_tape_context")

    def collect_onboard_config(self) -> dict[str, Any]:
        current_config: dict[str, Any] = {}

        for impl in reversed(list(self._hook_runtime._iter_hookimpls("onboard_config"))):
            result = self._hook_runtime._invoke_impl_sync(
                hook_name="onboard_config",
                impl=impl,
                call_kwargs={"current_config": current_config},
                kwargs={"current_config": current_config},
            )
            if result is _SKIP_VALUE:
                continue
            if result is None:
                continue
            if not isinstance(result, dict):
                raise TypeError("hook.onboard_config must return dict or None")
            configure.merge(current_config, result)
        return configure.validate(current_config)

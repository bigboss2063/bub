# Hooks Reference

All hooks are defined in `bub.hookspecs.BubHookSpecs`. Import the marker:

```python
from bub.hookspecs import hookimpl
```

## Hook Types

- **firstresult**: framework uses first non-None return; later registrations have higher priority (reversed iteration order).
- **multi (default)**: all implementations run; results collected in list.

## Full Hook List

### `resolve_session(message: Envelope) -> str` [firstresult]

Resolve a session ID from an inbound message. Default: `f"{channel}:{chat_id}"`.

### `build_prompt(message: Envelope, session_id: str, state: State) -> str | list[dict]` [firstresult]

Build the model prompt for a turn. Return a string or OpenAI multimodal content parts list. Default: raw message content.

### `run_model(prompt: str | list[dict], session_id: str, state: State) -> str` [firstresult]

Run the model (non-streaming) and return text output. Mutually exclusive with `run_model_stream`.

### `run_model_stream(prompt: str | list[dict], session_id: str, state: State) -> AsyncStreamEvents` [firstresult]

Run the model and return an async event stream. Mutually exclusive with `run_model`.

### `load_state(message: Envelope, session_id: str) -> State` [multi]

Return a dict merged into the session state. Later plugins override earlier keys. Framework injects `_runtime_workspace` and `_runtime_steering`.

### `save_state(session_id: str, state: State, message: Envelope, model_output: str) -> None` [multi]

Persist state after a turn completes. Runs in a `finally` block (always executes, even on error).

### `render_outbound(message: Envelope, session_id: str, state: State, model_output: str) -> list[Envelope]` [multi]

Create outbound messages from model output. Return a list of dicts/ChannelMessages. If all hooks return empty, framework falls back to a simple `{"content": model_output}` envelope.

### `dispatch_outbound(message: Envelope) -> bool` [multi]

Send an outbound message to external channels. Return `True` if handled.

### `register_cli_commands(app: typer.Typer) -> None` [multi]

Add CLI subcommands to the root Typer app. Called once at startup.

### `onboard_config(current_config: dict) -> dict | None` [multi]

Collect plugin config fragments for `bub install` interactive onboarding. Return a dict to merge.

### `on_error(stage: str, error: Exception, message: Envelope | None) -> None` [multi]

Observe errors from any pipeline stage. Observer failures are swallowed.

### `system_prompt(prompt: str | list[dict], state: State) -> str` [multi]

Return a string to prepend to the system prompt. Multiple results are joined with `\n\n`.

### `provide_tape_store() -> TapeStore | AsyncTapeStore` [firstresult]

Provide the conversation tape storage backend. Default: `FileTapeStore` at `~/.bub/tapes`.

### `provide_channels(message_handler: MessageHandler) -> list[Channel]` [multi]

Return channel adapters (e.g. TelegramChannel, CliChannel). First match wins by name.

### `build_tape_context() -> TapeContext` [firstresult]

Build tape context for the current session.

### `admit_message(session_id: str, message: Envelope, turn: TurnSnapshot) -> AdmitDecision | None` [firstresult]

Control inbound message flow. Return `None` for default concurrent scheduling, or an `AdmitDecision` to drop, queue, or steer.

### `before_tool_call(tool_name: str, arguments: dict, session_id: str) -> dict | None` [multi]

Inspect or block a tool before execution. Return `{"block": True, "reason": "..."}` to prevent.

### `after_tool_call(tool_name: str, arguments: dict, result: Any, session_id: str, is_error: bool) -> dict | None` [multi]

Inspect or override a tool result. Return `{"content": ..., "is_error": True, "terminate": True}` to override.

## Key Types

- `Envelope`: `dict[str, Any]` — generic message container.
- `State`: `dict[str, Any]` — mutable session state.
- `MessageHandler`: `Callable[[Envelope], Awaitable[None]]` — callback for channels.
- `Channel`: abstract base with `name`, `start()`, `stop()`, `send()`.
- `AdmitDecision`: enum with process/drop/follow_up/steer actions.

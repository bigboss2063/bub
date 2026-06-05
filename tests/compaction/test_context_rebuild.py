from __future__ import annotations

from republic import TapeEntry

from bub.builtin.context import _select_messages, default_tape_context


def _msg_entry(entry_id: int, role: str, content: str) -> TapeEntry:
    return TapeEntry(id=entry_id, kind="message", payload={"role": role, "content": content})


def _anchor_entry(entry_id: int, name: str, state: dict | None = None) -> TapeEntry:
    payload: dict = {"name": name}
    if state is not None:
        payload["state"] = state
    return TapeEntry(id=entry_id, kind="anchor", payload=payload)


def test_select_messages_without_compaction() -> None:
    ctx = default_tape_context()
    entries = [
        _msg_entry(1, "user", "hello"),
        _msg_entry(2, "assistant", "hi"),
    ]
    messages = _select_messages(entries, ctx)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"


def test_select_messages_compact_anchor_renders_summary() -> None:
    ctx = default_tape_context()
    entries = [
        _anchor_entry(1, "compact", {"summary": "## Goal\nTest task", "tokens_before": 500}),
        _msg_entry(2, "user", "new message"),
        _msg_entry(3, "assistant", "new response"),
    ]
    messages = _select_messages(entries, ctx)
    assert messages[0]["role"] == "user"
    assert "<compact-summary>" in messages[0]["content"]
    assert "## Goal\nTest task" in messages[0]["content"]
    assert "<tokens-before>500</tokens-before>" in messages[0]["content"]
    assert len(messages) == 3
    assert messages[1]["content"] == "new message"
    assert messages[2]["content"] == "new response"


def test_select_messages_generic_anchor_rendered_as_plain() -> None:
    ctx = default_tape_context()
    entries = [
        _anchor_entry(1, "phase-1", {"note": "checkpoint"}),
        _msg_entry(2, "user", "hello"),
    ]
    messages = _select_messages(entries, ctx)
    assert len(messages) == 2
    assert "[Anchor created: phase-1]" in messages[0]["content"]


def test_select_messages_compact_anchor_without_retained() -> None:
    ctx = default_tape_context()
    entries = [
        _anchor_entry(1, "compact", {"summary": "summary text", "tokens_before": 100}),
    ]
    messages = _select_messages(entries, ctx)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "<compact-summary>" in messages[0]["content"]

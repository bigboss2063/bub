from __future__ import annotations

from dataclasses import replace

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


def test_select_messages_with_compaction_summary() -> None:
    ctx = default_tape_context()
    ctx = replace(ctx, state={
        "compaction_summary": "## Goal\nTest task",
        "compaction_last_entry_before": 2,
        "compaction_tokens_before": 500,
    })
    entries = [
        _msg_entry(1, "user", "old message"),
        _msg_entry(2, "assistant", "old response"),
        _msg_entry(3, "user", "new message"),
        _msg_entry(4, "assistant", "new response"),
    ]
    messages = _select_messages(entries, ctx)
    assert messages[0]["role"] == "user"
    assert "<compaction-summary>" in messages[0]["content"]
    assert "## Goal\nTest task" in messages[0]["content"]
    assert "<tokens-before>500</tokens-before>" in messages[0]["content"]
    assert len(messages) == 3
    assert messages[1]["content"] == "new message"
    assert messages[2]["content"] == "new response"


def test_select_messages_compaction_filters_old_entries() -> None:
    ctx = default_tape_context()
    ctx = replace(ctx, state={
        "compaction_summary": "summary",
        "compaction_last_entry_before": 3,
    })
    entries = [
        _msg_entry(1, "user", "old1"),
        _msg_entry(2, "assistant", "old2"),
        _msg_entry(3, "user", "old3"),
        _msg_entry(4, "assistant", "kept"),
        _msg_entry(5, "user", "new"),
    ]
    messages = _select_messages(entries, ctx)
    assert messages[0]["role"] == "user"
    assert "<compaction-summary>" in messages[0]["content"]
    assert len(messages) == 3
    contents = [m["content"] for m in messages[1:]]
    assert "old1" not in contents
    assert "old2" not in contents
    assert "old3" not in contents
    assert "kept" in contents
    assert "new" in contents


def test_select_messages_old_anchor_rendered_as_plain() -> None:
    ctx = default_tape_context()
    entries = [
        _anchor_entry(1, "phase-1", {"note": "checkpoint"}),
        _msg_entry(2, "user", "hello"),
    ]
    messages = _select_messages(entries, ctx)
    assert len(messages) == 2
    assert "[Anchor created: phase-1]" in messages[0]["content"]


def test_select_messages_compaction_anchor_rendered_as_plain_after_compaction() -> None:
    ctx = default_tape_context()
    ctx = replace(ctx, state={
        "compaction_summary": "summary",
        "compaction_last_entry_before": 1,
    })
    entries = [
        _anchor_entry(1, "compaction/v1", {"summary": "old"}),
        _msg_entry(2, "user", "after compaction"),
    ]
    messages = _select_messages(entries, ctx)
    assert messages[0]["role"] == "user"
    assert "<compaction-summary>" in messages[0]["content"]
    assert len(messages) == 2
    assert messages[1]["content"] == "after compaction"

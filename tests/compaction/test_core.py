from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from republic import TapeEntry

from bub.builtin.compaction.core import (
    compact,
    generate_summary,
    should_compact,
)
from bub.builtin.compaction.types import CompactionSettings, FileOperations


def test_should_compact_below_threshold() -> None:
    settings = CompactionSettings(reserve_tokens=1000)
    assert should_compact(500, 2000, settings) is False


def test_should_compact_above_threshold() -> None:
    settings = CompactionSettings(reserve_tokens=1000)
    assert should_compact(1500, 2000, settings) is True


def test_should_compact_disabled() -> None:
    settings = CompactionSettings(enabled=False, reserve_tokens=1000)
    assert should_compact(1500, 2000, settings) is False


@pytest.mark.asyncio
async def test_generate_summary_initial() -> None:
    llm = MagicMock()
    llm.chat_async = AsyncMock(return_value="## Goal\nTest summary")
    entries = [
        TapeEntry(id=1, kind="message", payload={"role": "user", "content": "hello"}),
        TapeEntry(id=2, kind="message", payload={"role": "assistant", "content": "hi"}),
    ]
    result = await generate_summary(llm, entries, FileOperations())
    assert result == "## Goal\nTest summary"
    llm.chat_async.assert_called_once()
    call_kwargs = llm.chat_async.call_args
    assert call_kwargs.kwargs.get("tape") is None


@pytest.mark.asyncio
async def test_generate_summary_incremental() -> None:
    llm = MagicMock()
    llm.chat_async = AsyncMock(return_value="## Goal\nUpdated summary")
    entries = [
        TapeEntry(id=1, kind="message", payload={"role": "user", "content": "more work"}),
    ]
    result = await generate_summary(llm, entries, FileOperations(), previous_summary="old summary")
    assert result == "## Goal\nUpdated summary"
    call_args = llm.chat_async.call_args
    assert "<previous-summary>" in call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")


@pytest.mark.asyncio
async def test_generate_summary_with_instructions() -> None:
    llm = MagicMock()
    llm.chat_async = AsyncMock(return_value="summary")
    entries = [
        TapeEntry(id=1, kind="message", payload={"role": "user", "content": "hello"}),
    ]
    await generate_summary(llm, entries, FileOperations(), instructions="focus on errors")
    call_args = llm.chat_async.call_args
    prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
    assert "Additional focus: focus on errors" in prompt


@pytest.mark.asyncio
async def test_compact_returns_none_when_nothing_to_cut() -> None:
    llm = MagicMock()
    llm.chat_async = AsyncMock(return_value="summary")
    entries = [
        TapeEntry(id=1, kind="message", payload={"role": "user", "content": "short"}),
    ]
    settings = CompactionSettings(keep_recent_tokens=50000)
    result = await compact(llm, "tape1", entries, settings)
    assert result is None
    llm.chat_async.assert_not_called()


@pytest.mark.asyncio
async def test_compact_writes_anchor_and_returns_result() -> None:
    llm = MagicMock()
    llm.chat_async = AsyncMock(return_value="## Goal\nTest")
    entries = [
        TapeEntry(id=1, kind="message", payload={"role": "user", "content": "x" * 200}),
        TapeEntry(id=2, kind="message", payload={"role": "assistant", "content": "x" * 200}),
        TapeEntry(id=3, kind="message", payload={"role": "user", "content": "x" * 200}),
        TapeEntry(id=4, kind="message", payload={"role": "assistant", "content": "x" * 200}),
        TapeEntry(id=5, kind="message", payload={"role": "user", "content": "x" * 200}),
        TapeEntry(id=6, kind="message", payload={"role": "assistant", "content": "x" * 200}),
    ]
    settings = CompactionSettings(keep_recent_tokens=50)
    anchor_calls: list[tuple[str, dict]] = []

    async def fake_write_anchor(name: str, state: dict) -> None:
        anchor_calls.append((name, state))

    result = await compact(llm, "tape1", entries, settings, reason="threshold", write_anchor=fake_write_anchor)
    assert result is not None
    assert result.summary == "## Goal\nTest"
    assert result.cut_index > 0
    assert result.tokens_before > 0
    assert len(anchor_calls) == 1
    assert anchor_calls[0][0] == "compact"
    assert anchor_calls[0][1]["trigger"] == "threshold"

from __future__ import annotations

from republic import TapeEntry

from bub.builtin.compaction.core import find_cut_point


def _msg_entry(entry_id: int, role: str, content: str) -> TapeEntry:
    return TapeEntry(id=entry_id, kind="message", payload={"role": role, "content": content})


def _tool_call_entry(entry_id: int) -> TapeEntry:
    return TapeEntry(
        id=entry_id,
        kind="tool_call",
        payload={"calls": [{"id": f"c{entry_id}", "function": {"name": "bash", "arguments": "{}"}}]},
    )


def _tool_result_entry(entry_id: int) -> TapeEntry:
    return TapeEntry(id=entry_id, kind="tool_result", payload={"results": ["ok"]})


def test_find_cut_point_basic() -> None:
    entries = [
        _msg_entry(1, "user", "x" * 100),
        _msg_entry(2, "assistant", "x" * 100),
        _msg_entry(3, "user", "x" * 100),
        _msg_entry(4, "assistant", "x" * 100),
        _msg_entry(5, "user", "x" * 100),
        _msg_entry(6, "assistant", "x" * 100),
    ]
    result = find_cut_point(entries, boundary_start=0, keep_recent_tokens=50)
    assert result.cut_index > 0
    assert result.cut_index < len(entries)
    assert result.is_split_turn is False


def test_find_cut_point_preserves_tool_pairs() -> None:
    entries = [
        _msg_entry(1, "user", "x" * 100),
        _msg_entry(2, "assistant", "x" * 100),
        _tool_call_entry(3),
        _tool_result_entry(4),
        _msg_entry(5, "user", "x" * 100),
        _msg_entry(6, "assistant", "x" * 100),
    ]
    result = find_cut_point(entries, boundary_start=0, keep_recent_tokens=50)
    cut = result.cut_index
    has_tool_call_before = any(entries[i].kind == "tool_call" for i in range(cut - 1, cut))
    has_tool_result_at = cut < len(entries) and entries[cut].kind == "tool_result" if cut < len(entries) else False
    assert not (has_tool_call_before and has_tool_result_at)


def test_find_cut_point_with_boundary_start() -> None:
    entries = [
        _msg_entry(1, "user", "x" * 100),
        _msg_entry(2, "assistant", "x" * 100),
        _msg_entry(3, "user", "x" * 100),
        _msg_entry(4, "assistant", "x" * 100),
        _msg_entry(5, "user", "x" * 100),
        _msg_entry(6, "assistant", "x" * 100),
    ]
    result = find_cut_point(entries, boundary_start=2, keep_recent_tokens=50)
    assert result.cut_index >= 2


def test_find_cut_point_split_turn() -> None:
    entries = [
        _msg_entry(1, "user", "x" * 100),
        _msg_entry(2, "assistant", "x" * 200),
        _tool_call_entry(3),
        _tool_result_entry(4),
        _msg_entry(5, "assistant", "x" * 200),
        _msg_entry(6, "user", "x" * 100),
        _msg_entry(7, "assistant", "x" * 200),
    ]
    result = find_cut_point(entries, boundary_start=0, keep_recent_tokens=100)
    if result.is_split_turn:
        assert result.turn_start_index is not None
        assert result.turn_start_index < result.cut_index


def test_find_cut_point_nothing_to_cut() -> None:
    entries = [
        _msg_entry(1, "user", "short"),
    ]
    result = find_cut_point(entries, boundary_start=0, keep_recent_tokens=50000)
    assert result.cut_index == 0


def test_find_cut_point_all_entries_are_recent() -> None:
    entries = [
        _msg_entry(1, "user", "x" * 40),
        _msg_entry(2, "assistant", "x" * 40),
    ]
    result = find_cut_point(entries, boundary_start=0, keep_recent_tokens=50000)
    assert result.cut_index == 0

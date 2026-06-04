from __future__ import annotations

import pytest

from bub.builtin.compaction.utils import (
    estimate_tokens,
    extract_file_operations,
    serialize_messages,
    truncate_tool_result,
)


def test_estimate_tokens_string_content() -> None:
    msg = {"content": "hello world!!"}  # 13 chars -> 3 tokens
    assert estimate_tokens(msg) == 3


def test_estimate_tokens_empty_content() -> None:
    msg = {"content": ""}
    assert estimate_tokens(msg) == 0


def test_estimate_tokens_list_content() -> None:
    msg = {"content": [{"text": "hello"}, {"text": "world"}]}
    assert estimate_tokens(msg) == 2  # 10 chars -> 2


def test_estimate_tokens_no_content() -> None:
    msg = {"role": "user"}
    assert estimate_tokens(msg) == 0


def test_truncate_tool_result_short() -> None:
    assert truncate_tool_result("short") == "short"


def test_truncate_tool_result_long() -> None:
    content = "x" * 3000
    result = truncate_tool_result(content)
    assert len(result) < len(content)
    assert result.endswith("... (truncated)")


def test_serialize_messages_user_and_assistant() -> None:
    entries = [
        _make_message_entry({"role": "user", "content": "hello"}),
        _make_message_entry({"role": "assistant", "content": "hi there"}),
    ]
    result = serialize_messages(entries)
    assert "[User]: hello" in result
    assert "[Assistant]: hi there" in result


def test_serialize_messages_tool_calls() -> None:
    entries = [
        _make_tool_call_entry([
            {"id": "c1", "function": {"name": "fs.read", "arguments": '{"path": "a.py"}'}},
        ]),
    ]
    result = serialize_messages(entries)
    assert "[Assistant tool calls]: fs.read" in result


def test_serialize_messages_tool_results() -> None:
    entries = [
        _make_tool_result_entry(["file content here"]),
    ]
    result = serialize_messages(entries)
    assert "[Tool result]: file content here" in result


def test_serialize_messages_truncates_tool_results() -> None:
    long_content = "x" * 3000
    entries = [_make_tool_result_entry([long_content])]
    result = serialize_messages(entries)
    assert "... (truncated)" in result


def test_extract_file_operations_reads() -> None:
    entries = [
        _make_tool_call_entry([
            {"id": "c1", "function": {"name": "fs.read", "arguments": '{"path": "src/main.py"}'}},
        ]),
    ]
    ops = extract_file_operations(entries)
    assert "src/main.py" in ops.read
    assert len(ops.modified) == 0


def test_extract_file_operations_writes() -> None:
    entries = [
        _make_tool_call_entry([
            {"id": "c1", "function": {"name": "fs.write", "arguments": '{"path": "out.txt"}'}},
            {"id": "c2", "function": {"name": "fs.edit", "arguments": '{"path": "src/app.py"}'}},
        ]),
    ]
    ops = extract_file_operations(entries)
    assert "out.txt" in ops.modified
    assert "src/app.py" in ops.modified


def test_extract_file_operations_ignores_other_tools() -> None:
    entries = [
        _make_tool_call_entry([
            {"id": "c1", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}},
        ]),
    ]
    ops = extract_file_operations(entries)
    assert len(ops.read) == 0
    assert len(ops.modified) == 0


def _make_message_entry(payload: dict) -> object:
    from republic import TapeEntry
    return TapeEntry(id=0, kind="message", payload=payload)


def _make_tool_call_entry(calls: list[dict]) -> object:
    from republic import TapeEntry
    return TapeEntry(id=0, kind="tool_call", payload={"calls": calls})


def _make_tool_result_entry(results: list) -> object:
    from republic import TapeEntry
    return TapeEntry(id=0, kind="tool_result", payload={"results": results})

# Bub Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the reactive-only `auto_handoff` mechanism with a proactive compaction pipeline that summarizes truncated history and injects it back into context.

**Architecture:** A new `src/bub/builtin/compaction/` module handles cut-point selection, summary generation (via off-tape LLM calls), and anchor writing. The agent loop triggers compaction proactively (threshold) or reactively (overflow). The context selector (`context.py`) is enhanced to detect compaction metadata in `TapeContext.state` and render the summary as a user message before the kept entries.

**Tech Stack:** Python 3.12+, republic (LLM/Tape/TapeEntry), pydantic-settings, pytest, asyncio

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `src/bub/builtin/compaction/__init__.py` | Public API re-exports |
| Create | `src/bub/builtin/compaction/types.py` | `CompactionSettings`, `CompactionResult`, `CutPointResult`, `FileOperations` |
| Create | `src/bub/builtin/compaction/utils.py` | Token estimation, message serialization, file operation extraction |
| Create | `src/bub/builtin/compaction/core.py` | `should_compact()`, `find_cut_point()`, `generate_summary()`, `generate_turn_prefix_summary()`, `compact()` |
| Modify | `src/bub/builtin/tape.py:36-130` | Add `TapeService.compact()` method |
| Modify | `src/bub/builtin/context.py:18-33` | Detect compaction state, render summary, filter entries |
| Modify | `src/bub/builtin/agent.py:259-369` | Replace auto_handoff with compaction in non-streaming loop |
| Modify | `src/bub/builtin/agent.py:371-487` | Replace auto_handoff with compaction in streaming loop |
| Modify | `src/bub/builtin/tools.py:227-232` | Add `tape.compact` tool, deprecate `tape.handoff` |
| Create | `tests/compaction/__init__.py` | Test package marker |
| Create | `tests/compaction/test_types.py` | Settings and type tests |
| Create | `tests/compaction/test_utils.py` | Token estimation, serialization, file ops tests |
| Create | `tests/compaction/test_cut_point.py` | Cut point selection tests |
| Create | `tests/compaction/test_context_rebuild.py` | Selector compaction rendering tests |
| Create | `tests/compaction/test_core.py` | Summary generation and compact orchestration tests |
| Modify | `tests/test_builtin_agent.py` | Update agent loop tests for compaction |

---

### Task 1: Compaction Types and Settings

**Files:**
- Create: `src/bub/builtin/compaction/__init__.py`
- Create: `src/bub/builtin/compaction/types.py`
- Test: `tests/compaction/__init__.py`
- Test: `tests/compaction/test_types.py`

- [ ] **Step 1: Create test package and write failing tests for types**

Create `tests/compaction/__init__.py`:
```python
```

Create `tests/compaction/test_types.py`:
```python
from __future__ import annotations

import pytest

from bub.builtin.compaction.types import (
    CompactionResult,
    CompactionSettings,
    CutPointResult,
    FileOperations,
)


def test_compaction_settings_defaults() -> None:
    s = CompactionSettings()
    assert s.enabled is True
    assert s.context_window == 128000
    assert s.reserve_tokens == 16384
    assert s.keep_recent_tokens == 20000


def test_compaction_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_COMPACTION_ENABLED", "false")
    monkeypatch.setenv("BUB_COMPACTION_CONTEXT_WINDOW", "200000")
    monkeypatch.setenv("BUB_COMPACTION_RESERVE_TOKENS", "8192")
    monkeypatch.setenv("BUB_COMPACTION_KEEP_RECENT_TOKENS", "10000")
    s = CompactionSettings()
    assert s.enabled is False
    assert s.context_window == 200000
    assert s.reserve_tokens == 8192
    assert s.keep_recent_tokens == 10000


def test_compaction_result_is_frozen() -> None:
    r = CompactionResult(summary="test", last_entry_before=42, tokens_before=1000)
    assert r.summary == "test"
    assert r.last_entry_before == 42
    assert r.tokens_before == 1000
    with pytest.raises(AttributeError):
        r.summary = "changed"  # type: ignore[misc]


def test_cut_point_result() -> None:
    c = CutPointResult(cut_index=5, is_split_turn=False, turn_start_index=None)
    assert c.cut_index == 5
    assert c.is_split_turn is False
    assert c.turn_start_index is None


def test_cut_point_result_split_turn() -> None:
    c = CutPointResult(cut_index=10, is_split_turn=True, turn_start_index=8)
    assert c.is_split_turn is True
    assert c.turn_start_index == 8


def test_file_operations() -> None:
    f = FileOperations(read={"a.py", "b.py"}, modified={"c.py"})
    assert "a.py" in f.read
    assert "c.py" in f.modified
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compaction/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bub.builtin.compaction'`

- [ ] **Step 3: Create the compaction package and types module**

Create `src/bub/builtin/compaction/__init__.py`:
```python
"""Compaction pipeline for context management."""

from bub.builtin.compaction.types import (
    CompactionResult,
    CompactionSettings,
    CutPointResult,
    FileOperations,
)

__all__ = [
    "CompactionResult",
    "CompactionSettings",
    "CutPointResult",
    "FileOperations",
]
```

Create `src/bub/builtin/compaction/types.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_settings import SettingsConfigDict

from bub import Settings, config


@dataclass(frozen=True)
class CompactionResult:
    """Result of a compaction operation."""

    summary: str
    last_entry_before: int
    tokens_before: int


@dataclass(frozen=True)
class CutPointResult:
    """Result of cut-point selection."""

    cut_index: int
    is_split_turn: bool
    turn_start_index: int | None


@dataclass(frozen=True)
class FileOperations:
    """File operations extracted from tool calls."""

    read: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)


@config()
class CompactionSettings(Settings):
    """Configuration for the compaction pipeline."""

    model_config = SettingsConfigDict(env_prefix="BUB_COMPACTION_", env_parse_none_str="null", extra="ignore")
    enabled: bool = True
    context_window: int = 128000
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compaction/test_types.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/bub/builtin/compaction/ tests/compaction/
git commit -m "feat(compaction): add types and settings module"
```

---

### Task 2: Utility Functions (Token Estimation, Serialization, File Ops)

**Files:**
- Create: `src/bub/builtin/compaction/utils.py`
- Test: `tests/compaction/test_utils.py`

- [ ] **Step 1: Write failing tests for token estimation**

Create `tests/compaction/test_utils.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compaction/test_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bub.builtin.compaction.utils'`

- [ ] **Step 3: Implement utils module**

Create `src/bub/builtin/compaction/utils.py`:
```python
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from republic import TapeEntry

from bub.builtin.compaction.types import FileOperations

TOOL_RESULT_TRUNCATE_AT = 2000


def estimate_tokens(message: dict[str, Any]) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        return sum(len(part.get("text", "")) for part in content if isinstance(part, dict)) // 4
    return 0


def truncate_tool_result(content: str) -> str:
    if len(content) <= TOOL_RESULT_TRUNCATE_AT:
        return content
    return content[:TOOL_RESULT_TRUNCATE_AT] + "... (truncated)"


def serialize_messages(entries: Iterable[TapeEntry]) -> str:
    lines: list[str] = []
    for entry in entries:
        match entry.kind:
            case "message":
                payload = entry.payload
                role = payload.get("role", "")
                content = payload.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                if role == "user":
                    lines.append(f"[User]: {content}")
                elif role == "assistant":
                    lines.append(f"[Assistant]: {content}")
            case "tool_call":
                calls = entry.payload.get("calls", [])
                call_descs: list[str] = []
                for call in calls:
                    func = call.get("function", {})
                    name = func.get("name", "unknown")
                    call_descs.append(name)
                if call_descs:
                    lines.append(f"[Assistant tool calls]: {'; '.join(call_descs)}")
            case "tool_result":
                results = entry.payload.get("results", [])
                for result in results:
                    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                    lines.append(f"[Tool result]: {truncate_tool_result(text)}")
    return "\n".join(lines)


def extract_file_operations(entries: Iterable[TapeEntry]) -> FileOperations:
    read_files: set[str] = set()
    modified_files: set[str] = set()
    for entry in entries:
        if entry.kind != "tool_call":
            continue
        calls = entry.payload.get("calls", [])
        for call in calls:
            func = call.get("function", {})
            name = func.get("name", "")
            args_raw = func.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except (json.JSONDecodeError, TypeError):
                continue
            path = args.get("path", "")
            if not path:
                continue
            if name == "fs.read":
                read_files.add(path)
            elif name in ("fs.write", "fs.edit"):
                modified_files.add(path)
    return FileOperations(read=read_files, modified=modified_files)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compaction/test_utils.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/bub/builtin/compaction/utils.py tests/compaction/test_utils.py
git commit -m "feat(compaction): add token estimation, serialization, and file ops utils"
```

---

### Task 3: Cut Point Selection

**Files:**
- Create: `src/bub/builtin/compaction/core.py` (partial — `find_cut_point` only)
- Test: `tests/compaction/test_cut_point.py`

- [ ] **Step 1: Write failing tests for cut point selection**

Create `tests/compaction/test_cut_point.py`:
```python
from __future__ import annotations

import pytest
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compaction/test_cut_point.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement find_cut_point in core.py**

Create `src/bub/builtin/compaction/core.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from republic import LLM, TapeEntry

from bub.builtin.compaction.types import (
    CompactionResult,
    CompactionSettings,
    CutPointResult,
    FileOperations,
)
from bub.builtin.compaction.utils import (
    estimate_tokens,
    extract_file_operations,
    serialize_messages,
)

logger = logging.getLogger(__name__)

SUMMARIZATION_SYSTEM_PROMPT = """\
You are a context summarization assistant. Your task is to read a conversation \
between a user and an AI coding assistant, then produce a structured summary \
following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the \
conversation. ONLY output the structured summary.\
"""

INITIAL_SUMMARY_INSTRUCTIONS = """\
The messages above are a conversation to summarize. Create a structured context \
checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]

Keep each section concise. Preserve exact file paths, function names, and \
error messages.\
"""

UPDATE_SUMMARY_INSTRUCTIONS = """\
The messages above are NEW conversation messages to incorporate into the \
existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use the same format as above.\
"""

TURN_PREFIX_INSTRUCTIONS = """\
This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent \
work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix.\
"""

SUMMARY_TIMEOUT_SECONDS = 120


def should_compact(total_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    if not settings.enabled:
        return False
    return total_tokens > context_window - settings.reserve_tokens


def find_cut_point(
    entries: list[TapeEntry],
    boundary_start: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    scannable = entries[boundary_start:]
    if not scannable:
        return CutPointResult(cut_index=0, is_split_turn=False, turn_start_index=None)

    accumulated = 0
    cut_offset = 0
    for i in range(len(scannable) - 1, -1, -1):
        entry = scannable[i]
        if entry.kind == "message":
            accumulated += estimate_tokens(entry.payload)
        elif entry.kind == "tool_call":
            accumulated += 50
        elif entry.kind == "tool_result":
            results = entry.payload.get("results", [])
            for r in results:
                text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
                accumulated += len(text) // 4
        if accumulated >= keep_recent_tokens:
            cut_offset = i
            break
    else:
        return CutPointResult(cut_index=0, is_split_turn=False, turn_start_index=None)

    cut_index = boundary_start + cut_offset

    cut_index = _adjust_for_tool_pairs(entries, cut_index, boundary_start)

    is_split_turn = False
    turn_start_index: int | None = None
    if cut_index > boundary_start:
        prev = entries[cut_index - 1] if cut_index > 0 else None
        curr = entries[cut_index] if cut_index < len(entries) else None
        if (
            prev is not None
            and curr is not None
            and prev.kind == "message"
            and prev.payload.get("role") == "assistant"
            and curr.kind == "message"
            and curr.payload.get("role") == "assistant"
        ):
            is_split_turn = True
            turn_start_index = _find_turn_start(entries, cut_index, boundary_start)

    return CutPointResult(
        cut_index=cut_index,
        is_split_turn=is_split_turn,
        turn_start_index=turn_start_index,
    )


def _adjust_for_tool_pairs(entries: list[TapeEntry], cut_index: int, boundary_start: int) -> int:
    if cut_index <= boundary_start or cut_index >= len(entries):
        return cut_index
    if cut_index > 0 and entries[cut_index - 1].kind == "tool_call" and entries[cut_index].kind == "tool_result":
        return cut_index - 1 if cut_index - 1 > boundary_start else cut_index + 1
    return cut_index


def _find_turn_start(entries: list[TapeEntry], cut_index: int, boundary_start: int) -> int:
    for i in range(cut_index - 1, boundary_start - 1, -1):
        entry = entries[i]
        if entry.kind == "message" and entry.payload.get("role") == "user":
            return i
    return boundary_start


async def generate_summary(
    llm: LLM,
    entries: list[TapeEntry],
    file_ops: FileOperations,
    previous_summary: str | None = None,
    instructions: str | None = None,
) -> str:
    conversation_text = serialize_messages(entries)
    parts: list[str] = [f"<conversation>\n{conversation_text}\n</conversation>"]

    if previous_summary:
        parts.append(f"\n<previous-summary>\n{previous_summary}\n</previous-summary>")
        parts.append(f"\n{UPDATE_SUMMARY_INSTRUCTIONS}")
    else:
        parts.append(f"\n{INITIAL_SUMMARY_INSTRUCTIONS}")

    file_ops_xml = _render_file_operations(file_ops)
    if file_ops_xml:
        parts.append(f"\n{file_ops_xml}")

    if instructions:
        parts.append(f"\nAdditional focus: {instructions}")

    prompt = "\n".join(parts)

    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            return await llm.chat_async(
                prompt=prompt,
                system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
                tape=None,
                max_tokens=4096,
            )
    except (TimeoutError, asyncio.TimeoutError):
        return "Compaction failed: summary generation timed out after 120s"
    except Exception as exc:
        logger.warning("summary generation failed: {}", exc)
        return f"Compaction failed: {exc}"


async def generate_turn_prefix_summary(
    llm: LLM,
    entries: list[TapeEntry],
) -> str:
    conversation_text = serialize_messages(entries)
    prompt = f"<conversation>\n{conversation_text}\n</conversation>\n\n{TURN_PREFIX_INSTRUCTIONS}"

    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            return await llm.chat_async(
                prompt=prompt,
                system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
                tape=None,
                max_tokens=4096,
            )
    except (TimeoutError, asyncio.TimeoutError):
        return "Turn prefix summary timed out after 120s"
    except Exception as exc:
        logger.warning("turn prefix summary generation failed: {}", exc)
        return f"Turn prefix summary failed: {exc}"


def _render_file_operations(file_ops: FileOperations) -> str:
    parts: list[str] = []
    if file_ops.read:
        parts.append("<read-files>")
        parts.extend(sorted(file_ops.read))
        parts.append("</read-files>")
    if file_ops.modified:
        parts.append("<modified-files>")
        parts.extend(sorted(file_ops.modified))
        parts.append("</modified-files>")
    return "\n".join(parts) if parts else ""


async def compact(
    llm: LLM,
    tape_name: str,
    entries: list[TapeEntry],
    settings: CompactionSettings,
    *,
    reason: str = "manual",
    instructions: str | None = None,
    write_anchor: Any = None,
) -> CompactionResult | None:
    previous_summary: str | None = None
    boundary_start = 0
    for entry in reversed(entries):
        if entry.kind == "anchor" and entry.payload.get("name", "").startswith("compaction/"):
            state = entry.payload.get("state", {})
            if isinstance(state, dict):
                previous_summary = state.get("summary")
                last_before = state.get("last_entry_before")
                if isinstance(last_before, int):
                    for idx, e in enumerate(entries):
                        if e.id > last_before:
                            boundary_start = idx
                            break
                    else:
                        boundary_start = len(entries)
            break

    cut = find_cut_point(entries, boundary_start, settings.keep_recent_tokens)
    if cut.cut_index == 0:
        return None

    to_summarize = entries[boundary_start:cut.cut_index]
    if not to_summarize:
        return None

    file_ops = extract_file_operations(to_summarize)

    if cut.is_split_turn and cut.turn_start_index is not None:
        history_entries = entries[boundary_start:cut.turn_start_index]
        prefix_entries = entries[cut.turn_start_index:cut.cut_index]
        history_task = generate_summary(llm, history_entries, file_ops, previous_summary, instructions)
        prefix_task = generate_turn_prefix_summary(llm, prefix_entries)
        history_result, prefix_result = await asyncio.gather(history_task, prefix_task)
        summary = f"{history_result}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix_result}"
    else:
        summary = await generate_summary(llm, to_summarize, file_ops, previous_summary, instructions)

    last_entry_before = entries[cut.cut_index - 1].id
    tokens_before = sum(
        estimate_tokens(e.payload) for e in to_summarize if e.kind == "message"
    )

    if write_anchor is not None:
        await write_anchor(
            "compaction/v1",
            state={
                "summary": summary,
                "last_entry_before": last_entry_before,
                "tokens_before": tokens_before,
                "details": {
                    "read_files": sorted(file_ops.read),
                    "modified_files": sorted(file_ops.modified),
                },
                "trigger": reason,
            },
        )

    return CompactionResult(
        summary=summary,
        last_entry_before=last_entry_before,
        tokens_before=tokens_before,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compaction/test_cut_point.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/bub/builtin/compaction/core.py tests/compaction/test_cut_point.py
git commit -m "feat(compaction): add cut point selection algorithm"
```

---

### Task 4: Summary Generation Tests

**Files:**
- Test: `tests/compaction/test_core.py`

- [ ] **Step 1: Write failing tests for should_compact and generate_summary**

Create `tests/compaction/test_core.py`:
```python
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
    assert result.last_entry_before > 0
    assert len(anchor_calls) == 1
    assert anchor_calls[0][0] == "compaction/v1"
    assert anchor_calls[0][1]["trigger"] == "threshold"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/compaction/test_core.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/compaction/test_core.py
git commit -m "test(compaction): add summary generation and compact orchestration tests"
```

---

### Task 5: TapeService.compact() Method

**Files:**
- Modify: `src/bub/builtin/tape.py:1-130`
- Test: (covered by existing `test_core.py` integration)

- [ ] **Step 1: Add CompactionResult import and compact method to TapeService**

In `src/bub/builtin/tape.py`, add the import at the top:
```python
from dataclasses import replace

from bub import ensure_config
from bub.builtin.compaction.types import CompactionResult, CompactionSettings
from bub.builtin.compaction.core import compact as _run_compaction
```

Add the `compact` method to `TapeService` (after the `handoff` method, around line 111):
```python
    async def compact(
        self,
        tape_name: str,
        *,
        reason: str = "manual",
        instructions: str | None = None,
    ) -> CompactionResult | None:
        tape = self._llm.tape(tape_name)
        entries = list(await tape.query_async.all())
        settings = ensure_config(CompactionSettings)

        async def write_anchor(name: str, state: dict) -> None:
            await tape.handoff_async(name, state=state)

        result = await _run_compaction(
            self._llm,
            tape_name,
            entries,
            settings,
            reason=reason,
            instructions=instructions,
            write_anchor=write_anchor,
        )
        if result is not None:
            tape.context = replace(tape.context, state={
                **tape.context.state,
                "compaction_summary": result.summary,
                "compaction_last_entry_before": result.last_entry_before,
                "compaction_tokens_before": result.tokens_before,
            })
        return result
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: All existing tests still PASS

- [ ] **Step 3: Commit**

```bash
git add src/bub/builtin/tape.py
git commit -m "feat(compaction): add TapeService.compact() method"
```

---

### Task 6: Context Selector Compaction Rendering

**Files:**
- Modify: `src/bub/builtin/context.py:1-105`
- Test: `tests/compaction/test_context_rebuild.py`

- [ ] **Step 1: Write failing tests for context rebuild**

Create `tests/compaction/test_context_rebuild.py`:
```python
from __future__ import annotations

from dataclasses import replace

import pytest
from republic import TapeContext, TapeEntry

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
    assert "[Anchor: phase-1]" in messages[0]["content"]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/compaction/test_context_rebuild.py -v`
Expected: FAIL — compaction rendering not implemented yet

- [ ] **Step 3: Modify _select_messages to handle compaction state**

In `src/bub/builtin/context.py`, replace the `_select_messages` function (lines 18-33):
```python
def _select_messages(entries: Iterable[TapeEntry], _context: TapeContext) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []

    entry_list = list(entries)

    compaction_summary = _context.state.get("compaction_summary")
    if compaction_summary:
        last_before = _context.state.get("compaction_last_entry_before", 0)
        tokens_before = _context.state.get("compaction_tokens_before", 0)
        messages.append({
            "role": "user",
            "content": _render_compaction_summary(str(compaction_summary), tokens_before),
        })
        entry_list = [e for e in entry_list if e.id > last_before]

    for entry in entry_list:
        match entry.kind:
            case "anchor":
                _append_anchor_entry(messages, entry)
            case "message":
                _append_message_entry(messages, entry)
            case "tool_call":
                pending_calls = _append_tool_call_entry(messages, entry)
            case "tool_result":
                _append_tool_result_entry(messages, pending_calls, entry)
                pending_calls = []
    return messages


def _render_compaction_summary(summary: str, tokens_before: int) -> str:
    return (
        "<compaction-summary>\n"
        f"<tokens-before>{tokens_before}</tokens-before>\n"
        "<summary>\n"
        f"{summary}\n"
        "</summary>\n"
        "</compaction-summary>"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/compaction/test_context_rebuild.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/bub/builtin/context.py tests/compaction/test_context_rebuild.py
git commit -m "feat(compaction): add compaction summary rendering in context selector"
```

---

### Task 7: Agent Loop Integration (Threshold + Overflow)

**Files:**
- Modify: `src/bub/builtin/agent.py:259-369` (non-streaming)
- Modify: `src/bub/builtin/agent.py:371-487` (streaming)

- [ ] **Step 1: Write failing test for threshold compaction in agent loop**

In `tests/test_builtin_agent.py`, add a test at the end of the file:
```python
@pytest.mark.asyncio
async def test_agent_loop_triggers_compaction_on_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from bub.builtin.compaction.types import CompactionResult

    agent = _make_agent()
    agent.settings = AgentSettings.model_construct(
        model="test:model", api_key="k", api_base="b", max_steps=5, max_tokens=4096,
    )

    fake_tape_service = MagicMock()
    fake_tape_service.compact = AsyncMock(
        return_value=CompactionResult(summary="## Goal\nDone", last_entry_before=10, tokens_before=5000)
    )
    fake_tape_service.append_event = AsyncMock()
    agent.tapes = fake_tape_service

    tape = MagicMock()
    tape.name = "test_tape"
    tape.context = TapeContext(state={})

    from republic import ToolAutoResult
    step_count = 0

    async def fake_run_once(**kwargs: Any) -> ToolAutoResult:
        nonlocal step_count
        step_count += 1
        if step_count == 1:
            return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None, usage=None)
        return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None, usage=None)

    agent._run_once = fake_run_once  # type: ignore[assignment]

    monkeypatch.setattr(agent_module, "_resolve_tool_auto_result", lambda output: agent_module._ToolAutoOutcome(kind="text", text="done"))

    result = await agent._run_tools_with_auto_handoff(tape=tape, prompt="hello")
    assert result == "done"
```

- [ ] **Step 2: Modify the non-streaming agent loop to use compaction**

In `src/bub/builtin/agent.py`, add the import near the top (after line 35):
```python
from bub import ensure_config
from bub.builtin.compaction.core import should_compact
from bub.builtin.compaction.types import CompactionSettings
```

Replace the overflow handling block in `_run_tools_with_auto_handoff` (lines 328-354) with:
```python
            if auto_handoff_remaining > 0 and _is_context_length_error(outcome.error):
                auto_handoff_remaining -= 1
                logger.warning(
                    "compaction: context overflow, triggering compaction. tape={} step={}",
                    tape.name,
                    step,
                )
                await self.tapes.compact(tape.name, reason="overflow")
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "compaction_overflow",
                        "error": outcome.error,
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                next_prompt = prompt
                continue
```

After the `outcome.kind == "continue"` block (around line 326, before the overflow check), add threshold compaction check. Insert this block right after the `continue` on line 326 and before the overflow check:
```python
            # Check for proactive threshold compaction
            compaction_settings = ensure_config(CompactionSettings)
            if compaction_settings.enabled:
                usage = getattr(output, "usage", None)
                total_tokens = getattr(usage, "total_tokens", None) if usage else None
                if total_tokens and should_compact(total_tokens, compaction_settings.context_window, compaction_settings):
                    logger.info("compaction: threshold reached, triggering proactive compaction. tape={}", tape.name)
                    await self.tapes.compact(tape.name, reason="threshold")
```

- [ ] **Step 3: Apply the same changes to the streaming loop**

In `_stream_events_with_auto_handoff` (lines 371-487), apply the same pattern:

Replace the overflow block (lines 446-472) with:
```python
            if auto_handoff_remaining > 0 and _is_context_length_error(outcome.error):
                auto_handoff_remaining -= 1
                logger.warning(
                    "compaction: context overflow, triggering compaction. tape={} step={}",
                    tape.name,
                    step,
                )
                await self.tapes.compact(tape.name, reason="overflow")
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "compaction_overflow",
                        "error": outcome.error,
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                next_prompt = prompt
                continue
```

After the streaming `outcome.kind == "continue"` block (around line 444), add threshold compaction:
```python
            # Check for proactive threshold compaction
            compaction_settings = ensure_config(CompactionSettings)
            if compaction_settings.enabled:
                usage = state.usage
                total_tokens = getattr(usage, "total_tokens", None) if usage else None
                if total_tokens and should_compact(total_tokens, compaction_settings.context_window, compaction_settings):
                    logger.info("compaction: threshold reached, triggering proactive compaction. tape={}", tape.name)
                    await self.tapes.compact(tape.name, reason="threshold")
```

- [ ] **Step 4: Run tests to verify**

Run: `uv run pytest tests/test_builtin_agent.py -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/bub/builtin/agent.py tests/test_builtin_agent.py
git commit -m "feat(compaction): integrate threshold and overflow compaction into agent loop"
```

---

### Task 8: Tool Replacement (tape.compact)

**Files:**
- Modify: `src/bub/builtin/tools.py:227-232`

- [ ] **Step 1: Write failing test for tape.compact tool**

In `tests/test_builtin_tools.py`, add a test:
```python
@pytest.mark.asyncio
async def test_tape_compact_tool(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from bub.builtin.compaction.types import CompactionResult

    agent = MagicMock()
    agent.tapes.compact = AsyncMock(
        return_value=CompactionResult(summary="## Goal\nDone", last_entry_before=5, tokens_before=1000)
    )
    ctx = _tool_context(tmp_path)
    ctx.state["_runtime_agent"] = agent

    from bub.builtin.tools import REGISTRY
    compact_tool = REGISTRY.get("tape.compact")
    assert compact_tool is not None
```

- [ ] **Step 2: Add tape.compact tool and deprecate tape.handoff**

In `src/bub/builtin/tools.py`, after the `tape_handoff` function (line 232), add:
```python
@tool(context=True, name="tape.compact")
async def tape_compact(instructions: str = "", *, context: ToolContext) -> str:
    """Run compaction on the current tape to summarize history and free context space."""
    agent = _get_agent(context)
    result = await agent.tapes.compact(context.tape or "", reason="manual", instructions=instructions or None)
    if result is None:
        return "compaction skipped: nothing to compact"
    return f"compaction complete: {result.tokens_before} tokens summarized"
```

Update the `tape_handoff` docstring to mark it deprecated:
```python
@tool(context=True, name="tape.handoff")
async def tape_handoff(name: str = "handoff", summary: str = "", *, context: ToolContext) -> str:
    """DEPRECATED: Use tape.compact instead. Add a handoff anchor to the current tape."""
    agent = _get_agent(context)
    await agent.tapes.handoff(context.tape or "", name=name, state={"summary": summary})
    return f"anchor added: {name}"
```

- [ ] **Step 3: Run tests to verify**

Run: `uv run pytest tests/test_builtin_tools.py -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 5: Run linting and type checking**

Run: `uv run ruff check .`
Run: `uv run mypy src`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/bub/builtin/tools.py tests/test_builtin_tools.py
git commit -m "feat(compaction): add tape.compact tool, deprecate tape.handoff"
```

---

### Task 9: Update __init__.py Exports and Final Verification

**Files:**
- Modify: `src/bub/builtin/compaction/__init__.py`

- [ ] **Step 1: Update compaction __init__.py with full public API**

Replace `src/bub/builtin/compaction/__init__.py`:
```python
"""Compaction pipeline for context management."""

from bub.builtin.compaction.core import (
    compact,
    find_cut_point,
    generate_summary,
    generate_turn_prefix_summary,
    should_compact,
)
from bub.builtin.compaction.types import (
    CompactionResult,
    CompactionSettings,
    CutPointResult,
    FileOperations,
)
from bub.builtin.compaction.utils import (
    estimate_tokens,
    extract_file_operations,
    serialize_messages,
)

__all__ = [
    "CompactionResult",
    "CompactionSettings",
    "CutPointResult",
    "FileOperations",
    "compact",
    "estimate_tokens",
    "extract_file_operations",
    "find_cut_point",
    "generate_summary",
    "generate_turn_prefix_summary",
    "serialize_messages",
    "should_compact",
]
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 3: Run linting**

Run: `uv run ruff check .`
Expected: No errors (fix any that appear)

- [ ] **Step 4: Run type checking**

Run: `uv run mypy src`
Expected: No errors (fix any that appear)

- [ ] **Step 5: Run make check if available**

Run: `make check`
Expected: PASS

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat(compaction): finalize exports and pass all checks"
```

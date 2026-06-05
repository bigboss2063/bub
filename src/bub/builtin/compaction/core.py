from __future__ import annotations

import asyncio
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
    estimate_entry_tokens,
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
        accumulated += estimate_entry_tokens(scannable[i])
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
            result = await llm.chat_async(
                prompt=prompt,
                system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
                tape=None,
                max_tokens=4096,
            )
        return str(result)
    except TimeoutError:
        return "Compaction failed: summary generation timed out after 120s"
    except Exception as exc:
        logger.warning("summary generation failed: %s", exc)
        return f"Compaction failed: {exc}"


async def generate_turn_prefix_summary(
    llm: LLM,
    entries: list[TapeEntry],
) -> str:
    conversation_text = serialize_messages(entries)
    prompt = f"<conversation>\n{conversation_text}\n</conversation>\n\n{TURN_PREFIX_INSTRUCTIONS}"

    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            result = await llm.chat_async(
                prompt=prompt,
                system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
                tape=None,
                max_tokens=4096,
            )
        return str(result)
    except TimeoutError:
        return "Turn prefix summary timed out after 120s"
    except Exception as exc:
        logger.warning("turn prefix summary generation failed: %s", exc)
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


def _find_previous_compaction_boundary(entries: list[TapeEntry]) -> tuple[str | None, int]:
    previous_summary: str | None = None
    boundary_start = 0
    for entry in reversed(entries):
        if entry.kind == "anchor" and entry.payload.get("name", "").startswith("compact"):
            state = entry.payload.get("state", {})
            if isinstance(state, dict):
                previous_summary = state.get("summary")
            # The retained entries are re-appended after the compact anchor,
            # so boundary_start is the index right after it.
            for idx, e in enumerate(entries):
                if e is entry:
                    boundary_start = idx + 1
                    break
            break
    return previous_summary, boundary_start


async def _generate_summary_for_cut(
    llm: LLM,
    entries: list[TapeEntry],
    boundary_start: int,
    cut: CutPointResult,
    file_ops: FileOperations,
    previous_summary: str | None,
    instructions: str | None,
) -> str:
    if cut.is_split_turn and cut.turn_start_index is not None:
        history_entries = entries[boundary_start:cut.turn_start_index]
        prefix_entries = entries[cut.turn_start_index:cut.cut_index]
        history_task = generate_summary(llm, history_entries, file_ops, previous_summary, instructions)
        prefix_task = generate_turn_prefix_summary(llm, prefix_entries)
        history_result, prefix_result = await asyncio.gather(history_task, prefix_task)
        return f"{history_result}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix_result}"
    return await generate_summary(llm, entries, file_ops, previous_summary, instructions)


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
    previous_summary, boundary_start = _find_previous_compaction_boundary(entries)

    cut = find_cut_point(entries, boundary_start, settings.keep_recent_tokens)
    if cut.cut_index == 0:
        return None

    to_summarize = entries[boundary_start:cut.cut_index]
    if not to_summarize:
        return None

    file_ops = extract_file_operations(to_summarize)
    summary = await _generate_summary_for_cut(
        llm, to_summarize, boundary_start, cut, file_ops, previous_summary, instructions
    )

    tokens_before = sum(estimate_entry_tokens(e) for e in to_summarize)

    if write_anchor is not None:
        await write_anchor(
            "compact",
            state={
                "summary": summary,
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
        tokens_before=tokens_before,
        cut_index=cut.cut_index,
    )

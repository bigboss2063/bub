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

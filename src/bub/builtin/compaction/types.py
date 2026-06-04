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

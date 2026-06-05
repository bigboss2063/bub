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
    r = CompactionResult(summary="test", cut_index=4, tokens_before=1000)
    assert r.summary == "test"
    assert r.cut_index == 4
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

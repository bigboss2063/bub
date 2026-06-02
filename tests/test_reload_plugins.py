"""Tests for the reload.plugins command and plugin tool tracking."""
from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.tools import REGISTRY


def test_load_hooks_tracks_plugin_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_hooks should record which REGISTRY keys each plugin introduced."""
    framework = BubFramework()

    class ToolPlugin:
        def __init__(self, fw):
            pass

    entry_point = SimpleNamespace(
        name="tool-plugin",
        load=lambda: ToolPlugin,
        value="some_module:ToolPlugin",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])

    framework.load_hooks()

    assert "tool-plugin" in framework._plugin_tools
    assert "builtin" in framework._plugin_tools

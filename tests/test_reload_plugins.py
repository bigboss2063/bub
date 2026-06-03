"""Tests for the reload.plugins command and plugin tool tracking."""
from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace

import pytest
from bub.framework import BubFramework


def test_load_hooks_tracks_plugin_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_hooks should record which REGISTRY keys each plugin introduced."""
    framework = BubFramework()

    class ToolPlugin:
        def __init__(self, fw):
            from bub.tools import tool as bub_tool

            @bub_tool(name="my_test_tool")
            def my_tool() -> str:
                return "done"

    entry_point = SimpleNamespace(
        name="tool-plugin",
        load=lambda: ToolPlugin,
        value="some_module:ToolPlugin",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])

    framework.load_hooks()

    assert "tool-plugin" in framework._plugin_tools
    assert "builtin" in framework._plugin_tools
    assert "my_test_tool" in framework._plugin_tools["tool-plugin"]


def test_clear_plugin_modules_removes_cached_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """_clear_plugin_modules should remove plugin modules from sys.modules."""
    import sys
    from bub.framework import _clear_plugin_modules

    # Simulate cached plugin modules
    sys.modules["fake_plugin"] = type(sys)("fake_plugin")
    sys.modules["fake_plugin.sub"] = type(sys)("fake_plugin.sub")
    sys.modules["unrelated"] = type(sys)("unrelated")

    _clear_plugin_modules("fake_plugin:SomeClass")

    assert "fake_plugin" not in sys.modules
    assert "fake_plugin.sub" not in sys.modules
    assert "unrelated" in sys.modules  # untouched

    # Cleanup
    sys.modules.pop("unrelated", None)

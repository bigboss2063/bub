"""Tests for the reload.plugins command and plugin tool tracking."""
from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace

import pytest

from bub.framework import BubFramework
from bub.hookspecs import hookimpl


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


def test_reload_plugins_reregisters_external_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """reload_plugins should unregister and re-register external plugins."""
    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

        @hookimpl
        def system_prompt(self, prompt, state):
            return "v1"

    entry_point = SimpleNamespace(
        name="my-plugin",
        load=lambda: PluginV1,
        value="my_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    # Verify initial load
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt

    # Reload — entry_point still returns PluginV1
    status = framework.reload_plugins()

    assert status["my-plugin"].is_success is True
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt


def test_reload_plugins_keeps_old_plugin_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a plugin fails to reload, its old version should be preserved."""
    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

        @hookimpl
        def system_prompt(self, prompt, state):
            return "v1"

    entry_point = SimpleNamespace(
        name="my-plugin",
        load=lambda: PluginV1,
        value="my_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    # Now make reload fail
    call_count = [1]
    original_load = entry_point.load

    def failing_load():
        call_count[0] += 1
        if call_count[0] > 1:
            raise ImportError("plugin broke")
        return original_load()

    entry_point.load = failing_load

    status = framework.reload_plugins()

    assert status["my-plugin"].is_success is False
    assert "plugin broke" in status["my-plugin"].detail
    # Old plugin still works
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt


def test_reload_plugins_removes_old_tools_and_registers_new_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """After reload, old plugin tools should be gone and new ones registered."""
    from bub.tools import REGISTRY

    framework = BubFramework()
    call_count = [0]

    class PluginV1:
        def __init__(self, fw):
            pass

    def load_plugin():
        from bub.tools import tool as bub_tool

        call_count[0] += 1
        if call_count[0] == 1:

            @bub_tool(name="plugin_v1_tool")
            def old_tool() -> str:
                return "v1"
        else:

            @bub_tool(name="plugin_v2_tool")
            def new_tool() -> str:
                return "v2"

        return PluginV1

    entry_point = SimpleNamespace(
        name="versioned-plugin",
        load=load_plugin,
        value="versioned_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    assert "plugin_v1_tool" in REGISTRY
    assert "plugin_v2_tool" not in REGISTRY

    framework.reload_plugins()

    assert "plugin_v1_tool" not in REGISTRY
    assert "plugin_v2_tool" in REGISTRY

    REGISTRY.pop("plugin_v2_tool", None)


def test_reload_plugins_with_no_external_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """reload_plugins with only builtin plugins should return status without error."""
    framework = BubFramework()
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [])

    framework.load_hooks()
    status = framework.reload_plugins()

    assert "builtin" in status
    assert status["builtin"].is_success is True


@pytest.mark.asyncio
async def test_reload_plugins_tool_returns_formatted_report() -> None:
    """The reload.plugins tool should return a human-readable status report."""
    from republic import ToolContext

    from bub.builtin.tools import reload_plugins
    from bub.framework import PluginStatus

    framework = BubFramework()
    expected_status = {
        "builtin": PluginStatus(is_success=True),
        "good-plugin": PluginStatus(is_success=True),
        "bad-plugin": PluginStatus(is_success=False, detail="ImportError: no mod"),
    }

    class FakeAgent:
        def __init__(self, framework: BubFramework):
            self.framework = framework

    context = ToolContext(state={"_runtime_agent": FakeAgent(framework)}, tape="test", run_id="test")

    from unittest.mock import patch

    with patch.object(framework, "reload_plugins", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "2 ok, 1 failed" in result
    assert "✓ builtin" in result
    assert "✓ good-plugin" in result
    assert "✗ bad-plugin" in result
    assert "ImportError: no mod" in result


@pytest.mark.asyncio
async def test_show_help_lists_reload_plugins_command() -> None:
    """help should list the reload.plugins internal command."""
    from bub.builtin.tools import show_help

    assert ",reload.plugins" in await show_help.run()

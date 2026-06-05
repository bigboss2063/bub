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


def test_reload_hooks_reregisters_external_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """reload_hooks should unregister and re-register external plugins."""
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
    status = framework.reload_hooks()

    assert status["my-plugin"].is_success is True
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt


def test_reload_hooks_keeps_old_plugin_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
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

    status = framework.reload_hooks()

    assert status["my-plugin"].is_success is False
    assert "plugin broke" in status["my-plugin"].detail
    # Old plugin still works
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt


def test_reload_hooks_removes_old_tools_and_registers_new_ones(monkeypatch: pytest.MonkeyPatch) -> None:
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

    framework.reload_hooks()

    assert "plugin_v1_tool" not in REGISTRY
    assert "plugin_v2_tool" in REGISTRY

    REGISTRY.pop("plugin_v2_tool", None)


def test_reload_plugins_drops_orphaned_tools_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a renamed tool is registered in __init__ and reload fails, the new tool
    must not leak into REGISTRY and the old tool must be restored."""
    from bub.tools import REGISTRY

    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

    call_count = [0]
    fail_next_register = [False]

    def load_plugin():
        from bub.tools import tool as bub_tool

        call_count[0] += 1
        if call_count[0] == 1:

            @bub_tool(name="renamed_plugin_old")
            def old_tool() -> str:
                return "v1"
        else:

            @bub_tool(name="renamed_plugin_new")
            def new_tool() -> str:
                return "v2"
            fail_next_register[0] = True

        return PluginV1

    def failing_register(plugin, name):
        if fail_next_register[0] and name == "renamed-plugin":
            fail_next_register[0] = False
            raise RuntimeError("register blew up")
        return original_register(plugin, name=name)

    entry_point = SimpleNamespace(
        name="renamed-plugin",
        load=load_plugin,
        value="renamed_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    assert "renamed_plugin_old" in REGISTRY
    assert "renamed_plugin_new" not in REGISTRY

    # Force the reload's register() to fail so the new tool added in __init__
    # would otherwise be left behind in REGISTRY. The restore call must still
    # succeed.
    original_register = framework._plugin_manager.register
    framework._plugin_manager.register = failing_register  # type: ignore[method-assign]

    try:
        status = framework.reload_hooks()
    finally:
        framework._plugin_manager.register = original_register  # type: ignore[method-assign]

    assert status["renamed-plugin"].is_success is False
    assert "renamed_plugin_new" not in REGISTRY, "orphaned tool leaked into REGISTRY"
    assert "renamed_plugin_old" in REGISTRY, "old tool not restored after rollback"

    REGISTRY.pop("renamed_plugin_old", None)


def test_reload_plugins_with_no_external_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """reload_plugins with only builtin plugins should return status without error."""
    framework = BubFramework()
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [])

    framework.load_hooks()
    status = framework.reload_hooks()

    assert "builtin" in status
    assert status["builtin"].is_success is True


def test_reload_hooks_treats_deleted_plugin_as_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a plugin's module is deleted from disk, reload should mark it as removed, not failed."""
    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

        @hookimpl
        def system_prompt(self, prompt, state):
            return "v1"

    call_count = [0]

    def load_plugin():
        call_count[0] += 1
        if call_count[0] == 1:
            return PluginV1
        raise ModuleNotFoundError("No module named 'deleted_plugin'")

    entry_point = SimpleNamespace(
        name="deleted-plugin",
        load=load_plugin,
        value="deleted_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt

    status = framework.reload_hooks()

    assert status["deleted-plugin"].is_success is True
    assert status["deleted-plugin"].detail == "removed"


def test_reload_hooks_treats_file_not_found_as_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError during reload should also be treated as removed."""
    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

    call_count = [0]

    def load_plugin():
        call_count[0] += 1
        if call_count[0] == 1:
            return PluginV1
        raise FileNotFoundError("plugin directory not found")

    entry_point = SimpleNamespace(
        name="missing-plugin",
        load=load_plugin,
        value="missing_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    status = framework.reload_hooks()

    assert status["missing-plugin"].is_success is True
    assert status["missing-plugin"].detail == "removed"


def test_reload_hooks_non_import_error_still_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-filesystem errors (e.g. RuntimeError) should still trigger rollback."""
    framework = BubFramework()

    class PluginV1:
        def __init__(self, fw):
            pass

        @hookimpl
        def system_prompt(self, prompt, state):
            return "v1"

    call_count = [0]

    def load_plugin():
        call_count[0] += 1
        if call_count[0] == 1:
            return PluginV1
        raise RuntimeError("unexpected error")

    entry_point = SimpleNamespace(
        name="error-plugin",
        load=load_plugin,
        value="error_plugin:PluginV1",
    )
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    status = framework.reload_hooks()

    assert status["error-plugin"].is_success is False
    assert "unexpected error" in status["error-plugin"].detail
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt


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

    with patch.object(framework, "reload_hooks", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "2 ok, 0 removed, 1 failed" in result
    assert "✓ builtin" in result
    assert "✓ good-plugin" in result
    assert "✗ bad-plugin" in result
    assert "ImportError: no mod" in result


@pytest.mark.asyncio
async def test_reload_plugins_tool_shows_removed_status() -> None:
    """The reload.plugins tool should show '\\u2013' for removed plugins."""
    from republic import ToolContext

    from bub.builtin.tools import reload_plugins
    from bub.framework import PluginStatus

    framework = BubFramework()
    expected_status = {
        "builtin": PluginStatus(is_success=True),
        "deleted-plugin": PluginStatus(is_success=True, detail="removed"),
    }

    class FakeAgent:
        def __init__(self, framework: BubFramework):
            self.framework = framework

    context = ToolContext(state={"_runtime_agent": FakeAgent(framework)}, tape="test", run_id="test")

    from unittest.mock import patch

    with patch.object(framework, "reload_hooks", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "1 ok, 1 removed, 0 failed" in result
    assert "\u2013 deleted-plugin" in result


@pytest.mark.asyncio
async def test_show_help_lists_reload_plugins_command() -> None:
    """help should list the reload.plugins internal command."""
    from bub.builtin.tools import show_help

    assert ",reload.plugins" in await show_help.run()

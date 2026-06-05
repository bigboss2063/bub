"""Tests for the reload.plugins command and plugin lifecycle via filesystem scanning."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from republic import ToolContext

from bub.builtin.tools import reload_plugins, show_help
from bub.framework import BubFramework, PluginStatus
from bub.hookspecs import hookimpl
from bub.plugin_manager import PluginStatusCode
from bub.tools import REGISTRY


def _make_plugin_dir(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    return plugin_dir


def _write_plugin(
    plugin_dir: Path,
    name: str,
    class_name: str,
    code: str,
    *,
    manifest_extra: str = "",
) -> Path:
    plugin_path = plugin_dir / name
    plugin_path.mkdir(exist_ok=True)
    (plugin_path / "bub.toml").write_text(
        f'[plugin]\nname = "{name}"\nentry = "{name}:{class_name}"\n{manifest_extra}',
        encoding="utf-8",
    )
    (plugin_path / f"{name}.py").write_text(code, encoding="utf-8")
    return plugin_path


def _setup_framework(tmp_path: Path) -> BubFramework:
    plugin_dir = _make_plugin_dir(tmp_path)
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()
    return framework


def test_load_hooks_loads_builtin(tmp_path: Path) -> None:
    framework = _setup_framework(tmp_path)
    assert "builtin" in framework._plugin_status
    assert framework._plugin_status["builtin"].ok


def test_reload_hooks_with_no_external_plugins(tmp_path: Path) -> None:
    framework = _setup_framework(tmp_path)
    status = framework.reload_hooks()
    assert "builtin" in status
    assert status["builtin"].ok


def test_reload_hooks_reloads_external_plugin(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _write_plugin(
        plugin_dir,
        "my_plugin",
        "MyPlugin",
        "class MyPlugin:\n    def __init__(self, fw): pass\n",
    )

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()

    assert "my_plugin" in framework._plugin_status
    assert framework._plugin_status["my_plugin"].ok

    status = framework.reload_hooks()
    assert status["my_plugin"].ok
    assert status["my_plugin"].code == PluginStatusCode.LOADED

    for mod in list(sys.modules.keys()):
        if mod.startswith("my_plugin"):
            del sys.modules[mod]


def test_reload_hooks_keeps_old_plugin_on_failure(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_path = _write_plugin(
        plugin_dir,
        "fail_plugin",
        "FailPlugin",
        "from bub.hookspecs import hookimpl\n"
        "class FailPlugin:\n"
        "    def __init__(self, fw): pass\n"
        "    @hookimpl\n"
        "    def system_prompt(self, prompt, state):\n"
        "        return 'v1'\n",
    )

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()

    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt

    (plugin_path / "fail_plugin.py").write_text("raise SyntaxError('plugin broke')\n", encoding="utf-8")

    status = framework.reload_hooks()
    assert not status["fail_plugin"].ok
    assert "plugin broke" in status["fail_plugin"].detail

    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt

    for mod in list(sys.modules.keys()):
        if mod.startswith("fail_plugin"):
            del sys.modules[mod]


def test_reload_hooks_detects_removed_plugin(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_path = _write_plugin(
        plugin_dir,
        "removed_plugin",
        "RemovedPlugin",
        "class RemovedPlugin:\n    def __init__(self, fw): pass\n",
    )

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()

    assert "removed_plugin" in framework._plugin_status

    shutil.rmtree(plugin_path)

    status = framework.reload_hooks()
    assert status["removed_plugin"].ok
    assert status["removed_plugin"].code == PluginStatusCode.REMOVED

    for mod in list(sys.modules.keys()):
        if mod.startswith("removed_plugin"):
            del sys.modules[mod]


def test_reload_hooks_detects_new_plugin(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()

    _write_plugin(
        plugin_dir,
        "new_plugin",
        "NewPlugin",
        "class NewPlugin:\n    def __init__(self, fw): pass\n",
    )

    status = framework.reload_hooks()
    assert "new_plugin" in status
    assert status["new_plugin"].ok
    assert status["new_plugin"].code == PluginStatusCode.LOADED

    for mod in list(sys.modules.keys()):
        if mod.startswith("new_plugin"):
            del sys.modules[mod]


def test_reload_hooks_swaps_tools_on_reload(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_path = _write_plugin(
        plugin_dir,
        "swap_plugin",
        "SwapPlugin",
        "from bub.tools import tool\n"
        "class SwapPlugin:\n"
        "    def __init__(self, fw): pass\n"
        "@tool(name='swap_tool_v1')\n"
        "def v1_tool() -> str:\n"
        "    return 'v1'\n",
    )

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._plugin_mgr.plugin_dirs = [plugin_dir]
    framework.load_hooks()

    assert "swap_tool_v1" in REGISTRY

    (plugin_path / "swap_plugin.py").write_text(
        "from bub.tools import tool\n"
        "class SwapPlugin:\n"
        "    def __init__(self, fw): pass\n"
        "@tool(name='swap_tool_v2')\n"
        "def v2_tool() -> str:\n"
        "    return 'v2'\n",
        encoding="utf-8",
    )

    framework.reload_hooks()

    assert "swap_tool_v1" not in REGISTRY
    assert "swap_tool_v2" in REGISTRY

    REGISTRY.pop("swap_tool_v2", None)
    for mod in list(sys.modules.keys()):
        if mod.startswith("swap_plugin"):
            del sys.modules[mod]


@pytest.mark.asyncio
async def test_reload_plugins_tool_returns_formatted_report() -> None:
    framework = BubFramework()
    expected_status = {
        "builtin": PluginStatus(ok=True, code=PluginStatusCode.LOADED),
        "good-plugin": PluginStatus(ok=True, code=PluginStatusCode.LOADED),
        "bad-plugin": PluginStatus(ok=False, code=PluginStatusCode.FAILED, detail="ImportError: no mod"),
    }

    class FakeAgent:
        def __init__(self, fw: BubFramework):
            self.framework = fw

    context = ToolContext(state={"_runtime_agent": FakeAgent(framework)}, tape="test", run_id="test")

    with patch.object(framework, "reload_hooks", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "2 ok, 0 removed, 1 failed" in result
    assert "✓ builtin" in result
    assert "✓ good-plugin" in result
    assert "✗ bad-plugin" in result
    assert "ImportError: no mod" in result


@pytest.mark.asyncio
async def test_reload_plugins_tool_shows_removed_status() -> None:
    framework = BubFramework()
    expected_status = {
        "builtin": PluginStatus(ok=True, code=PluginStatusCode.LOADED),
        "deleted-plugin": PluginStatus(ok=True, code=PluginStatusCode.REMOVED),
    }

    class FakeAgent:
        def __init__(self, fw: BubFramework):
            self.framework = fw

    context = ToolContext(state={"_runtime_agent": FakeAgent(framework)}, tape="test", run_id="test")

    with patch.object(framework, "reload_hooks", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "1 ok, 1 removed, 0 failed" in result
    assert "\u2013 deleted-plugin" in result


@pytest.mark.asyncio
async def test_show_help_lists_reload_plugins_command() -> None:
    assert ",reload.plugins" in await show_help.run()

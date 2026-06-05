"""Tests for plugin_manager filesystem scanning and lifecycle."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.plugin_manager import PluginManager, PluginSpec, PluginStatusCode
from bub.tools import REGISTRY


@pytest.fixture
def make_plugin_dir(tmp_path: Path) -> Path:
    """Create a temporary plugin directory."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    return plugin_dir


def test_scan_finds_valid_plugins(make_plugin_dir: Path) -> None:
    """scan() should discover plugins with valid bub.toml manifests."""
    plugin_dir = make_plugin_dir

    # Valid plugin
    valid = plugin_dir / "valid_plugin"
    valid.mkdir()
    (valid / "bub.toml").write_text(
        '[plugin]\nname = "valid_plugin"\nentry = "valid_plugin:Plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (valid / "valid_plugin.py").write_text(
        "class Plugin:\n    pass\n",
        encoding="utf-8",
    )

    # Missing manifest
    no_manifest = plugin_dir / "no_manifest"
    no_manifest.mkdir()

    # Malformed manifest
    bad = plugin_dir / "bad_plugin"
    bad.mkdir()
    (bad / "bub.toml").write_text("not valid toml {{\n", encoding="utf-8")

    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[plugin_dir])
    specs = pm.scan()

    names = {s.name for s in specs}
    assert "valid_plugin" in names
    assert "bad_plugin" not in names
    assert "no_manifest" not in names


def test_load_registers_plugin_and_tracks_tools_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_load should register a plugin and track tools/modules deltas."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()

    plugin_path = plugin_dir / "my_plugin"
    plugin_path.mkdir()
    (plugin_path / "bub.toml").write_text(
        '[plugin]\nname = "my_plugin"\nentry = "my_plugin:MyPlugin"\n',
        encoding="utf-8",
    )
    (plugin_path / "my_plugin.py").write_text(
        "from bub.tools import tool\n"
        "class MyPlugin:\n"
        "    def __init__(self, fw):\n"
        "        pass\n"
        "@tool(name='my_plugin_tool')\n"
        "def my_tool() -> str:\n"
        "    return 'hello'\n",
        encoding="utf-8",
    )

    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[plugin_dir])
    spec = PluginSpec(name="my_plugin", entry="my_plugin:MyPlugin", path=plugin_path)

    pre_modules = set(sys.modules.keys())
    state = pm._load(spec)

    assert "my_plugin" in pm._plugins
    assert state.instance in pm.pluggy_manager.get_plugins()
    assert "my_plugin_tool" in state.tools
    assert state.modules
    assert state.modules.issubset(set(sys.modules.keys()) - pre_modules)

    pm._unload("my_plugin")
    REGISTRY.pop("my_plugin_tool", None)
    for mod in list(sys.modules.keys()):
        if mod.startswith("my_plugin"):
            del sys.modules[mod]


def test_unload_removes_plugin_and_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_unload should unregister plugin, remove tools, and clean sys.modules."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()

    plugin_path = plugin_dir / "unload_plugin"
    plugin_path.mkdir()
    (plugin_path / "bub.toml").write_text(
        '[plugin]\nname = "unload_plugin"\nentry = "unload_plugin:UnloadPlugin"\n',
        encoding="utf-8",
    )
    (plugin_path / "unload_plugin.py").write_text(
        "from bub.tools import tool\n"
        "class UnloadPlugin:\n"
        "    def __init__(self, fw):\n"
        "        pass\n"
        "@tool(name='unload_tool')\n"
        "def unload_tool() -> str:\n"
        "    return 'bye'\n",
        encoding="utf-8",
    )

    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[plugin_dir])
    spec = PluginSpec(name="unload_plugin", entry="unload_plugin:UnloadPlugin", path=plugin_path)
    state = pm._load(spec)
    pm._plugins["unload_plugin"] = state

    assert state.instance in pm.pluggy_manager.get_plugins()
    assert "unload_tool" in REGISTRY

    pm._unload("unload_plugin")

    assert state.instance not in pm.pluggy_manager.get_plugins()
    assert "unload_tool" not in REGISTRY
    for mod in state.modules:
        assert mod not in sys.modules


def test_unload_refuses_permanent_plugin(tmp_path: Path) -> None:
    """_unload should refuse to unload a permanent plugin."""
    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[])
    spec = PluginSpec(name="builtin", entry="foo:Bar", path=tmp_path, permanent=True)
    # Fake a plugin state without actually loading
    pm._plugins["builtin"] = SimpleNamespace(spec=spec, instance=object(), tools=set(), modules=set())
    with pytest.raises(RuntimeError, match="Cannot unload permanent plugin"):
        pm._unload("builtin")


def test_reload_all_handles_added_removed_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """reload_all should handle added, removed, and existing plugins correctly."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()

    # Plugin A (will be removed)
    path_a = plugin_dir / "plugin_a"
    path_a.mkdir()
    (path_a / "bub.toml").write_text(
        '[plugin]\nname = "plugin_a"\nentry = "plugin_a:PluginA"\n',
        encoding="utf-8",
    )
    (path_a / "plugin_a.py").write_text(
        "class PluginA:\n    def __init__(self, fw): pass\n",
        encoding="utf-8",
    )

    # Plugin B (will stay)
    path_b = plugin_dir / "plugin_b"
    path_b.mkdir()
    (path_b / "bub.toml").write_text(
        '[plugin]\nname = "plugin_b"\nentry = "plugin_b:PluginB"\n',
        encoding="utf-8",
    )
    (path_b / "plugin_b.py").write_text(
        "class PluginB:\n    def __init__(self, fw): pass\n",
        encoding="utf-8",
    )

    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[plugin_dir])
    pm.load_all_external()

    assert "plugin_a" in pm._plugins
    assert "plugin_b" in pm._plugins

    # Remove plugin_a from filesystem
    import shutil

    shutil.rmtree(path_a)

    # Add plugin_c
    path_c = plugin_dir / "plugin_c"
    path_c.mkdir()
    (path_c / "bub.toml").write_text(
        '[plugin]\nname = "plugin_c"\nentry = "plugin_c:PluginC"\n',
        encoding="utf-8",
    )
    (path_c / "plugin_c.py").write_text(
        "class PluginC:\n    def __init__(self, fw): pass\n",
        encoding="utf-8",
    )

    status = pm.reload_all()

    assert "plugin_a" not in pm._plugins
    assert status["plugin_a"].code == PluginStatusCode.REMOVED
    assert "plugin_b" in pm._plugins
    assert status["plugin_b"].code == PluginStatusCode.LOADED
    assert "plugin_c" in pm._plugins
    assert status["plugin_c"].code == PluginStatusCode.LOADED

    # Cleanup
    for name in list(pm._plugins.keys()):
        if name != "builtin":
            pm._unload(name)
    for mod in list(sys.modules.keys()):
        if mod.startswith(("plugin_a", "plugin_b", "plugin_c")):
            del sys.modules[mod]


def test_reload_all_rollback_on_existing_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """reload_all should rollback an existing plugin if reload fails."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()

    path_x = plugin_dir / "plugin_x"
    path_x.mkdir()
    (path_x / "bub.toml").write_text(
        '[plugin]\nname = "plugin_x"\nentry = "plugin_x:PluginX"\n',
        encoding="utf-8",
    )
    (path_x / "plugin_x.py").write_text(
        "class PluginX:\n"
        "    def __init__(self, fw):\n"
        "        pass\n"
        "    def system_prompt(self, prompt, state):\n"
        "        return 'v1'\n",
        encoding="utf-8",
    )

    pm = PluginManager(framework=SimpleNamespace(), plugin_dirs=[plugin_dir])
    pm.load_all_external()

    assert "plugin_x" in pm._plugins

    # Corrupt the plugin so reload fails
    (path_x / "plugin_x.py").write_text(
        "raise SyntaxError('boom')\n",
        encoding="utf-8",
    )

    status = pm.reload_all()

    assert status["plugin_x"].code == PluginStatusCode.FAILED
    # Rollback should have restored the old plugin instance
    assert "plugin_x" in pm._plugins

    # Cleanup
    pm._unload("plugin_x")
    for mod in list(sys.modules.keys()):
        if mod.startswith("plugin_x"):
            del sys.modules[mod]

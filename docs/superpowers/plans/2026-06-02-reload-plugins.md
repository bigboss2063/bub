# `,reload.plugins` 命令实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 bub 运行时添加 `,reload.plugins` 内部命令，支持在不重启进程的情况下热重载外部插件。

**Architecture:** 在 `BubFramework` 中追踪每个插件引入的 REGISTRY 工具（通过快照 diff），新增 `reload_hooks()` 方法执行"注销→清除→重新发现→重新注册"流程，单个插件失败时回滚到旧版本。通过 `builtin/tools.py` 暴露为 `,reload.plugins` 内部命令。

**Tech Stack:** Python 3.12+, pluggy, pytest, monkeypatch

---

## File Structure

| 文件 | 职责 | 变更类型 |
|------|------|----------|
| `src/bub/framework.py` | 框架核心：插件生命周期管理 | 修改 |
| `src/bub/builtin/tools.py` | 内部命令工具注册 | 修改 |
| `tests/test_reload_plugins.py` | reload 功能测试 | 新建 |

---

### Task 1: 在 BubFramework 中添加插件工具追踪

**Files:**
- Modify: `src/bub/framework.py`
- Test: `tests/test_reload_plugins.py`

- [ ] **Step 1: 编写测试——load_hooks 追踪插件工具**

```python
# tests/test_reload_plugins.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_load_hooks_tracks_plugin_tools -v`
Expected: FAIL — `AttributeError: 'BubFramework' object has no attribute '_plugin_tools'`

- [ ] **Step 3: 在 BubFramework.__init__ 中添加 _plugin_tools**

在 `src/bub/framework.py` 的 `__init__` 方法中，第49行 `self._plugin_status` 之后添加：

```python
self._plugin_tools: dict[str, set[str]] = {}
```

- [ ] **Step 4: 修改 load_hooks 添加快照 diff 追踪**

将 `load_hooks()` 方法（第66-90行）修改为在每个插件注册前后做快照 diff。关键改动：

1. 在 `_load_builtin_hooks` 之后、外部插件循环之前，对 builtin 做一次快照 diff
2. 对每个外部插件，在 `register` 前后做快照 diff

替换 `load_hooks` 方法为：

```python
def load_hooks(self) -> None:
    import importlib.metadata

    pending_plugins: list[tuple[str, Any]] = []

    self._load_builtin_hooks()
    # Track builtin tools
    self._plugin_tools["builtin"] = set(REGISTRY.keys())

    for entry_point in importlib.metadata.entry_points(group="bub"):
        try:
            plugin = entry_point.load()
        except Exception as exc:
            logger.warning(f"Failed to load plugin '{entry_point.name}': {exc}")
            self._plugin_status[entry_point.name] = PluginStatus(is_success=False, detail=str(exc))
        else:
            pending_plugins.append((entry_point.name, plugin))

    for plugin_name, plugin in pending_plugins:
        try:
            before = set(REGISTRY.keys())
            if callable(plugin):  # Support entry points that are classes
                plugin = plugin(self)
            self._plugin_manager.register(plugin, name=plugin_name)
            after = set(REGISTRY.keys())
            self._plugin_tools[plugin_name] = after - before
        except Exception as exc:
            logger.warning(f"Failed to initialize plugin '{plugin_name}': {exc}")
            self._plugin_status[plugin_name] = PluginStatus(is_success=False, detail=str(exc))
        else:
            self._plugin_status[plugin_name] = PluginStatus(is_success=True)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_load_hooks_tracks_plugin_tools -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/bub/framework.py tests/test_reload_plugins.py
git commit -m "feat(framework): track plugin tool ownership during load_hooks"
```

---

### Task 2: 实现 _clear_plugin_modules 辅助函数

**Files:**
- Modify: `src/bub/framework.py`

- [ ] **Step 1: 编写测试——模块缓存清除**

在 `tests/test_reload_plugins.py` 中添加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_clear_plugin_modules_removes_cached_entries -v`
Expected: FAIL — `ImportError: cannot import name '_clear_plugin_modules'`

- [ ] **Step 3: 实现 _clear_plugin_modules**

在 `src/bub/framework.py` 顶部（`BubFramework` 类之前）添加：

```python
def _clear_plugin_modules(entry_point_value: str) -> None:
    """Remove plugin modules from sys.modules to allow re-import."""
    import sys

    module_name = entry_point_value.split(":")[0]
    root = module_name.split(".")[0]
    to_remove = [key for key in sys.modules if key == root or key.startswith(f"{root}.")]
    for key in to_remove:
        del sys.modules[key]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_clear_plugin_modules_removes_cached_entries -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/bub/framework.py tests/test_reload_plugins.py
git commit -m "feat(framework): add _clear_plugin_modules helper"
```

---

### Task 3: 实现 BubFramework.reload_hooks() 方法

**Files:**
- Modify: `src/bub/framework.py`
- Test: `tests/test_reload_plugins.py`

- [ ] **Step 1: 编写测试——正常 reload**

在 `tests/test_reload_plugins.py` 中添加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_reload_hooks_reregisters_external_plugins -v`
Expected: FAIL — `AttributeError: 'BubFramework' object has no attribute 'reload_hooks'`

- [ ] **Step 3: 编写测试——单个插件失败回滚**

```python
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
    call_count = [0]
    original_load = entry_point.load

    def failing_load():
        call_count[0] += 1
        if call_count[0] > 1:  # First load is initial, second is reload
            raise ImportError("plugin broke")
        return original_load()

    entry_point.load = failing_load

    status = framework.reload_hooks()

    assert status["my-plugin"].is_success is False
    assert "plugin broke" in status["my-plugin"].detail
    # Old plugin still works
    prompt = framework.get_system_prompt(prompt="hello", state={})
    assert "v1" in prompt
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_reload_hooks_keeps_old_plugin_on_failure -v`
Expected: FAIL — `AttributeError: 'BubFramework' object has no attribute 'reload_hooks'`

- [ ] **Step 5: 实现 reload_hooks 方法**

在 `src/bub/framework.py` 的 `BubFramework` 类中，在 `load_hooks` 方法之后添加：

```python
def reload_hooks(self) -> dict[str, PluginStatus]:
    """Reload external plugins while preserving builtins.

    For each external plugin: unregister from pluggy, remove its tracked
    tools from REGISTRY, clear sys.modules cache, then re-discover and
    re-register. On per-plugin failure the old plugin and its tools are
    restored so the running session is never left worse off.
    """
    import importlib
    import sys

    importlib.invalidate_caches()

    # Snapshot external plugin names and their tool sets
    external_names = [name for name in self._plugin_status if name != "builtin"]
    old_plugins: dict[str, Any] = {}
    old_tools: dict[str, set[str]] = {}

    for name in external_names:
        plugin = self._plugin_manager.unregister(name=name)
        old_plugins[name] = plugin
        tool_names = self._plugin_tools.pop(name, set())
        old_tools[name] = tool_names
        for tool_name in tool_names:
            REGISTRY.pop(tool_name, None)

    # Re-discover entry points
    for entry_point in importlib.metadata.entry_points(group="bub"):
        ep_name = entry_point.name
        try:
            _clear_plugin_modules(entry_point.value)
            importlib.invalidate_caches()
            plugin = entry_point.load()
            before = set(REGISTRY.keys())
            if callable(plugin):
                plugin = plugin(self)
            self._plugin_manager.register(plugin, name=ep_name)
            after = set(REGISTRY.keys())
            self._plugin_tools[ep_name] = after - before
            self._plugin_status[ep_name] = PluginStatus(is_success=True)
        except Exception as exc:
            logger.warning(f"Failed to reload plugin '{ep_name}': {exc}")
            # Rollback: restore old plugin and its tools
            if ep_name in old_plugins and old_plugins[ep_name] is not None:
                self._plugin_manager.register(old_plugins[ep_name], name=ep_name)
                for tool_name in old_tools.get(ep_name, set()):
                    # Tools are re-registered by the old plugin module import,
                    # but we need to ensure they're in REGISTRY
                    pass
            self._plugin_tools[ep_name] = old_tools.get(ep_name, set())
            self._plugin_status[ep_name] = PluginStatus(is_success=False, detail=str(exc))

    return dict(self._plugin_status)
```

**注意回滚逻辑：** 当旧插件被 pluggy `unregister` 后再 `register` 回去时，pluggy 会恢复其 hook 实现。但 REGISTRY 中的工具需要额外处理——旧插件的工具在 unregister 阶段被删除了。由于旧插件模块仍在 sys.modules 中（reload 失败意味着没有成功重新加载），其 `@tool()` 装饰器不会重新执行。

我们需要在删除前保存旧工具的 Tool 对象，并在回滚时恢复它们：

将 Task 3 Step 5 中的回滚逻辑改为更完整的版本。在删除工具时保存 Tool 对象：

```python
def reload_hooks(self) -> dict[str, PluginStatus]:
    """Reload external plugins while preserving builtins."""
    import importlib
    import sys

    importlib.invalidate_caches()

    external_names = [name for name in self._plugin_status if name != "builtin"]
    old_plugins: dict[str, Any] = {}
    old_tools: dict[str, set[str]] = {}
    old_tool_objects: dict[str, dict[str, Any]] = {}

    # Phase 1: Unregister external plugins and save their state
    for name in external_names:
        plugin = self._plugin_manager.unregister(name=name)
        old_plugins[name] = plugin
        tool_names = self._plugin_tools.pop(name, set())
        old_tools[name] = tool_names
        saved = {}
        for tool_name in tool_names:
            saved[tool_name] = REGISTRY.pop(tool_name, None)
        old_tool_objects[name] = saved

    # Phase 2: Re-discover and re-register
    for entry_point in importlib.metadata.entry_points(group="bub"):
        ep_name = entry_point.name
        try:
            _clear_plugin_modules(entry_point.value)
            importlib.invalidate_caches()
            plugin = entry_point.load()
            before = set(REGISTRY.keys())
            if callable(plugin):
                plugin = plugin(self)
            self._plugin_manager.register(plugin, name=ep_name)
            after = set(REGISTRY.keys())
            self._plugin_tools[ep_name] = after - before
            self._plugin_status[ep_name] = PluginStatus(is_success=True)
        except Exception as exc:
            logger.warning(f"Failed to reload plugin '{ep_name}': {exc}")
            # Rollback: restore old plugin instance and its tools
            if ep_name in old_plugins and old_plugins[ep_name] is not None:
                self._plugin_manager.register(old_plugins[ep_name], name=ep_name)
            for tool_name, tool_obj in old_tool_objects.get(ep_name, {}).items():
                if tool_obj is not None:
                    REGISTRY[tool_name] = tool_obj
            self._plugin_tools[ep_name] = old_tools.get(ep_name, set())
            self._plugin_status[ep_name] = PluginStatus(is_success=False, detail=str(exc))

    return dict(self._plugin_status)
```

- [ ] **Step 6: 运行全部 reload 测试确认通过**

Run: `uv run python -m pytest tests/test_reload_plugins.py -v`
Expected: ALL PASS

- [ ] **Step 7: 提交**

```bash
git add src/bub/framework.py tests/test_reload_plugins.py
git commit -m "feat(framework): add reload_hooks method with per-plugin rollback"
```

---

### Task 4: 添加更多测试用例

**Files:**
- Modify: `tests/test_reload_plugins.py`

- [ ] **Step 1: 编写测试——工具清理和替换**

```python
def test_reload_hooks_removes_old_tools_and_registers_new_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """After reload, old plugin tools should be gone and new ones registered."""
    framework = BubFramework()

    call_count = [0]

    class PluginV1:
        def __init__(self, fw):
            pass

    def make_entry_point():
        def load():
            call_count[0] += 1
            if call_count[0] <= 1:
                # First load: register a tool
                from bub.tools import tool as bub_tool

                @bub_tool(name="plugin_v1_tool")
                def old_tool():
                    return "v1"
            else:
                # Reload: register a different tool
                from bub.tools import tool as bub_tool

                @bub_tool(name="plugin_v2_tool")
                def new_tool():
                    return "v2"
            return PluginV1

        return SimpleNamespace(
            name="versioned-plugin",
            load=load,
            value="versioned_plugin:PluginV1",
        )

    entry_point = make_entry_point()
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])
    framework.load_hooks()

    assert "plugin_v1_tool" in REGISTRY
    assert "plugin_v2_tool" not in REGISTRY

    framework.reload_hooks()

    assert "plugin_v1_tool" not in REGISTRY
    assert "plugin_v2_tool" in REGISTRY

    # Cleanup
    REGISTRY.pop("plugin_v2_tool", None)
```

- [ ] **Step 2: 编写测试——无外部插件时 reload 不报错**

```python
def test_reload_hooks_with_no_external_plugins() -> None:
    """reload_hooks with only builtin plugins should return status without error."""
    framework = BubFramework()
    framework.load_hooks()

    status = framework.reload_hooks()

    assert "builtin" in status
    assert status["builtin"].is_success is True
```

- [ ] **Step 3: 运行全部测试确认通过**

Run: `uv run python -m pytest tests/test_reload_plugins.py -v`
Expected: ALL PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_reload_plugins.py
git commit -m "test: add reload plugin tests for tool cleanup and empty reload"
```

---

### Task 5: 添加 reload.plugins 工具和更新 help

**Files:**
- Modify: `src/bub/builtin/tools.py`
- Test: `tests/test_reload_plugins.py`

- [ ] **Step 1: 编写测试——reload.plugins 工具输出格式**

在 `tests/test_reload_plugins.py` 中添加：

```python
@pytest.mark.asyncio
async def test_reload_plugins_tool_returns_formatted_report(tmp_path) -> None:
    """The reload.plugins tool should return a human-readable status report."""
    from bub.builtin.tools import reload_plugins
    from bub.framework import BubFramework, PluginStatus
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace

    framework = BubFramework()
    expected_status = {
        "builtin": PluginStatus(is_success=True),
        "good-plugin": PluginStatus(is_success=True),
        "bad-plugin": PluginStatus(is_success=False, detail="ImportError: no mod"),
    }

    class FakeAgent:
        def __init__(self):
            self.framework = framework

    fake_agent = FakeAgent()
    context = SimpleNamespace(
        state={"_runtime_agent": fake_agent},
        tape="test",
        run_id="test",
    )

    with patch.object(framework, "reload_hooks", return_value=expected_status):
        result = await reload_plugins.run(context=context)

    assert "2 ok, 1 failed" in result
    assert "✓ builtin" in result
    assert "✓ good-plugin" in result
    assert "✗ bad-plugin" in result
    assert "ImportError: no mod" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_reload_plugins_tool_returns_formatted_report -v`
Expected: FAIL — `ImportError: cannot import name 'reload_plugins'`

- [ ] **Step 3: 实现 reload.plugins 工具**

在 `src/bub/builtin/tools.py` 中，在 `quit_tool` 函数之后、`_resolve_path` 函数之前添加：

```python
@tool(name="reload.plugins", context=True)
async def reload_plugins(*, context: ToolContext) -> str:
    """Reload external plugins without restarting the process."""
    agent = _get_agent(context)
    status = await asyncio.to_thread(agent.framework.reload_hooks)
    lines = []
    ok = sum(1 for s in status.values() if s.is_success)
    failed = len(status) - ok
    lines.append(f"Plugins reloaded ({ok} ok, {failed} failed):")
    for name, s in status.items():
        mark = "✓" if s.is_success else "✗"
        detail = f": {s.detail}" if s.detail else ""
        lines.append(f"  {mark} {name}{detail}")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m pytest tests/test_reload_plugins.py::test_reload_plugins_tool_returns_formatted_report -v`
Expected: PASS

- [ ] **Step 5: 更新 show_help**

在 `src/bub/builtin/tools.py` 的 `show_help` 函数（第290-310行）中，在 `",quit\n"` 之后添加一行：

```python
"  ,reload.plugins\n"
```

- [ ] **Step 6: 提交**

```bash
git add src/bub/builtin/tools.py tests/test_reload_plugins.py
git commit -m "feat(tools): add reload.plugins command and update help"
```

---

### Task 6: 运行完整测试套件确保无回归

**Files:** 无修改

- [ ] **Step 1: 运行全部测试**

Run: `uv run python -m pytest -v`
Expected: ALL PASS（包括已有的测试和新增的测试）

- [ ] **Step 2: 运行 lint 检查**

Run: `uv run ruff check src/bub/framework.py src/bub/builtin/tools.py tests/test_reload_plugins.py`
Expected: No errors

- [ ] **Step 3: 最终提交（如有 lint 修复）**

```bash
git add -u
git commit -m "chore: lint fixes for reload.plugins"
```

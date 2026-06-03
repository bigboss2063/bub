# `,reload.plugins` 命令设计

**日期**: 2026-06-02
**状态**: 已批准
**范围**: 为 bub 运行时添加 `,reload-plugins` 内部命令，支持在不重启进程的情况下热重载外部插件

## 背景

当前 bub 的插件系统基于 pluggy，通过 `importlib.metadata.entry_points(group="bub")` 在启动时一次性发现并加载。`BubFramework.load_hooks()` 只在 `__main__.py` 的 `create_cli_app()` 中调用一次。

用户安装或更新插件后（通过 `bub install`/`bub update`），必须重启整个 bub 进程才能生效。这在长期运行的会话（如 Telegram bot）中尤其不便。

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 重载范围 | 仅外部插件 | 内置 hooks 和 tools 在整个会话生命周期中保持稳定 |
| 工具清理 | 清除并重新注册插件拥有的工具 | 避免 REGISTRY 中残留已移除插件的工具 |
| 失败策略 | 失败时保留旧版本 | 运行中的会话永远不会因 reload 而变差 |
| 资源清理 | 不处理（第一版） | 与 bub 现有的 shutdown 行为一致——进程退出时也不做插件 teardown |

## 设计

### 1. 插件工具追踪

在 `BubFramework` 中新增 `_plugin_tools: dict[str, set[str]]`，映射 `插件名 → 该插件引入的 REGISTRY 键集合`。

修改 `load_hooks()` 中每个插件的注册流程：

1. 注册前：快照 `set(REGISTRY.keys())`
2. 执行 `self._plugin_manager.register(plugin, name=plugin_name)`
3. 注册后：再次快照 `set(REGISTRY.keys())`
4. 差集 `after - before` 存入 `_plugin_tools[plugin_name]`

内置插件（name=`"builtin"`）也遵循此流程，但 `reload_plugins()` 会跳过它。

**限制**：只追踪通过 `@tool()` 装饰器在注册期间添加的工具。插件在注册完成后动态修改 REGISTRY 的情况无法追踪。

### 2. `BubFramework.reload_plugins()` 方法

在 `framework.py` 上新增同步方法：

```python
def reload_plugins(self) -> dict[str, PluginStatus]:
```

**流程：**

```
1. 调用 importlib.invalidate_caches() 清除元数据缓存
2. 收集当前外部插件名称列表（排除 "builtin"）
3. 对每个外部插件：
   a. 从 pluggy 注销：self._plugin_manager.unregister(plugin)
   b. 从 REGISTRY 移除其工具：del REGISTRY[tool_name] for tool_name in self._plugin_tools[name]
   c. 暂存旧插件实例和工具集用于回滚
4. 重新发现 entry_points(group="bub")
5. 对每个 entry_point：
   a. 清除相关 sys.modules 条目（从 entry point value 提取模块名前缀）
   b. importlib.invalidate_caches()
   c. entry_point.load() 重新加载
   d. 如果 callable，调用 plugin(self) 初始化
   e. self._plugin_manager.register(plugin, name=name)
   f. 快照 diff 追踪新工具
   g. 更新 _plugin_status[name] = PluginStatus(is_success=True)
   h. 如果任何步骤失败：
      - 恢复旧插件：self._plugin_manager.register(old_plugin, name=name)
      - 恢复旧工具到 REGISTRY
      - 记录：_plugin_status[name] = PluginStatus(is_success=False, detail=str(exc))
      - 日志警告
6. 返回 _plugin_status
```

### 3. `reload.plugins` 工具

在 `builtin/tools.py` 中新增：

```python
@tool(name="reload.plugins", context=True)
async def reload_plugins(*, context: ToolContext) -> str:
    """Reload external plugins without restarting the process."""
    agent = _get_agent(context)
    status = await asyncio.to_thread(agent.framework.reload_plugins)
    # 格式化输出
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

使用 `asyncio.to_thread()` 包装避免阻塞事件循环。

同时更新 `show_help()` 工具，添加 `,reload.plugins` 说明。

### 4. 模块缓存清除

reload 时需要清除插件相关的 `sys.modules` 条目，确保代码变更生效：

```python
def _clear_plugin_modules(entry_point_value: str) -> None:
    """Remove plugin modules from sys.modules to allow re-import."""
    module_name = entry_point_value.split(":")[0]
    root = module_name.split(".")[0]
    to_remove = [key for key in sys.modules if key == root or key.startswith(f"{root}.")]
    for key in to_remove:
        del sys.modules[key]
```

### 5. 测试策略

新增 `tests/test_reload_plugins.py`，覆盖：

1. **正常 reload**：两个外部插件 reload 后 hooks 和 tools 正确重新注册
2. **单个插件失败**：一个插件异常时，其他插件正常加载，失败的保留旧版本
3. **工具清理**：旧插件工具从 REGISTRY 移除，新插件工具被添加
4. **模块缓存清除**：reload 后 `entry_point.load()` 返回新模块内容（mock）
5. **无外部插件**：只有 builtin 时 reload 不报错

使用 pytest + monkeypatch 模拟 entry_points 和插件实例。

## 变更范围

| 文件 | 变更 |
|------|------|
| `src/bub/framework.py` | 新增 `_plugin_tools` dict；修改 `load_hooks()` 追踪工具；新增 `reload_plugins()` 方法 |
| `src/bub/builtin/tools.py` | 新增 `reload.plugins` 工具；更新 `show_help()` |
| `tests/test_reload_plugins.py` | 新增测试文件 |

预计 ~150 行新增代码。不涉及 hookspecs、channel 层或 CLI 命令的修改。

## 用户体验

用户在聊天中输入 `,reload.plugins` 触发（遵循 bub 的点号命名惯例，同 `fs.read`、`tape.info`）：

```
> ,reload.plugins
Plugins reloaded (3 ok, 0 failed):
  ✓ telegram
  ✓ gh
  ✓ skill-creator
```

失败时：

```
> ,reload.plugins
Plugins reloaded (2 ok, 1 failed):
  ✓ telegram
  ✓ gh
  ✗ my-plugin: ImportError - module 'foo' not found
```

## 附录：命令名称说明

内部命令通过 `_parse_internal_command` 解析，空格分隔命令名和参数。工具名中使用 `.`（如 `fs.read`、`tape.info`）是 bub 的惯例。使用 `reload.plugins` 作为工具名，用户输入 `,reload.plugins` 触发。

注意：用户输入 `,reload-plugins`（带横杠）时，由于横杠不是命令名的一部分且没有空格分隔，会被整体当作命令名 `reload-plugins` 查找 REGISTRY——找不到则 fallback 到 bash 执行。因此文档和 help 中统一引导用户使用 `,reload.plugins`。

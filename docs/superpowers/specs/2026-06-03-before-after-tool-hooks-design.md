# Tool Lifecycle Hooks (before_tool_call / after_tool_call)

**日期**: 2026-06-03
**状态**: 已批准
**范围**: 为 bub 工具执行添加前置/后置 hook 拦截点，支持插件检查、阻止或修改工具调用

## 背景

Bub 的工具通过 `REGISTRY` 直接分派给 republic 的 `ToolExecutor`，没有暴露工具执行生命周期的拦截点。插件无法在工具调用前后检查参数、阻止执行或覆盖结果。这限制了插件的能力（如限流、审计、结果后处理等）。

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| Hook 触发方式 | `call_many`（所有插件都通知） | 工具审计和观测类插件需要全部看到；阻止由结果中的 `block` 字段表示而非 `firstresult` |
| 阻止语义 | 任一插件可阻止 | 安全插件（限流、权限）必须能阻止危险操作 |
| 外部命令排除 | `_run_command`（`,` 前缀）不触发 hooks | 它绕过 republic 的工具机制，增加 hooks 只添复杂性无收益 |
| `after_tool_call` 覆盖策略 | 完全替换，非深度合并 | 简单可预测；插件拿到最终结果做全量替换 |
| 异常隔离 | 包装器内 try/except 捕获插件异常 | 插件失败不应中断工具执行 |
| `session_id` 传递 | 作为 `_run_once` 形式参数传入 | 调用链中可见且无需从 state 间接提取 |

## 设计

### 1. Hook 签名

`before_tool_call` — 工具执行前触发，可阻止：

```python
@hookspec
def before_tool_call(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str,
) -> dict[str, Any] | None:
```

- 返回 `{"block": True, "reason": "..."}` 阻止执行
- 返回 `None` 表示放行

`after_tool_call` — 工具执行后触发，可覆盖结果：

```python
@hookspec
def after_tool_call(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    session_id: str,
    is_error: bool,
) -> dict[str, Any] | None:
```

- 返回 `{"content": ..., "is_error": ..., "terminate": ...}` 覆盖结果
- 返回 `None` 保留原始结果
- `content` 覆盖：字符串换字符串，字典换字典，不做深度合并
- `is_error` 覆盖：设为 `True` 时将结果标记为错误（无论原始 handler 的执行结果如何）
- `terminate` 覆盖：设为 `True` 时终止当前对话循环

两个 hook 均不使用 `firstresult=True`，所有注册的插件都会收到通知。

### 2. `session_id` 传递

`session_id` 在 `run`、`run_stream`、`_agent_loop` 中均可获取，但未被传入 `_run_once`。需要给 `_run_once` 新增 `session_id: str` 形式参数，并更新其两个 overload 签名和所有调用点。

### 3. 工具包装 (`Agent._wrap_tools_with_hooks`)

在 `Agent._run_once()` 中，解析 tool 列表后、调用 `stream_events_async` 或 `run_tools_async` 之前，执行 `_wrap_tools_with_hooks(session_id=session_id, tools=tools)` 对每个 `Tool` 的 handler 进行包装：

```python
def _wrap_tools_with_hooks(
    self,
    *,
    session_id: str,
    tools: list[Tool],
    agent_state: _AgentState,
) -> list[Tool]:
```

包装器的闭包逻辑：

1. `try/except` 包裹调用 `hook_runtime.call_many("before_tool_call", ...)`
   （插件异常被捕获并记录日志，工具继续执行）
2. 如果任一结果包含 `block: True`，抛出 `RepublicError(ErrorKind.TOOL, reason)`
   → 由 republic 捕获并记录为工具错误结果，传递给 LLM 显示原因
3. 调用原始 handler（async-safe）
4. `try/except` 包裹调用 `hook_runtime.call_many("after_tool_call", ...)`
5. 应用覆盖：`content`、`is_error`、`terminate`

每个包装后的 tool 通过 `dataclasses.replace(tool, handler=wrapped_handler)` 创建。结果列表再经 `model_tools()` 处理（后者也使用 `replace`，会保留包装后的 handler）。

`ErrorKind` 需导入到 `agent.py` 的 `republic` 导入列表中。

### 4. 终止处理

新增 `_AgentState` 数据类，接收 `after_tool_call` 发出的终止信号：

```python
@dataclass
class _AgentState:
    tools_terminated: bool = False
```

此实例在 `_agent_loop` 中创建、传递给 `_wrap_tools_with_hooks`、被各包装 handler 闭包捕获。

**非 streaming 路径** (`_run_tools_with_auto_handoff`)：`_run_once` 返回后，`_resolve_tool_auto_result` 评估前，检查 `agent_state.tools_terminated`。若为 `True`，立即终止 step 循环（如有结果文本则返回，否则生成结束结果）。

**Streaming 路径** (`_stream_events_with_auto_handoff`)：`async for event in output` 循环消费完 `_run_once` 的所有事件后，检查 `agent_state.tools_terminated`。若为 `True`，立即 `return`（`_run_once` 流的 final 事件即为结束事件），不再进入下一个 step 循环。

注意：终止在当前轮所有工具执行完毕后才生效。终止轮的各个工具结果仍会被 LLM 看到。

### 5. HookRuntime

不新增方法。直接使用已有的 `HookRuntime.call_many()` —— 触发所有插件、收集结果、由调用方解读。

### 6. 错误处理

| 场景 | 行为 |
|------|------|
| `before_tool_call` 阻止 | RepublicError(ErrorKind.TOOL) → 工具错误结果，LLM 看到阻止原因 |
| Hook 插件抛出异常 | 工具包装器在 `call_many` 外 try/except 捕获；记录日志；工具按原始 handler 继续执行 |
| `after_tool_call` 异常 | 工具包装器在 `call_many` 外 try/except 捕获；记录日志；跳过覆盖，保留原始结果 |

## 变更范围

| 文件 | 变更 |
|------|------|
| `src/bub/hookspecs.py` | 新增 `before_tool_call`、`after_tool_call` hook 定义 |
| `src/bub/builtin/agent.py` | 新增 `_AgentState`、`_wrap_tools_with_hooks`；给 `_run_once` 加 `session_id` 参数；在 `run`/`run_stream`/agent loop 中集成 |
| `tests/test_hook_tool_lifecycle.py` | 新增测试文件 |

## 测试策略

新增 `tests/test_hook_tool_lifecycle.py`，覆盖：

1. **`test_before_tool_call_blocks_tool`** — hook 返回 `block: True`，工具不执行
2. **`test_before_tool_call_does_not_block`** — hook 返回 None，工具正常执行
3. **`test_after_tool_call_overrides_result`** — hook 返回 `{"content": "overridden"}`
4. **`test_after_tool_call_terminate`** — hook 返回 `{"terminate": True}`，循环停止
5. **`test_before_tool_call_multiple_plugins`** — 两个插件，第一个 block
6. **`test_tool_hooks_with_streaming`** — stream_output 路径中 hooks 同样触发
7. **`test_tool_hooks_parallel_safety`** — 并发工具调用各自获得 hooks
8. **`test_before_tool_call_plugin_error`** — hook 插件抛出异常，工具仍运行

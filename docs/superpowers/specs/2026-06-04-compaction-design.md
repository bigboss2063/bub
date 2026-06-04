# Bub Compaction 设计文档

## 背景

Bub 目前只有反应式 `auto_handoff`：当模型返回 `context_length_exceeded` 错误时，在 agent loop 中写入一个 anchor 截断历史，然后用原始 prompt 重试。这个机制有两个明显缺口：

1. **触发太晚** — 只有在模型已经报 context overflow 后才响应
2. **没有摘要** — 被截断的历史不会以任何形式重新进入上下文

Pi 已经实现了完整的 compaction 管线。本设计复用 bub 现有的 append-only tape 与 anchor 机制，将 anchor 升级为 compaction 的语义载体。

## 目标

- 用主动触发（threshold）替代纯反应式触发（overflow）
- 被截断的历史以结构化摘要的形式重新进入上下文
- 保留近期消息的原文（raw tail），避免过度摘要导致的细节丢失
- 支持增量更新（`previousSummary`）
- 不保留任何旧 handoff 语义

## 非目标

- 支持用户自定义 compaction 策略（未来可通过 hook 扩展）
- 支持多模型摘要（复用主 LLM）
- 保留旧 tape 中的非标准 anchor 截断语义

---

## 架构

### Anchor 语义扩展

Anchor 的语义从单一的"上下文截断重置"扩展为"上下文重启点"：

| Anchor 类型 | 语义 | Context Builder 行为 |
|------------|------|---------------------|
| `session/start` | 会话起点 | 正常渲染其后的所有消息 |
| `compaction/*` | Compaction 重启点 | 渲染 summary 为 user 消息，然后渲染保留的近期消息，再接 compaction 之后的消息 |
| 其他（如 `phase-1`） | 普通标记 | 当作普通消息渲染，不参与截断 |

系统中只有 `session/start` 和 `compaction/*` 两种具有截断语义的 anchor。`session/start` 是会话起点自动写入的。用户/模型无法创建裸截断 anchor。

### Compaction Anchor State

```python
{
    "summary": str,              # 结构化摘要文本
    "first_kept_entry_id": int,  # 保留消息的起始 entry ID
    "tokens_before": int,        # compaction 前的 token 数
    "details": {                 # 可选：结构化元数据
        "read_files": list[str],
        "modified_files": list[str],
    },
    "trigger": str,              # "overflow" | "threshold" | "manual"
}
```

### Context Rebuild

当 context builder 遇到 `compaction/*` anchor 时，重建的 LLM 消息序列：

```
[system prompt]                    ← 正常 system prompt
[user]: <compaction-summary>       ← summary 渲染为 user 消息
[user]: <kept-msg-1>               ← first_kept_entry_id 开始的保留消息
[assistant]: <kept-msg-2>
...
[user]: <msg-after-compaction>     ← compaction anchor 之后的消息
[assistant]: <msg-after-compaction>
```

Summary 消息的包装格式：

```xml
<compaction-summary>
<tokens-before>{tokens_before}</tokens-before>
<summary>
{summary}
</summary>
</compaction-summary>
```

旧 anchor（非 `session/start` 且非 `compaction/*`）渲染为：

```
[Anchor: {name}]
{state_json}
```

### TapeContext

**不需要修改** `TapeContext` 的定义。它继续负责查询策略（从哪个 anchor 开始）。`first_kept_entry_id` 存储在 compaction anchor 的 `state` 中，由 context builder 在渲染时读取。

---

## 触发条件

### 主动触发（Threshold）

每次 assistant 响应后，检查 `usage.total_tokens`：

```python
if usage and usage.total_tokens > context_window - reserve_tokens:
    trigger_compaction(reason="threshold")
```

如果 usage 不可用（如错误响应），从 agent state 的消息中估算：找到最后一条有 usage 的 assistant 消息，估算从那条消息到当前消息的 token 增量。

### 反应式触发（Overflow）

当模型返回 context overflow 错误时：

```python
if is_context_overflow(error):
    # 1. 从 agent state 中移除错误消息
    # 2. 触发 compaction (reason="overflow")
    # 3. 用原始 prompt 重试
```

### 手动触发

用户通过 `tape.compact` 工具或 `/compact` 命令触发。

---

## Cut Point 选择

从 pi 移植的算法：

1. **Walk backwards**：从最新消息开始，累积估算 token 数（字符数/4 启发式）
2. **Stop at keep_recent_tokens**：当累积 >= `keep_recent_tokens` 时停止
3. **Find valid cut point**：找到最近的有效切割点
4. **Handle split turn**：如果切割点落在 assistant 消息中间，记录 turn start

### 有效切割点

- `message` kind 的 user/assistant 消息
- **不能**切在 `tool_call` 和 `tool_result` 之间（必须保持配对）

### Token 估算

```python
def estimate_tokens(message: dict) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content) // 4
    return sum(len(part.get("text", "")) for part in content) // 4
```

---

## Summary 生成

### Prompt 结构

**初始摘要**（无 previous summary）：

```
You are a context summarization assistant. Summarize the conversation below
into a structured summary. Do NOT continue the conversation. Do NOT respond
to any questions. ONLY output the structured summary.

<conversation>
[User]: ...
[Assistant]: ...
...
</conversation>

Format:
## Goal
## Constraints & Preferences
## Progress
- Done: ...
- In Progress: ...
- Blocked: ...
## Key Decisions
## Next Steps
## Critical Context
```

**增量更新**（有 previous summary）：

```
The messages above are NEW conversation messages to incorporate into the
existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- PRESERVE exact file paths
```

### Split Turn 处理

当切割点落在 assistant 消息中间时：

1. 生成 **history summary**（完整 turns 的摘要）
2. 生成 **turn prefix summary**（被切割 turn 的前缀摘要）
3. 合并：

```
{history_summary}

---

**Turn Context (split turn):**

{turn_prefix_summary}
```

### 消息序列化

在送入 summary LLM 之前，消息被序列化为纯文本格式，防止模型将其当作对话继续：

```
[User]: message text
[Assistant]: response text
[Assistant tool calls]: fs.read(path="..."); fs.edit(path="...")
[Tool result]: output (truncated to 2000 chars)
```

### 文件操作跟踪

从 assistant 消息的 tool calls 中提取：

```python
def extract_file_operations(messages: list[dict]) -> FileOperations:
    read_files = set()
    modified_files = set()
    for msg in messages:
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            for call in msg["tool_calls"]:
                func = call.get("function", {})
                name = func.get("name", "")
                if name == "fs.read":
                    read_files.add(extract_path(func))
                elif name in ("fs.write", "fs.edit"):
                    modified_files.add(extract_path(func))
    return FileOperations(read=read_files, modified=modified_files)
```

Summary 末尾附加 XML：

```xml
<read-files>
path/to/file1.ts
</read-files>

<modified-files>
path/to/changed.ts
</modified-files>
```

---

## `previousSummary` 增量更新

当 tape 上已存在 compaction anchor 时：

1. 找到最新的 compaction entry
2. 读取其 `summary` 作为 `previousSummary`
3. 将 `boundaryStart` 设为该 compaction 的 `first_kept_entry_id`
4. 新 compaction 的摘要范围从 `boundaryStart` 到当前 cut point

这意味着：
- 之前保留的消息会被**重新摘要**进新的 summary
- Summary 是**累积/迭代**的，不是每次都从头开始

---

## Agent Loop 集成

### 非流式路径

```python
async def _run_tools(self, tape, prompt, ...):
    for step in range(1, self.settings.max_steps + 1):
        # ... 调用模型 ...
        
        # 检查是否需要 compaction
        if usage and should_compact(usage.total_tokens, context_window, settings):
            await self._compact(tape, reason="threshold")
            # 继续循环，不中断
        
        # 处理 tool calls ...
```

### Overflow 处理

```python
if is_context_overflow(error):
    # 移除错误消息
    # 触发 compaction
    await self._compact(tape, reason="overflow")
    # 重试
    continue
```

### 流式路径

类似，在流结束后检查 usage。

---

## TapeService API

```python
class TapeService:
    async def compact(
        self,
        tape_name: str,
        *,
        reason: str = "manual",
        instructions: str | None = None,
    ) -> dict:
        """Run compaction on a tape.
        
        Returns the compaction anchor state.
        """
        
    async def handoff(self, tape_name: str, *, name: str, state: dict | None = None) -> list[TapeEntry]:
        """DEPRECATED: Use compact() instead.
        
        For non-compaction anchors, still writes a plain anchor.
        """
```

### 工具替换

- `tape.handoff` → `tape.compact`
- `tape.info` 返回的信息更新：显示 compaction 统计

---

## 配置

```python
@dataclass
class CompactionSettings:
    enabled: bool = True
    reserve_tokens: int = 16384      # 触发阈值 = context_window - reserve_tokens
    keep_recent_tokens: int = 20000  # 保留的近期消息 token 数
```

配置来源（优先级从高到低）：

1. 环境变量：`BUB_COMPACTION_ENABLED`, `BUB_COMPACTION_RESERVE_TOKENS`, `BUB_COMPACTION_KEEP_RECENT_TOKENS`
2. `bub configure` 交互配置
3. 默认值

---

## 错误处理

| 场景 | 行为 |
|------|------|
| Summary LLM 失败 | 退化为"有 recent tail 但无 summary"的 compaction anchor（state["summary"] = "Compaction failed: {error}"） |
| 没有可压缩的内容 | 跳过，不写入 anchor |
| 已经是 compaction 边界 | 跳过（防止重复 compaction） |
| Overflow 后 compaction 失败 | 重试一次，再失败则抛出错误 |

---

## 代码结构

```
src/bub/builtin/
├── compaction/           # 新增：compaction 模块
│   ├── __init__.py       # 导出 public API
│   ├── core.py           # 核心逻辑
│   │   ├── should_compact()
│   │   ├── find_cut_point()
│   │   ├── prepare_compaction()
│   │   ├── generate_summary()
│   │   └── compact()
│   ├── utils.py          # token 估算、消息序列化、文件操作提取
│   └── types.py          # CompactionSettings, CutPointResult, FileOperations
├── agent.py              # 修改：集成 compaction 触发
├── context.py            # 修改：识别 compaction/* anchor，渲染 summary
├── tape.py               # 修改：TapeService.compact()
└── tools.py              # 修改：tape.compact 替换 tape.handoff
```

---

## 测试策略

### 单元测试（`tests/compaction/`）

- `test_cut_point.py` — cut point 选择逻辑
  - 正常切割
  - Split turn 处理
  - Tool call/tool_result 配对保护
- `test_token_estimate.py` — token 估算
- `test_context_rebuild.py` — context builder 的 compaction 渲染
  - `compaction/*` anchor 渲染为 summary 消息
  - 保留消息正确渲染
  - 旧 anchor 当作普通消息
- `test_file_operations.py` — 文件操作提取

### 集成测试（`tests/test_agent.py`）

- 模拟 overflow 场景，验证 compaction 触发
- 模拟 threshold 场景，验证主动触发
- 验证 summary 进入上下文

### 端到端测试

- 长会话自动 compaction
- 手动 `/compact` 命令
- Overflow 恢复

---

## 与 Pi 的差异

| 方面 | Pi | Bub |
|------|-----|-----|
| 存储结构 | `CompactionEntry`（专用 entry 类型） | `TapeEntry.anchor`（复用 anchor） |
| 消息类型 | `compactionSummary`（自定义消息类型） | 直接渲染为 user 消息 |
| 会话结构 | 树形（`id`/`parentId`） | 线性 append-only |
| 扩展机制 | `session_before_compact` / `session_compact` 事件 | 未来可通过 hook 扩展 |
| 文件操作工具 | `read`, `edit`, `write` | `fs.read`, `fs.edit`, `fs.write` |
| 摘要 LLM | 可配置（默认主 LLM） | 复用主 LLM |

---

## 迁移说明

- 旧 tape 中的 `auto_handoff/*` anchor 不再具有截断语义，当作普通消息渲染
- 旧 tape 中的非标准 anchor（如 `phase-1`）当作普通消息渲染
- `tape.handoff` 工具标记为 deprecated，替换为 `tape.compact`
- 不需要自动迁移旧 tape

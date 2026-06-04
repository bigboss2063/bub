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
- 公共 API 统一为 handoff（compaction 是 handoff 的内部算法实现）

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
    "summary": str,                # 结构化摘要文本
    "last_entry_before": int,      # compaction 前最后一条条目的 entry ID
    "tokens_before": int,          # compaction 前的 token 数
    "details": {                   # 可选：结构化元数据
        "read_files": list[str],
        "modified_files": list[str],
    },
    "trigger": str,                # "overflow" | "threshold" | "manual"
}
```

`last_entry_before` 记录 compaction 发生前 tape 上最后一条条目的 ID。Selector 用这个值区分"被压缩的旧消息"（ID <= last_entry_before）和"compaction 后的新消息"（ID > last_entry_before）。

### Context Rebuild

当 selector 检测到 `context.state` 中存在 compaction 元数据时，重建的 LLM 消息序列：

```
[system prompt]                    ← 正常 system prompt
[user]: <compaction-summary>       ← summary 渲染为 user 消息
[user]: <kept-msg-1>               ← last_entry_before 之后的保留消息
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

### TapeContext 与 Selector 交互

**核心问题**：`TapeContext` 的 `LAST_ANCHOR` 查询返回 anchor **之后**的条目（跳过 anchor 本身）。Selector 无法直接访问 compaction anchor 的 state。

**解决方案**：Compaction 完成后，将 compaction 元数据注入到 `tape.context.state` 中，供 selector 读取：

```python
# compact() 完成后
tape.context = replace(tape.context, state={
    **tape.context.state,
    "compaction_summary": summary_text,
    "compaction_last_entry_before": last_entry_before_id,
})
```

Selector（`_select_messages`）在渲染前检查 `context.state`：

```python
def _select_messages(entries, context):
    messages = []

    # 如果有 compaction summary，先渲染为 user 消息
    if compaction_summary := context.state.get("compaction_summary"):
        last_before = context.state["compaction_last_entry_before"]
        messages.append({
            "role": "user",
            "content": _render_compaction_summary(compaction_summary, context.state),
        })
        # 只渲染 last_entry_before 之后的条目
        entries = [e for e in entries if e.id > last_before]

    # 正常渲染
    for entry in entries:
        match entry.kind:
            case "anchor": ...
            case "message": ...
            ...
    return messages
```

**Tape 语义不变**：
- Tape 本身：append-only，compaction anchor 正常写入
- 查询机制不变：依然是 `LAST_ANCHOR`，返回 anchor 之后的条目
- 变的只是 selector 的渲染逻辑——通过 `context.state` 知道存在 compaction summary

**注意**：Token 估算使用 `len(content) // 4` 的启发式。这对 ASCII 文本是合理的近似，但对 CJK 内容会低估 token 数（CJK 字符通常映射到 1-2 个 token，而非 0.25 个）。这意味着阈值触发可能比预期稍晚。作为 v1 的已知限制接受，未来可引入更精确的 tokenizer。

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

用户通过 `tape.compact` 工具或 `/compact` 命令触发。可附加 `instructions` 参数，传递到摘要 prompt 的末尾作为额外指示：

```
Additional focus: {instructions}
```

---

## Cut Point 选择

从 pi 移植的算法：

1. **确定扫描范围**：如果存在旧 compaction，扫描范围为旧 `last_entry_before` 到当前末尾；否则从 tape 起点到末尾
2. **Walk backwards**：从最新消息开始，累积估算 token 数（字符数/4 启发式）
3. **Stop at keep_recent_tokens**：当累积 >= `keep_recent_tokens` 时停止
4. **Find valid cut point**：找到最近的有效切割点
5. **Handle split turn**：如果切割点落在 assistant 消息中间，记录 turn start

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

### 增量更新边界情况

当新 cut point 落在旧 kept 范围**之内**时（即新的 cut point 比旧的 `last_entry_before` 更靠后）：

- `boundaryStart` 仍设为旧 compaction 的 `last_entry_before`
- `findCutPoint()` 在 `boundaryStart` 到当前末尾之间寻找新切割点
- 旧 kept 范围内的消息被**重新摘要**进新的 summary
- 摘要使用**增量更新**（`UPDATE_SUMMARIZATION_PROMPT`），将旧 summary 作为 `<previous-summary>` 传入

这意味着 summary 是**累积/迭代**的，不是每次都从头开始。

---

## Summary 生成

### Summary LLM 调用

Summary 通过 `LLM.chat_async()` 以 off-tape 方式生成：

```python
summary = await llm.chat_async(
    messages=[{"role": "user", "content": summary_prompt}],
    system_prompt=SUMMARIZATION_SYSTEM_PROMPT,
    tape=None,       # off-tape，不写入任何 tape
    max_tokens=4096,
)
```

`tape=None` 时不会产生 tape 记录，确保 summary 调用不会污染主 tape。

### 超时处理

Summary 调用使用 `asyncio.timeout(120)` 包裹（120 秒超时）。超时后退化为无摘要 anchor：

```python
try:
    async with asyncio.timeout(120):
        summary = await generate_summary(...)
except (TimeoutError, asyncio.TimeoutError):
    summary = f"Compaction failed: summary generation timed out after 120s"
```

### System Prompt

```
You are a context summarization assistant. Your task is to read a conversation
between a user and an AI coding assistant, then produce a structured summary
following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the
conversation. ONLY output the structured summary.
```

### Prompt 结构

**初始摘要**（无 previous summary）：

```
<conversation>
[User]: message text
[Assistant]: response text
[Assistant tool calls]: fs.read(path="..."); fs.edit(path="...")
[Tool result]: output (truncated to 2000 chars)
</conversation>

The messages above are a conversation to summarize. Create a structured context
checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]

Keep each section concise. Preserve exact file paths, function names, and
error messages.
```

**增量更新**（有 previous summary）：

```
<conversation>
[User]: ...
[Assistant]: ...
</conversation>

<previous-summary>
{previous_summary}
</previous-summary>

The messages above are NEW conversation messages to incorporate into the
existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use the same format as above.
```

### Split Turn 处理

当切割点落在 assistant 消息中间时，执行**两次并发 LLM 调用**（通过 `asyncio.gather`）：

1. **History summary**（`generate_summary`）：完整 turns 的摘要
2. **Turn prefix summary**（`generate_turn_prefix_summary`）：被切割 turn 的前缀摘要

Turn prefix summary 的 prompt：

```
This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent
work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix.
```

两次调用完成后，代码拼接（不使用 LLM 合并）：

```python
summary = f"{history_result}\n\n---\n\n**Turn Context (split turn):**\n\n{turn_prefix_result}"
```

### 消息序列化

在送入 summary LLM 之前，消息被序列化为纯文本格式，防止模型将其当作对话继续：

```
[User]: message text
[Assistant]: response text
[Assistant tool calls]: fs.read(path="..."); fs.edit(path="...")
[Tool result]: output (truncated to 2000 chars)
```

**工具结果截断**：工具输出截断到 2000 字符，超出部分用 `... (truncated)` 替代。

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

## Compaction 操作时序

完整操作序列：

```
1. Agent loop 检测到触发条件（threshold/overflow/manual）
2. 调用 tape_service.handoff(tape_name, reason=reason)
3. handoff() 内部调用 compaction 模块：
   a. 查询 tape 上所有条目（tape.query_async.all()）
   b. 查找最新 compaction anchor（如果有）
      - 读取其 state.summary 作为 previousSummary
      - 读取其 state.last_entry_before 作为 boundaryStart
   c. find_cut_point(entries, boundaryStart, keep_recent_tokens)
      - 返回 cut_index, is_split_turn, turn_start_index
   d. 提取待摘要消息：entries[boundaryStart:cut_index]
   e. 序列化消息为纯文本
   f. 提取文件操作（read_files, modified_files）
   g. 如果 is_split_turn:
        - asyncio.gather(generate_summary(...), generate_turn_prefix_summary(...))
        - 拼接两个摘要
      否则:
        - generate_summary(...) （使用增量更新 prompt 如果有 previousSummary）
   h. 计算 last_entry_before = entries[cut_index - 1].id
   i. 写入 compaction anchor：
      tape.handoff_async("compaction/v1", state={
          "summary": summary,
          "last_entry_before": last_entry_before,
          "tokens_before": total_tokens,
          "details": {"read_files": ..., "modified_files": ...},
          "trigger": reason,
      })
4. handoff() 返回 CompactionResult(summary, last_entry_before, tokens_before)
5. Agent loop 更新 tape context：
   tape.context = replace(tape.context, state={
       **tape.context.state,
       "compaction_summary": result.summary,
       "compaction_last_entry_before": result.last_entry_before,
   })
6. 如果是 overflow 触发：用原始 prompt 重试
7. 如果是 threshold 触发：继续循环
```

---

## Agent Loop 集成

### 非流式路径

```python
async def _run_tools(self, tape, prompt, ...):
    for step in range(1, self.settings.max_steps + 1):
        # ... 调用模型 ...

        # 检查是否需要 handoff
        if usage and should_compact(usage.total_tokens, context_window, settings):
            result = await self.tapes.handoff(tape.name, reason="threshold")
            tape.context = replace(tape.context, state={
                **tape.context.state,
                "compaction_summary": result.summary,
                "compaction_last_entry_before": result.last_entry_before,
            })
            # 继续循环，不中断

        # 处理 tool calls ...
```

### Overflow 处理

```python
if is_context_overflow(error):
    # 移除错误消息
    # 触发 handoff
    result = await self.tapes.handoff(tape.name, reason="overflow")
    tape.context = replace(tape.context, state={
        **tape.context.state,
        "compaction_summary": result.summary,
        "compaction_last_entry_before": result.last_entry_before,
    })
    # 重试
    continue
```

### 流式路径

类似，在流结束后检查 usage。

---

## TapeService API

```python
@dataclass(frozen=True)
class CompactionResult:
    summary: str
    last_entry_before: int
    tokens_before: int

class TapeService:
    async def handoff(
        self,
        tape_name: str,
        *,
        reason: str = "manual",
        instructions: str | None = None,
    ) -> CompactionResult:
        """Run handoff (compaction) on a tape.

        1. Reads all tape entries
        2. Finds cut point and generates summary (off-tape LLM call)
        3. Writes compaction anchor
        4. Returns result for caller to update tape.context

        The caller (agent loop) is responsible for updating tape.context
        with the returned compaction metadata.
        """
```

### 工具

- `tape.handoff` — 触发 handoff/compaction，可附加 `instructions` 参数

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
| Summary LLM 超时（120s） | 退化为"有 recent tail 但无 summary"的 compaction anchor（state["summary"] = "Compaction failed: summary generation timed out after 120s"） |
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
│   │   ├── generate_turn_prefix_summary()
│   │   └── compact()
│   ├── utils.py          # token 估算、消息序列化、文件操作提取
│   └── types.py          # CompactionSettings, CompactionResult, CutPointResult, FileOperations
├── agent.py              # 修改：集成 compaction 触发，更新 tape.context
├── context.py            # 修改：selector 识别 compaction state，渲染 summary
├── tape.py               # 修改：TapeService.handoff() 返回 CompactionResult
└── tools.py              # 修改：tape.handoff 工具触发 compaction
```

---

## 测试策略

### 单元测试（`tests/compaction/`）

- `test_cut_point.py` — cut point 选择逻辑
  - 正常切割
  - Split turn 处理
  - Tool call/tool_result 配对保护
  - 增量更新边界情况（新 cut point 在旧 kept 范围内）
- `test_token_estimate.py` — token 估算
- `test_context_rebuild.py` — selector 的 compaction 渲染
  - context.state 中有 compaction_summary 时渲染为 user 消息
  - 只渲染 last_entry_before 之后的条目
  - context.state 中无 compaction_summary 时正常渲染
  - 旧 anchor 当作普通消息
- `test_file_operations.py` — 文件操作提取

### 集成测试（`tests/test_agent.py`）

- 模拟 overflow 场景，验证 compaction 触发
- 模拟 threshold 场景，验证主动触发
- 验证 summary 进入上下文
- 验证 tape.context 在 compaction 后正确更新

### 端到端测试

- 长会话自动 compaction
- 手动 `/compact` 命令
- Overflow 恢复

---

## 与 Pi 的差异

| 方面 | Pi | Bub |
|------|-----|-----|
| 存储结构 | `CompactionEntry`（专用 entry 类型） | `TapeEntry.anchor`（复用 anchor） |
| 消息类型 | `compactionSummary`（自定义消息类型） | Selector 渲染为 user 消息（通过 `context.state`） |
| 会话结构 | 树形（`id`/`parentId`） | 线性 append-only |
| Compaction 元数据传递 | `CompactionEntry` 在 entry 流中，context builder 直接读取 | 通过 `TapeContext.state` 注入，selector 读取 |
| 扩展机制 | `session_before_compact` / `session_compact` 事件 | 未来可通过 hook 扩展 |
| Split turn 摘要 | 两次并发 LLM 调用 + 代码拼接 | 同 Pi |
| 文件操作工具 | `read`, `edit`, `write` | `fs.read`, `fs.edit`, `fs.write` |
| 摘要 LLM | 可配置（默认主 LLM） | 复用主 LLM |
| Summary LLM 调用 | 通过独立的 LLM 客户端 | `llm.chat_async(tape=None)` off-tape 调用 |

---

## 命名约定

- **handoff** — 公共操作名称（API、工具名、日志）。含义：往 tape 写入 anchor，重启上下文
- **compaction** — 内部算法名称（模块、类型、anchor name）。含义：摘要 + 截断的实现细节

这避免了"代码里到处是 handoff 但设计说 handoff 被废弃了"的矛盾。

## 迁移说明

- 旧 tape 中的 `auto_handoff/*` anchor 不再具有截断语义，当作普通消息渲染
- 旧 tape 中的非标准 anchor（如 `phase-1`）当作普通消息渲染
- `tape.handoff` 工具升级为带摘要的 handoff（内部使用 compaction 算法）
- 不需要自动迁移旧 tape

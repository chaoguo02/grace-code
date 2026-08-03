# P0 #1: 统一上下文预算系统 — 实施计划

> 状态：计划阶段 | 日期：2026-08-01
> 来源：跨模块安全审计 — Context 模块 P0 发现
> 预计工作量：3–5 人日（核心）+ 2–4 人日（结构化 token 计量）
> 前置依赖：provider token estimator 完善

---

## 1. 问题陈述

### 1.1 根因

当前系统存在**三条上下文装配路径**，在 `agent/core.py::_build_messages()` 中分流：

| 路径 | 入口函数 | 触发条件 | 预算/裁剪 |
|------|---------|---------|-----------|
| 主会话 | `build_request_messages()` | `is_subagent=False, _inherited_context=None` | ✅ 完整 |
| 子 Agent | `build_sub_agent_messages()` | `is_subagent=True` | ❌ 绕过 |
| 继承上下文 | `build_inherited_messages()` | `_inherited_context is not None` | ❌ 绕过 |

子 Agent 和继承路径直接拼接历史消息，不执行 `trim_history()`、语义压缩、`max_context_window` 强制和 Repo Map 裁剪。一个 40 条、每条数万字符的历史可以未经裁剪直接发送给 provider，触发 HTTP 400。

辅助问题：`estimate_tokens()` 类型约束为 `str`，但调用方可能传入 `ContentBlock[]` 或 `Message` 对象；`ContextLayer.max_tokens` 字段已声明但零生产调用；Artifact ID 仅对前 1000 字符计算 SHA-256，可能发生碰撞覆盖。

### 1.2 受影响文件

| 文件 | 当前行为 | 需要变更 |
|------|---------|---------|
| `context/manager.py:231` `build_request_messages()` | 完整预算管线 | 重命名为 `_build_main_messages()` |
| `context/manager.py:395` `build_sub_agent_messages()` | 零裁剪 | 合并入统一入口 |
| `context/manager.py:430` `build_inherited_messages()` | 快照+本地历史无裁剪 | 合并入统一入口 |
| `agent/core.py:3108` `_build_messages()` | 三路分流 | 替换为单一调用 |
| `context/token_budget.py:37` `estimate_tokens()` | 仅接受 `str` | 统一为 `str \| ContentBlock[] \| Message` |
| `context/structured.py:38` `ContextLayer.max_tokens` | 死代码 | 在装配时强制 |
| `context/artifacts.py:198` `maybe_externalize()` | 前缀哈希 | 全内容哈希 |

---

## 2. 目标架构

```
所有请求路径
  └─> finalize_request(messages: list[Message], model_limit: int) -> list[Message]
        ├─ 1. 按 provider 精确计算 token (unified estimator)
        ├─ 2. 若超限 → 逐层降级:
        │     ├─ pair-aware trim_history (保留 tool_use/tool_result 配对)
        │     ├─ semantic compaction (LLM 驱动摘要)
        │     └─ 硬截断 (保留 system + 最近 N 条)
        ├─ 3. 逐层强制 ContextLayer.max_tokens (render_with_budget)
        ├─ 4. 记录裁剪原因、原始/保留 token 到 ContextStats
        └─ 5. 返回最终消息列表
```

**不变量**：任何消息列表在发送给 provider 之前，必须经过 `finalize_request()` 的 token 计量和裁剪守卫。

---

## 3. 实施步骤

### Step 1: 统一 token 估算入口 (1–1.5 人日)

**文件**: `context/token_budget.py`

**3.1.1** 删除当前仅接受 `str` 的 `estimate_tokens(text: str)` 公共函数，替换为：

```python
from typing import Union
from core.types import ContentBlock, Message

def estimate_tokens(
    content: Union[str, list[ContentBlock], Message, list[Message]],
    *,
    provider: str | None = None,
) -> int:
    """Unified token estimator for all content types.

    - str: character-based estimation with provider-specific encoding fallback
    - ContentBlock[]: per-block estimation (text/image/document/tool_call/tool_result)
    - Message: estimates message.content + metadata overhead
    - Message[]: sum of individual message estimates
    """
```

**3.1.2** 为 `image`、`document`、`tool_call`、`tool_result` block 建立 provider-specific 估算器。至少覆盖 Anthropic 和 OpenAI/DeepSeek 两家。

**3.1.3** 在发送前（`finalize_request` 内部）调用 `estimate_tokens(messages, provider=active_provider)` 获取精确值，用于硬性窗口守卫。

**验证**: 构造包含 3 个 image block 的消息列表，估算值不再接近 `3 // 4`，而是与 provider 的实际计费 token 误差在 ±5% 以内。

### Step 2: 统一上下文装配入口 (1–1.5 人日)

**文件**: `context/manager.py`, `agent/core.py`

**3.2.1** 在 `ContextManager` 中新增 `finalize_request()`:

```python
def finalize_request(
    self,
    messages: list[dict],
    *,
    model_limit: int,
    token_budget: TokenBudget,
    consumed_tokens: int,
    enable_caching: bool = False,
    provider: str | None = None,
) -> RequestContext:
    """Single entry point for all context assembly.

    Guards:
    1. compute_plan -> trim_history (pair-aware 4-level)
    2. Semantic compaction if > 80% budget
    3. Hard truncation as last resort
    4. ContextStats with trim reason + original/retained tokens
    """
```

**3.2.2** 修改 `_build_messages()`（`agent/core.py:3108`）为三路统一调用：

```python
async def _build_messages(self, ...) -> list[dict]:
    # Step 1: build raw messages per path
    if self._inherited_context is not None:
        raw_messages = self._build_inherited_raw(snapshot, history)
    elif self._cfg.is_subagent:
        raw_messages = self._build_sub_agent_raw(history, system)
    else:
        raw_messages = self._build_main_raw(...)  # renamed from build_request_messages

    # Step 2: ALWAYS apply final budget gate
    ctx = self._context_manager.finalize_request(
        raw_messages,
        model_limit=self._effective_model_limit,
        token_budget=self._token_budget,
        consumed_tokens=self._consumed_tokens,
        provider=self._backend.provider_name,
    )
    return ctx.messages
```

**3.2.3** `build_sub_agent_messages()` 和 `build_inherited_messages()` 重命名为 `_build_sub_agent_raw()` 和 `_build_inherited_raw()`，职责收缩为"构建原始消息列表"，不再作为最终返回。其系统提示渲染、长期记忆注入等逻辑上移至 `_build_messages()` 中的 raw 构建阶段。

**验证**: 构造 200K token 的历史，启动子 Agent；确认裁剪后的消息列表不超过模型窗口，且保留了 system prompt + 最近的 tool_use/tool_result 配对。

### Step 3: 强制 ContextLayer max_tokens (0.5–1 人日)

**文件**: `context/structured.py`

**3.3.1** 在 `StructuredContext` 中新增 `render_with_budget()`:

```python
def render_with_budget(
    self,
    budget_map: dict[str, int],  # layer_name -> max_tokens
    enable_caching: bool = False,
) -> tuple[str, dict[str, int]]:
    """Render all layers, enforcing per-layer token caps.

    Returns (rendered_text, {layer_name: actual_tokens_used}).
    """
```

**3.3.2** 每个 `ContextLayer` 实现裁剪：如果 `max_tokens > 0` 且内容超限，执行优先级裁剪（保留前 N 行 + 截断标记），并记录裁剪原因到 debug 日志。

**3.3.3** "由外部 budget 控制"的注释应更新以反映新行为：`max_tokens=0` 仍为"不强制"，但正数值现在实际生效。

**验证**: 设置 `ContextLayer(name="repo_map", max_tokens=500)`，确认系统提示中的 repo map 被截断至 ~500 tokens。

### Step 4: 修复 Artifact ID 碰撞 (0.5–1 人日)

**文件**: `context/artifacts.py`

**3.4.1** `maybe_externalize()` 中的哈希改为全内容 SHA-256：

```python
# 当前 (line 198): key = hashlib.sha256(content[:1000].encode()).hexdigest()
# 修复后:
key = hashlib.sha256(content.encode()).hexdigest()
```

如果担心大内容性能，使用增量式 `hashlib.sha256()` + `update()` 循环 64KB 块。

**3.4.2** `ArtifactEntry` 增加 `original_length: int` 字段，区分"原始输出大小"与"保留在上下文中的引用大小"，用于调试和预算追踪。

**3.4.3** 向 `ArtifactStore` 的写入路径（`put()`）增加 `if key in self._cache: raise ArtifactCollisionError`，防止静默覆盖。碰撞时应记录旧条目和新条目的前 200 字符，便于诊断。

**验证**: 构造两个前 1000 字符相同但后续不同的 10KB 输出；确认它们获得不同的 Artifact ID，且均保存成功。

### Step 5: 回归测试与 Provider 边界测试 (1 人日)

**3.5.1** 新增测试：
- `test_sub_agent_trims_history_when_exceeds_window()` — 超过 200K token 的子 Agent 历史被裁剪
- `test_inherited_context_enforces_model_limit()` — 继承上下文不绕过窗口守卫
- `test_estimate_tokens_handles_multimodal_blocks()` — image/document block 的 token 估算值合理
- `test_context_layer_max_tokens_enforced()` — 正数 max_tokens 实际生效
- `test_artifact_no_collision_on_prefix_match()` — 前 1000 字符相同但内容不同的 Artifact 不碰撞

**3.5.2** 与真实 provider 进行集成测试：
- 发送接近窗口大小的消息，确认返回 200 而非 400
- 发送包含 image block 的消息（如已支持），确认 token 计数与 provider 返回的 `usage` 一致

---

## 4. 风险与回滚

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 裁剪后的消息丢失关键上下文，导致 Agent 行为退化 | 中 | 高 | trim_history 保留 system + 最近配对；compact 做 LLM 摘要而非静默删除；增加 debug 日志记录裁剪量 |
| unified estimator 对某些 block 类型估算不准 | 中 | 中 | 保留 5% 安全边距；使用 provider 返回的实际 usage 做校验日志 |
| finalize_request 成为性能瓶颈 | 低 | 中 | trim_history 已有 O(n) 实现；仅当估算超限时执行完整计量 |
| 子 Agent 此前隐式“全能读取历史”，裁剪后遗漏信息 | 低 | 中 | 裁剪策略保留 system + 最近的 tool 配对；初始可保守（仅截断，不压缩） |

**回滚方式**: `_build_messages()` 中的分流逻辑保留为 feature flag `UNIFIED_BUDGET_GATE`，默认开启。如果生产问题，关闭 flag 即可回退到旧路径。

---

## 5. 验证清单

- [ ] `estimate_tokens([image_block, text_block])` 返回值与 Anthropic API 返回的 `usage.input_tokens` 误差 < 5%
- [ ] 子 Agent + 200K 超长历史 → 裁剪后消息数 ≤ 模型窗口
- [ ] 继承上下文 + 100K 快照 + 50K 本地历史 → 裁剪后总 token ≤ model_limit
- [ ] `ContextLayer.max_tokens=500` → 该层内容 ≤ 500 tokens
- [ ] 两个前 1000 字符相同、后续不同的 10KB 输出 → 不同 Artifact ID，均保存
- [ ] 68 项现有测试保持通过
- [ ] 主会话路径行为不变（回归）
- [ ] 向真实 Anthropic 和 DeepSeek endpoint 发送接近窗口限制的请求，均返回 200

---

## 6. 文件变更汇总

| 文件 | 变更类型 | 行数估计 |
|------|---------|---------|
| `context/token_budget.py` | 重构 `estimate_tokens()`，新增 provider-specific 估算器 | +120/-30 |
| `context/manager.py` | 新增 `finalize_request()`；重命名两个 bypass 函数 | +80/-20 |
| `context/structured.py` | 新增 `render_with_budget()`；强制 max_tokens | +60/-10 |
| `context/artifacts.py` | 全内容哈希；ArtifactCollisionError；original_length | +30/-10 |
| `agent/core.py` | `_build_messages()` 重构为单入口 + raw 构建 | +40/-30 |
| `tests/test_context_budget.py` | 新增 5 个测试 | +150 |
| `tests/test_artifact_store.py` | 新增碰撞测试 | +40 |
| **总计** | | **~520/+120** |

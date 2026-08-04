# 工具系统 Async 化——背道而驰盘点 + 替换方案（v4）

> 日期：2026-08-04
> v4：盘点现有实现与 CC 背道而驰的部分，每个给完整替换方案，拆步骤逐步执行

---

## 一、背道而驰盘点——现有实现 vs CC

### 盘点的 5 个根本分歧

| # | 维度 | 我们现状 | CC 真实 | 背道而驰 |
|---|---|---|---|---|
| 1 | **主循环** | `execute()` sync for loop, `_state` 可变累积 | `query()` async generator, `state=next` 不可变 + `yield` 事件 | 🔴 背道而驰 |
| 2 | **Model 调用** | `backend.invoke()` 同步, `anthropic.Anthropic` 同步 client | `callModel()` async generator, `AsyncAnthropic` | 🔴 背道而驰 |
| 3 | **工具执行** | `_process_tool_calls()` 同步, 依赖 to_thread | `runTools()` async, 读并行/写串行 | 🔴 背道而驰 |
| 4 | **跨轮编排** | 每轮 HTTP 重建 context（stateless） | QueryEngine.mutableMessages 跨轮保留 | 🔴 背道而驰（Phase 11 已定 SessionAgent 修） |
| 5 | **事件流** | outcome 一次性返回, 无 yield | generator yield 事件给消费方 | 🔴 背道而驰 |

### 关键认知

**这 5 个不是孤立 bug——是同一个根因**：我们走了"同步单线程 + 每轮重建"的路线，CC 走了"async generator + 跨轮状态 + yield 事件流"的路线。async 化不是加个 async 关键字，而是**把整条链路从"同步返回"改成"async generator 流"**。

---

## 二、逐项替换方案（每个含 CC 对照 + 完整代码 + 验证）

### 替换 1：Model 层 — 同步 client → AsyncAnthropic

**CC 对照**：`callModel()` 返回 AsyncGenerator，`for await` 消费。

**现状**（llm/anthropic_backend.py:43）：
```python
self._client = _anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)  # 同步
```

**替换**（native_backend.py + anthropic_backend.py）：
```python
# anthropic_backend.py — 新增 async client
self._async_client = _anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)

# native_backend.py — 新增 ainvoke + astream_iter
class NativeBackend:
    async def ainvoke(self, conversation, *, tool_choice=None, model="", cancellation=None) -> ModelAction:
        """CC callModel() 等价 — async client, 不阻塞。"""
        api_messages = [to_api_dict(m) for m in conversation.non_system_messages]
        kwargs = {"model": model or self._model, "max_tokens": self._max_tokens,
                  "messages": api_messages}
        if system_content: kwargs["system"] = system_content
        if self._cached_api_tools: kwargs["tools"] = self._cached_api_tools
        # 异步调用
        response = await self._async_client.messages.create(**kwargs)
        return _parse_sdk_response(response).to_model_action()

    async def astream_iter(self, conversation, *, tool_choice=None, model=""):
        """CC callModel() async generator — 流式 + await。"""
        ...
        async with self._async_client.messages.stream(**kwargs) as stream:
            async for text_chunk in stream.text_stream:
                if text_chunk:
                    yield StreamEvent(kind=TEXT_DELTA, text=text_chunk)
            final = await stream.get_final_message()
            ...
```

**验证**（tests/runtime_core/test_native_async_backend.py）：
```python
async def test_ainvoke_returns_model_action():
    backend = NativeBackend(...)  # mock AsyncAnthropic
    action = await backend.ainvoke(conv)
    assert isinstance(action, ModelAction)

async def test_astream_iter_yields_deltas():
    deltas = []
    async for ev in backend.astream_iter(conv):
        if ev.kind == TEXT_DELTA: deltas.append(ev.text)
    assert deltas
```

**影响**：
- `llm/anthropic_backend.py`: +async client（同步 client 保留给 legacy）
- `llm/openai_backend.py`: 同样 +async（OpenAI AsyncOpenAI）
- `runtime_core/native_backend.py`: +ainvoke + astream_iter
- `runtime_core/openai_native_backend.py`: +async

---

### 替换 2：主循环 — sync execute → async generator aiterate

**CC 对照**：`query()` while(true) + yield 事件 + state=next 不可变。

**现状**（native_step_loop.py:138）：sync for loop + `_state` 可变。

**替换**（native_step_loop.py）：
```python
async def aiterate(self, context, *, text_callback=None):
    """CC query() 等价 — async generator, yield 事件。"""
    # CC: State 初始化（不可变, 从 DB/快照）
    state = self._init_state(context)
    
    for turn in range(context.max_steps):
        if context.cancellation.cancelled:
            yield {"type": "cancelled", "reason": "user cancelled"}
            return
        
        # CC 5步压缩 → ContextBudgetManager
        conv = state.to_conversation()
        pruned_conv, budget = self._budget.ensure_budget(conv)
        
        # CC callModel async generator
        tool_uses = []
        assistant_text = ""
        async for event in self._backend.astream_iter(pruned_conv, tool_choice="auto"):
            if event.kind == StreamEventKind.TEXT_DELTA:
                yield {"type": "text_delta", "text": event.text}
                if text_callback: text_callback(event.text)
            elif event.kind == StreamEventKind.TOOL_USE:
                tool_uses.append(event.tool_call)
            elif event.kind == StreamEventKind.FINISH:
                break
        
        # CC: needsFollowUp?
        if tool_uses:
            results = await self._atool_calls(tool_uses, context)
            for tr in results:
                yield {"type": "tool_result", "tool_call": tr.tool_call, "outcome": tr.outcome}
                state = state.with_tool_result(tr)   # CC state=next 不可变
            continue  # CC next_turn
        
        # CC: 无 tool_use → 完成
        outcome = RuntimeOutcome.completed(...)
        yield {"type": "completed", "outcome": outcome}
        return
```

**关键**：`state = state.with_tool_result(tr)`——对齐 CC 的 `state = next` 不可变。但我们的 `ConversationState` 是可变的。**需要新增不可变快照语义**（或用 `state.copy()` 返回新对象）。

**验证**：
```python
async def test_aiterate_yields_events():
    events = []
    async for ev in loop.aiterate(ctx):
        events.append(ev["type"])
    assert "completed" in events  # 至少到完成

async def test_aiterate_tool_loop():
    # model 返回 tool_use → 工具 → 再 model → 文本
    events = [ev["type"] for ev in ...]
    assert "tool_result" in events
    assert events[-1] == "completed"
```

---

### 替换 3：工具执行 — sync → async runTools 等价

**CC 对照**：`runTools()` + `partitionToolCalls()`（读并行 cap10, 写串行）。

**现状**：`_process_tool_calls()` 同步 + `_execute_parallel_batch` 靠 to_thread。

**替换**（native_step_loop.py + _RealTools）：
```python
async def _atool_calls(self, tool_uses, context):
    """CC runTools() 等价 — 分区并行。"""
    safe, serial = [], []
    for tc in tool_uses:
        meta = self._scheduler._registry.get(tc.name)
        (safe if meta and meta.read_only and meta.concurrency_safe else serial).append(tc)

    results = []
    # 并行读（CC cap 10）
    if safe:
        chunks = [safe[i:i+10] for i in range(0, len(safe), 10)]
        for chunk in chunks:
            results += await asyncio.gather(*[self._atool_one(tc, context) for tc in chunk])
    # 串行写
    for tc in serial:
        results.append(await self._atool_one(tc, context))
    return results

async def _atool_one(self, tc, context):
    """CC runToolUse() — hook → permission → await tool → post. """
    gate = self._ports.hooks.check("PreToolUse", PreToolUseInput(...), tc.name)
    if not gate.allowed:
        return ToolResult(tool_call=tc, hook_allowed=False, ...)
    params = gate.updated_input or tc.params
    outcome = await self._ports.tools.aexecute(tc.name, params, tc.id)  # await 工具
    post = self._ports.hooks.check("PostToolUse", ...)
    return ToolResult(tool_call=tc, outcome=outcome, ...)
```

**`_RealTools` 加 async**（composition/runtime_composition.py）：
```python
class _RealTools:
    async def aexecute(self, tool_name, params, invocation_id=""):
        """CC tool.call() 等价 — await 工具。"""
        if tool_name in self._dynamic:
            return await self._execute_dynamic_async(tool_name, params, invocation_id)
        return await _execute_via_registry_async(...)
```

**验证**：
```python
async def test_tool_parallel_read():
    # 3 个 read-only 工具 → 并行
    results = await loop._atool_calls([Read, Grep, Glob], ctx)
    assert len(results) == 3

async def test_tool_serial_write():
    # 2 个 write 工具 → 串行
    results = await loop._atool_calls([Write, Edit], ctx)
    assert len(results) == 2
```

---

### 替换 4：底层 subprocess — sync → async

**CC 对照**：Bash 底层 async subprocess。

**现状**：`core/process.py` `subprocess.Popen` + `communicate()` 同步。

**替换**（core/process.py）：
```python
class Runtime(ABC):
    async def aexec(self, command, args=None, cwd=None, timeout=30, env=None, cancel_token=None) -> RunResult:
        parts = [command] + (args or [])
        proc = await asyncio.create_subprocess_exec(
            *parts, stdout=PIPE, stderr=PIPE, cwd=cwd, env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            return RunResult(returncode=proc.returncode, stdout=out, stderr=err)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            return RunResult(returncode=-1, stderr=f"Command timed out after {timeout}s")
```

---

### 替换 5：SessionAgent — 消费 aiterate generator

见 v3 层 6。SessionAgent 消费 `async for event in loop.aiterate(ctx)`。

---

## 三、背道而驰的存量实现——删除/替换清单

| 存量实现 | 与 CC 背道而驰 | 处置 |
|---|---|---|
| `_run_async_from_sync` | 每轮 asyncio.run 临时 loop | 删除（aiterate 取代） |
| `_execute_parallel_batch` to_thread | sync 工具靠线程 | 删除（_atool_calls 取代） |
| `backend.invoke()` 同步调用 | model 阻塞 | 替换为 ainvoke |
| `anthropic.Anthropic` 同步 client | 阻塞 | 加 AsyncAnthropic |
| `_start_cancel_watcher` 线程 | 线程 watcher | 删除（task 取消取代） |
| `execute()` sync for loop | 无事件流 | 保留兼容, 新增 aiterate |

---

## 四、执行步骤（每步替换一个背道而驰, 做完反思）

### 阶段 A：替换 1 — Model async

| Step | 内容 | 文件 | 验证 |
|---|---|---|---|
| A1 | `anthropic_backend` + AsyncAnthropic | llm/anthropic_backend.py | client 创建 |
| A2 | `openai_backend` + AsyncOpenAI | llm/openai_backend.py | client 创建 |
| A3 | `NativeBackend.ainvoke` + `astream_iter` | native_backend.py | async 测试 |
| A4 | `OpenAINativeBackend` async | openai_native_backend.py | async 测试 |

### 阶段 B：替换 4 — 底层 subprocess

| Step | 内容 | 文件 | 验证 |
|---|---|---|---|
| B1 | `Runtime.aexec()` + Local/Docker | core/process.py | not_blocking 测试 |

### 阶段 C：替换 2 — 工具 async

| Step | 内容 | 文件 | 验证 |
|---|---|---|---|
| C1 | `BaseTool.aexecute()` abstract | core/base.py | 接口测试 |
| C2 | `_RealTools.aexecute()` | composition | 工具调用测试 |
| C3 | BashTool → aexec | tools/shell_tool.py | 不阻塞测试 |
| C4 | file I/O async | tools/file_tool.py | 并行读测试 |
| C5 | MCP 去外壳 | agent/mcp/ | MCP 调用测试 |

### 阶段 D：替换 3 — 执行链 async

| Step | 内容 | 文件 | 验证 |
|---|---|---|---|
| D1 | `_atool_calls()` async 分区 | native_step_loop.py | 并行/串行测试 |
| D2 | `_atool_one()` async（hook→perm→await） | native_step_loop.py | 全链测试 |

### 阶段 E：替换 2 — 主循环 async generator

| Step | 内容 | 文件 | 验证 |
|---|---|---|---|
| E1 | `NativeStepLoop.aiterate()` async generator | native_step_loop.py | yield 事件测试 |
| E2 | state 不可变快照（对齐 state=next） | conversation_state.py | 不可变测试 |
| E3 | SessionAgent 消费 generator | session_agent.py | 多轮测试 |

### 阶段 F：清理背道而驰存量

| Step | 内容 | 文件 |
|---|---|---|
| F1 | 删 `_run_async_from_sync` | native_step_loop.py |
| F2 | 删 `_execute_parallel_batch` to_thread | native_step_loop.py |
| F3 | 删 `_start_cancel_watcher` | core/process.py |
| F4 | 全量回归 | 全部 |

---

## 五、反思节点

每阶段完成后：
1. **对照 CC**：本阶段替换的，CC 源码怎么写的
2. **检查背道而驰残留**：是否还有 sync 阻塞 / to_thread / asyncio.run 残留
3. **验证不阻塞**：async 测试 + 全量回归
4. **确认主线推进**：SessionAgent async generator 主轴是否前进了一步

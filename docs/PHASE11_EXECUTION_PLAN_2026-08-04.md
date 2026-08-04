# Phase 11 长生命周期 Session — 具体执行计划

> 日期：2026-08-04
> 架构依据：CC 两层模型（QueryEngine 持有跨轮历史 + query() 单轮循环）
> 原则：不迁就现有代码，借鉴 CC 的架构

---

## 一、CC 两层架构（执行的唯一依据）

```
┌─ QueryEngine (session 层, 跨轮) ──────────────┐
│  mutableMessages: Message[]   ← 完整历史, 跨轮保留  │
│  totalUsage, permissionDenials                │
│  submitMessage() = 一轮的入口                    │
└──────────────────────────────────────────────┘
        │ yield* query({messages: mutableMessages})
        ▼
┌─ query() (单轮 agentic loop) ─────────────────┐
│  while(true):                                 │
│    think → act → observe                       │
│    State.messages 每次迭代重建 (不可变)           │
│    有 tool_use → continue                      │
│    无 tool_use → return (terminal)             │
└──────────────────────────────────────────────┘
```

**决定性结论**：
1. `query()` 内部**从不等待用户输入**——无 tool_use 就 return
2. 等待输入在**外层**（REPL/SDK 持有 loop 后等下一个 submitMessage）
3. `mutableMessages` 是跨轮历史所有者，每轮从中播种
4. **我们的 `NativeStepLoop.execute()` = CC 的 `query()`**——已经是"单轮跑完返回"语义，**完全正确，不动**

---

## 二、目标架构映射

| CC | Grace Code SessionAgent |
|---|---|
| `QueryEngine.mutableMessages` | `_mutable_messages: tuple[dict, ...]` |
| `QueryEngine.submitMessage()` | `submit_message()` |
| `query()` 单轮 agentic loop | `NativeStepLoop.execute()` 单轮 |
| 每轮从 mutableMessages 播种 | 每轮 `ConversationSnapshot(self._mutable_messages)` |
| 等待输入在外层 | `_pending` queue + `_session_loop()` |
| 跨轮 token 累计 | `_total_usage` |
| 每轮新增消息并入 | `_mutable_messages += outcome.messages` |

---

## 三、文件范围

| 文件 | 动作 | 新增行 |
|---|---|---|
| `server/services/session_agent.py` | **NEW** — PendingInput + SessionAgent + SessionAgentRegistry | ~150 |
| `composition/application_components.py` | +`session_registry` 字段 | +2 |
| `composition/runtime_composition.py` | 构造 SessionAgentRegistry + 传入 | +5 |
| `server/services/chat_pipeline.py` | `_execute_native()` 首次创建/后续注入 | +30 |
| `server/services/agent_service.py` | ChatPipelinePorts 传 session_registry | +1 |
| `tests/integration/test_session_agent.py` | **NEW** — 多轮、跨轮历史、TTL、恢复 | ~80 |

---

## 四、实现步骤（每步含代码）

### Step A1：`PendingInput` — 跨轮输入队列

```python
# server/services/session_agent.py

import asyncio
import uuid as _uuid
import threading
from dataclasses import dataclass, field

from runtime_core.native_step_loop import NativeStepLoop
from runtime_core.execution import ConversationSnapshot, RuntimeExecution
from core.eventing.identifiers import SessionId, RunId


class PendingInput:
    """CC REPL 等输入的等价物 — 线程安全队列 + 关闭信号。"""
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False

    async def post(self, text: str) -> None:
        """注入一条用户消息（HTTP handler 调）。"""
        await self._queue.put(text)

    async def wait(self, timeout: float) -> str | None:
        """等下一个输入；超时或关闭返回 None。"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._closed = True
```

### Step A2：`SessionAgent` — CC QueryEngine 等价物

```python
# server/services/session_agent.py

class SessionAgent:
    """CC QueryEngine — 持有跨轮历史，每轮播种单轮执行。

    关键：NativeStepLoop.execute() = CC query()（单轮，不动）。
    SessionAgent 负责跨轮 mutableMessages + 等待输入 + token 累计。
    """
    TTL_SECONDS = 30 * 60  # CC 会话无输入超时

    def __init__(self, session_id: str, ports, max_steps: int = 25,
                 workspace: str = "") -> None:
        self.session_id = session_id
        self._ports = ports
        self._max_steps = max_steps
        self._workspace = workspace
        self._loop = NativeStepLoop(ports)
        self._mutable_messages: tuple[dict, ...] = ()
        self._total_usage = {"input": 0, "output": 0}
        self._pending = PendingInput()
        self._task: asyncio.Task | None = None
        self._latest_outcome = None

    # ── 跨轮循环（CC REPL 等价）───────────────
    async def start(self, initial_messages: tuple[dict, ...] = ()) -> None:
        self._mutable_messages = tuple(initial_messages)
        self._task = asyncio.create_task(self._session_loop())

    async def _session_loop(self) -> None:
        while True:
            text = await self._pending.wait(self.TTL_SECONDS)
            if text is None:
                break  # TTL 超时 → 会话结束
            await self.submit_message(text)

    async def post_message(self, text: str) -> None:
        """HTTP handler 注入用户输入。"""
        await self._pending.post(text)

    # ── 单轮执行（CC submitMessage 等价）────────
    async def submit_message(self, text: str) -> None:
        # 1. 追加 user 消息（CC: mutableMessages.push）
        self._mutable_messages += ({"role": "user", "content": text},)
        # 2. 用历史播种单轮（CC: yield* query({messages: mutableMessages}))
        ctx = RuntimeExecution(
            session_id=SessionId(self.session_id),
            run_id=RunId(str(_uuid.uuid4())),
            max_steps=self._max_steps,
            conversation=ConversationSnapshot(self._mutable_messages),
            workspace=self._workspace,
        )
        # 3. 单轮 execute() — P1-2: to_thread 不阻塞事件循环
        outcome = await asyncio.to_thread(self._loop.execute, ctx)
        self._latest_outcome = outcome
        # 4. 本轮新增消息并入历史（CC: mutableMessages += query 输出）
        self._mutable_messages += tuple(outcome.messages or ())
        # 5. 跨轮 token 累计
        self._total_usage["input"] += outcome.input_tokens
        self._total_usage["output"] += outcome.output_tokens

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        self._pending.close()
        if self._task and not self._task.done():
            self._task.cancel()
```

### Step A3：`SessionAgentRegistry` — session_id → agent

```python
# server/services/session_agent.py

class SessionAgentRegistry:
    """session_id → SessionAgent 映射（CC session manager 等价）。"""
    def __init__(self) -> None:
        self._agents: dict[str, SessionAgent] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionAgent | None:
        with self._lock:
            return self._agents.get(session_id)

    def add(self, agent: SessionAgent) -> None:
        with self._lock:
            self._agents[agent.session_id] = agent

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._agents.pop(session_id, None)

    def list(self) -> list[SessionAgent]:
        with self._lock:
            return list(self._agents.values())
```

### Step A4：`assemble()` 接线 SessionAgentRegistry

```python
# composition/runtime_composition.py

# 在返回 ApplicationComponents 前
from server.services.session_agent import SessionAgentRegistry
_session_registry = SessionAgentRegistry()

# ApplicationComponents 加字段
session_registry: SessionAgentRegistry  # Phase 11: long-lived session registry
```

### Step A5：`ChatPipeline._execute_native()` 首次创建/后续注入

```python
# server/services/chat_pipeline.py

def _execute_native(self, request, prepared) -> RunResult:
    registry = getattr(self._ports, 'session_registry', None)

    # Phase 11: 长生命周期 — 首次创建 SessionAgent, 后续只注入输入
    if registry is not None:
        agent = registry.get(request.session_id)
        if agent is None:
            # 首次: 构建完整初始消息（system_prompt + rules + catalog + user1）
            # 但本轮先保持 stateless 行为 —— Phase 11 逐步切
            ...
        else:
            # 后续: 只注入 user message，不重建固定前缀
            asyncio.run(agent.post_message(prompt))
            return RunResult(...)  # 或挂起等流式（Step C）

    # 现有 stateless 路径（Phase 11 未完成时保持）
    ...
```

**注意**：Step A5 有两种形态——(a) 全量切长生命周期（需要 Step C 流式），(b) 渐进（先建 SessionAgent，stateless 兜底）。**本轮 Step A 只做 A1-A4（SessionAgent 骨架 + registry），A5 接线放到 Step B，流式放 Step C。**

---

## 五、每步验证

### Step A1-A4 验证

```python
# tests/integration/test_session_agent.py

async def test_session_agent_single_turn():
    """SessionAgent 提交一轮 → outcome 产生。"""
    agent = SessionAgent("s1", fake_ports)
    await agent.start()
    await agent.submit_message("hello")
    assert agent._latest_outcome is not None
    assert agent._mutable_messages  # 含 user + assistant

async def test_session_agent_preserves_history_across_turns():
    """两轮对话 → mutable_messages 含两轮完整历史（CC mutableMessages）。"""
    agent = SessionAgent("s1", fake_ports)
    await agent.start()
    await agent.submit_message("first")
    first_len = len(agent._mutable_messages)
    await agent.submit_message("second")
    assert len(agent._mutable_messages) > first_len  # 历史保留
    # 第二轮 ctx 播种含第一轮历史
    assert any("first" in str(m) for m in agent._mutable_messages)

async def test_session_agent_ttl_timeout():
    """TTL 内无输入 → 会话结束。"""
    agent = SessionAgent("s1", fake_ports, TTL_SECONDS=0.1)
    await agent.start()
    await asyncio.sleep(0.3)
    assert not agent.is_alive  # TTL 超时退出

async def test_registry_get_add_remove():
    """registry 增删查。"""
    reg = SessionAgentRegistry()
    agent = SessionAgent("s1", fake_ports)
    reg.add(agent)
    assert reg.get("s1") is agent
    reg.remove("s1")
    assert reg.get("s1") is None
```

### 每步通过的完整回归

```bash
python -m pytest tests/integration/test_session_agent.py tests/runtime_core/ tests/composition/ -q
```

---

## 六、Step B（后续）：`_execute_native()` 接线

依赖 Step A 完成。核心：

```python
# 首次
agent = SessionAgent(session_id, ports, workspace=repo_path)
registry.add(agent)
asyncio.create_task(agent.start())   # 或等首次输入再 start

# 后续
await agent.post_message(prompt)
# 返回 pending（Step C 用 asyncio.Queue 等 outcome 流式推 WebSocket）
```

## Step C（后续）：流式 + 恢复

- `_mutable_messages` 每轮并入后写入 `ConversationStore`（灾备）
- 进程重启 → `registry.rebuild_from_store(session_id)` → 从 SQLite 重建
- WebSocket 推流：`submit_message()` 内部加 `text_callback`（Phase 10 已有）→ 推事件

---

## 七、关键设计确认

1. **`NativeStepLoop.execute()` 完全不动** — 它是 CC query() 的正确对应，保持单轮语义
2. **SessionAgent 持有 `_mutable_messages`** — CC mutableMessages，跨轮历史所有者
3. **每轮从 `_mutable_messages` 播种 `ConversationSnapshot`** — CC 每轮从 mutableMessages 播种
4. **等待输入在 SessionAgent 层** — CC REPL/SDK 层，不在 StepLoop 内部
5. **`asyncio.to_thread` 包 execute()** — P1-2 调研结论，不阻塞事件循环

---

## 八、审计修正（对照代码核实）

### 已核实为真（非杜撰）

| 假设 | 证据 |
|---|---|
| execute() 可被 to_thread 包，跑在 worker 线程 | `native_step_loop.py:60-65` P1-1 注释明确预见了 Phase 11 SessionAgent 设计 |
| 并行工具执行内部用 to_thread + TaskGroup | `native_step_loop.py:531-534` |
| outcome.messages 是跨轮历史载体 | `outcome.py:71-74` Phase 5 |
| asyncio.run() 在 worker 线程安全 | `_run_async_from_sync` 注释（无 loop 线程） |
| max_steps 每轮独立预算 | CC maxTurns 单次 submitMessage 预算（P1-2 审查已确认） |
| workspace 显式传（进程内多 session 无进程 cwd） | `RuntimeExecution.workspace` 已加 |

### 审计发现的问题 + 修正

**问题 1（Step A 修正）**：`submit_message()` 每轮新建 run_id + RuntimeExecution，`_mutable_messages` 手动维护。CC 每轮 `recordTranscript` 落盘。但 `ConversationStore` 只有 `append_message(NativeMessage)`（逐条），没有整体 save。**修正：Step A 的 `SessionAgent.__init__` 接受 `store=None` 预留，落盘逻辑推迟到 Step C（与恢复机制一起做），避免在 Step A 硬塞可能错误的落盘。**

**问题 2（确认安全）**：每轮 `asyncio.to_thread` 里 `_execute_parallel_batch` 调 `_run_async_from_sync` → `asyncio.run()` 新建临时 loop。多轮重复建 loop 有资源开销，但功能正确。**对齐度可接受，不是架构错误。**

**问题 3（确认安全）**：`max_steps` 每轮独立——与 P1-2 审查结论一致（CC maxTurns 是单次预算）。

---

## 九、执行顺序（审计后）

```
Step A1: PendingInput 队列
Step A2: SessionAgent（__init__ 接受 store=None 预留落盘）
Step A3: SessionAgentRegistry
Step A4: assemble() 接线 registry
Step B:   _execute_native() 首次创建/后续注入
Step C:   流式 + 恢复（含落盘 + ConversationStore 对接）
```

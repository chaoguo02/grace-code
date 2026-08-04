"""SessionAgent — CC QueryEngine 等价, 消费 aiterate async generator.

Phase E: 长生命周期 session。SessionAgent 持有跨轮 mutableMessages,
每轮从 aiterate generator 消费事件, 通过 aexecute 执行。

CC 对照:
  QueryEngine.mutableMessages  →  _mutable_messages
  QueryEngine.submitMessage()  →  submit_message()
  query() async generator      →  NativeStepLoop.aiterate()
  等待输入在外层 REPL/SDK      →  _session_loop() 等 _pending queue
"""

from __future__ import annotations

import asyncio
import threading
import uuid as _uuid

from runtime_core.native_step_loop import NativeStepLoop
from runtime_core.execution import ConversationSnapshot, RuntimeExecution
from core.eventing.identifiers import SessionId, RunId


class PendingInput:
    """CC REPL 等输入的等价物 — asyncio queue + close 信号."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False

    async def post(self, text: str) -> None:
        """注入一条用户消息 (HTTP handler 调)."""
        await self._queue.put(text)

    async def wait(self, timeout: float) -> str | None:
        """等下一个输入; 超时或关闭返回 None."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._closed = True


class SessionAgent:
    """CC QueryEngine — 持有跨轮历史, 每轮消费 aiterate async generator.

    Key: NativeStepLoop.aiterate() = CC query() (async generator).
    SessionAgent 负责跨轮 mutableMessages + 等待输入 + token 累计.
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

    # ── 跨轮循环 (CC REPL 等价) ───────────────────────────────────

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
        """HTTP handler 注入用户输入."""
        await self._pending.post(text)

    # ── 单轮执行 (CC submitMessage 等价) ───────────────────────────

    async def submit_message(self, text: str) -> None:
        """CC submitMessage() — 一轮完整执行, 消费 aiterate generator."""
        # 1. 追加 user 消息 (CC: mutableMessages.push)
        self._mutable_messages += ({"role": "user", "content": text},)
        # 2. 用历史播种单轮 (CC: yield* query({messages: mutableMessages}))
        ctx = RuntimeExecution(
            session_id=SessionId(self.session_id),
            run_id=RunId(str(_uuid.uuid4())),
            max_steps=self._max_steps,
            conversation=ConversationSnapshot(self._mutable_messages),
            workspace=self._workspace,
        )
        # 3. 消费 aiterate async generator (CC: for await query events)
        final_outcome = None
        async for event in self._loop.aiterate(ctx):
            if event["type"] in ("completed", "failed", "cancelled", "blocked"):
                final_outcome = event["outcome"]
        self._latest_outcome = final_outcome
        # 4. 本轮新增消息并入历史 (CC: mutableMessages += query 输出)
        if final_outcome is not None:
            self._mutable_messages += tuple(final_outcome.messages or ())
            self._total_usage["input"] += final_outcome.input_tokens
            self._total_usage["output"] += final_outcome.output_tokens

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        self._pending.close()
        if self._task and not self._task.done():
            self._task.cancel()


class SessionAgentRegistry:
    """session_id → SessionAgent 映射 (CC session manager 等价)."""

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

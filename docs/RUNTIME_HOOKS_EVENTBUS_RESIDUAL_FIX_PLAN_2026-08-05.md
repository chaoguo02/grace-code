# 残余项消除执行计划

> 日期：2026-08-05
> 基线：Phase A-C 完成
> 原则：先写测试→确认失败→实现→确认通过；测试测行为，不测实现细节

---

## R1 — LiveEvent Scope Wiring（P0 残余）

### 1.1 症状

`_RealLiveEvents.publish()` 是 `pass`。`LiveEventPort` 协议没有 `scope` 参数，EventBus 无法路由。

### 1.2 正确行为的测试（先写，当前全部 FAIL）

```python
# Test 1: EventBus 收到事件
def test_live_event_reaches_bus():
    bus = ScopedEventBus()
    sid = SessionId("s-test")
    bus.ensure_session(sid, 0)
    scope = bus._tree.ensure_session(sid, 0).token
    received = []
    bus.subscribe("tool.executed.v1", lambda m: received.append(m),
                  "sub", scope=scope)

    # 构造一个带 scope 的 publish（模拟修复后的行为）
    msg = _make_scoped_message("tool.executed.v1", scope, {"tool": "read"})
    bus.publish(msg)

    assert len(received) == 1  # ← 当前 FAIL: publish() 是 pass

# Test 2: scope isolation
def test_live_event_scope_isolation():
    bus = ScopedEventBus()
    s_a = bus._tree.ensure_session(SessionId("s-a"), 0).token
    s_b = bus._tree.ensure_session(SessionId("s-b"), 0).token
    recv_a, recv_b = [], []
    bus.subscribe("tool.executed.v1", lambda m: recv_a.append(m), "a", scope=s_a)
    bus.subscribe("tool.executed.v1", lambda m: recv_b.append(m), "b", scope=s_b)

    msg = _make_scoped_message("tool.executed.v1", s_a, {"tool": "read"})
    bus.publish(msg)

    assert len(recv_a) == 1
    assert len(recv_b) == 0  # ← 当前 FAIL: 都没收到

# Test 3: LiveEventPort 接受 scope
def test_live_event_port_accepts_scope():
    """LiveEventPort.publish 现在接受 scope 参数"""
    import inspect
    sig = inspect.signature(LiveEventPort.publish)
    assert "scope" in sig.parameters  # ← 当前 FAIL: scope 不在签名中
```

### 1.3 修复

**三步，一个文件（`composition/runtime_composition.py`）：**

1. `LiveEventPort.publish` 签名增加 `scope: ScopeToken | None = None`
2. `_RealLiveEvents.publish` 实现：有 scope 时调用 `self._bus.publish()`，无 scope 时 fallback log
3. StepLoop 的 `_process_tool_calls` 中构造 scope 并传入

```python
# 修复 1 — ports.py
class LiveEventPort(Protocol):
    def publish(self, event_type: str, payload: FrozenJsonObject,
                scope: ScopeToken | None = None) -> None: ...

# 修复 2 — _RealLiveEvents
def publish(self, event_type, payload, scope=None):
    if scope is not None:
        from eventing.publisher import ScopedMessage
        msg = _LiveMessage(event_type=event_type, scope=scope, payload=payload)
        self._bus.publish(msg)

# 修复 3 — step_loop._process_tool_calls
scope = context.scope if hasattr(context, 'scope') else None
self._ports.live_events.publish("tool.executed.v1", payload, scope=scope)
```

---

## R2 — 删除 CancellationPort（P1 残余）

### 2.1 症状

`_RealCancellation.cancelled` 永远返回 `False`。StepLoop 的 5 处取消检查全部直接读 `context.cancellation.cancelled`，从未通过 port。

### 2.2 正确行为的测试

```python
# Test 1: step_loop 不引用 CancellationPort
def test_step_loop_never_uses_cancellation_port():
    with open("runtime_core/step_loop.py") as f:
        source = f.read()
    assert "self._ports.cancellation" not in source  # ← 当前 PASS（已经不用）

# Test 2: RuntimePorts 无 cancellation 字段仍编译
def test_runtime_ports_without_cancellation():
    ports = RuntimePorts(
        llm=..., tools=..., hooks=..., live_events=...,
        clock=..., token_usage=...,
        # ← 没有 cancellation
    )
    assert ports is not None  # ← 当前 FAIL: RuntimePorts 要求 7 个字段
```

### 2.3 修复

1. `runtime_core/ports.py`：删除 `CancellationPort` 协议和 `RuntimePorts.cancellation` 字段
2. `composition/runtime_composition.py`：删除 `_RealCancellation` 类和装配代码
3. 更新所有创建 `RuntimePorts(...)` 的调用方（`composition/runtime_composition.py` 的 `assemble()` 和 `run_submission.py`）

---

## R3 — ChatPipeline Native 路径（P1 残余）

### 3.1 症状

ChatPipeline 内部 `execute()` 方法调用 `self._runtime.run_session()`——`SessionRuntime`。

### 3.2 正确行为的测试

```python
# Test 1: ChatPipeline native 路径产生 evidence
def test_chat_pipeline_native_produces_evidence(tmp_path):
    """当 coordinator 可用时，ChatPipeline 通过 Native Runtime 执行，
    返回的 outcome.evidence 非 None"""
    ...

# Test 2: Native 路径写入 Outbox
def test_chat_pipeline_native_writes_to_outbox(tmp_path):
    """Native 路径执行后 event_outbox 有 terminal 事件"""
    ...
```

### 3.3 修复

ChatPipeline 构造增加 `coordinator` 参数（可选）。`execute()` 中优先使用 coordinator：

```python
if self._coordinator is not None:
    outcome = self._coordinator.execute(...)
    self._coordinator.finalize(...)
else:
    result = self._runtime.run_session(...)
```

---

## 阶段表

| 阶段 | 文件 | 消灭的残余 |
|---|---|---|
| R1 | `runtime_core/ports.py`, `composition/runtime_composition.py`, `tests/eventing/test_async_bus_lifecycle.py` | LiveEvent scope |
| R2 | `runtime_core/ports.py`, `composition/runtime_composition.py`, `tests/runtime_core/test_model_action_ports.py` | CancellationPort |
| R3 | `server/services/chat_pipeline.py`, `tests/integration/test_native_run_e2e.py` | ChatPipeline Native |

---

> **执行顺序：R1 → R2 → R3。每个阶段先写测试→确认 FAIL→实现→确认 PASS。**

# 后端可观测性 + 维护任务 + 变更追踪 — 治本设计

> 基于 OpenTelemetry 行业标准 + CC 官方 SDK 模式

---

## 根因 1: Stats/Metrics — 第一方 instrumentation

### 现状问题

`StatsRecorder` 是 `EventBus.publish()` 的被动观察者。它从翻译后的 WS 消息中提取数据，丢失了结构化信息（token 数、实际状态）。agent_name 需要外部注入。`record_step` 被调用两次（action + observation），产生重复记录。

### 行业标准

OpenTelemetry 的 **第一方 instrumentation** 模式：ReActAgent loop 自身在每个步骤创建 span，记录 metrics。不是挂在 EventBus 的副作用链上。

```
invoke_agent (root span)
├── cycle 1
│   ├── chat / llm_generate (token_count, model, latency)
│   └── execute_tool (tool_name, params, success, duration)
├── cycle 2
│   └── ...
```

**来源:** [OpenTelemetry Agent Observability 2025](https://sparkco.ai/blog/agent-observability-with-opentelemetry-2025-insights), [strands-agents SDK telemetry](https://deepwiki.com/strands-agents/harness-sdk/9.2-python-telemetry-and-metrics)

### 治本设计

**不在 EventBus 上挂 recorder。** 在 ReActAgent 的 tool 执行点直接记录：

```python
# agent/core.py — in the tool execution block:
_start = time.perf_counter()
result = tool.execute(params)
_elapsed = (time.perf_counter() - _start) * 1000

# Record directly, not via EventBus side effect
if self._cfg.stats_collector:
    self._cfg.stats_collector.record_tool_call(
        session_id=self._session_id,
        agent_name=self._agent_name,  # ← already known here
        step=step,
        tool_name=tool_name,
        params=params,
        success=result.success,
        duration_ms=_elapsed,
    )
```

**AgentConfig 新增 `stats_collector` 字段**。在 `run_session` 中注入。LLM token 数从 backend response 中提取（已经可用，在 `_run_body` 的 `total_tokens` 变量中）。

**关键改变:**
- recorder 不再是 EventBus 的被动观察者
- agent_name 不需要外部注入（ReActAgent 知道自己是谁）
- token 数可以直接从 LLM response 获取
- 每个 tool 调用只产生一条记录（不是 action+observation 两次）

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agent/core.py` | tool 执行点 + LLM 调用点直接 record |
| `agent/core.py` — `AgentConfig` | 新增 `stats_collector` 字段 |
| `server/services/stats_recorder.py` | 简化 API：`record_tool_call()`, `record_session_start()`, `record_session_end()` |
| `server/services/agent_service.py` | 注入 stats_collector 到 AgentConfig |
| `server/services/event_bus.py` | **移除** recorder 调用 |

---

## 根因 2: Memory 维护 — asyncio 调度

### 现状问题

`time.sleep(600)` 在 daemon 线程中阻塞。无优雅退出。10 分钟硬编码。

### 行业标准

Python 后台任务的标准模式：`asyncio.create_task()` + `asyncio.Event` 用于优雅关闭。或使用 `apscheduler` 的 `BackgroundScheduler`。

### 治本设计

使用 asyncio 的 `create_task` + `Event` 模式，因为 FastAPI 已经在 asyncio event loop 中运行：

```python
# server/main.py or agent_service.__init__
async def _memory_maintenance_loop(stop_event: asyncio.Event, store, interval: int = 600):
    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval)
            backend = getattr(store, '_backend', None)
            if backend and hasattr(backend, 'decay_confidences'):
                backend.decay_confidences()
        except Exception:
            pass

# Startup
_stop_event = asyncio.Event()
_maintenance_task = asyncio.create_task(
    _memory_maintenance_loop(_stop_event, memory_store, interval=600)
)

# Shutdown (in AgentService.shutdown())
_stop_event.set()
await _maintenance_task
```

**关键改变:**
- asyncio.sleep 不阻塞线程
- Event 实现优雅关闭
- interval 可配置

### 涉及文件

| 文件 | 改动 |
|------|------|
| `server/services/agent_service.py` | daemon 线程 → asyncio.create_task |
| `server/main.py` (或 app shutdown) | 优雅关闭 |

---

## 根因 3: Diff 追踪 — 工具主动报告 + git diff 兜底

### 现状问题

`_compute_diff` 用正则从人类可读输出文本 ("Created new file: /path/to/foo.py") 提取路径。`ToolResult.modified_files` 字段加了但 EventBus 没读它。

### CC 的做法

CC 的官方 SDK 有 `enable_file_checkpointing` 系统，追踪 Write/Edit/NotebookEdit 修改的文件。**Bash 命令的修改不被追踪** — 这是已知限制。

CC 的 checkpoint 系统是**工具层面主动报告** + **before/after snapshot** 模式，不是正则解析。

**来源:** [Claude Code file checkpointing](https://code.claude.com/docs/en/agent-sdk/file-checkpointing)

### 治本设计

**Step 1:** 在 observation 事件中携带 `modified_files`（从 ToolResult 传递到 Event → Observation → EventLog → EventBus）

**Step 2:** EventBus 优先使用 `modified_files`，没有时才用 `_compute_diff` 的正则作为兜底

**Step 3:** Write/Edit 工具填充 `modified_files`（Write 已做，Edit 待补）

```python
# EventBus.publish() — observation 处理:
_modified = payload.get("observation", {}).get("modified_files", [])
if _modified:
    for _fp in _modified:
        diff = git_diff_for_file(_fp)
        if diff:
            msg["diff"] = diff
else:
    # Fallback: regex (for Bash and tools that don't report modified_files)
    diff = self._compute_diff(tool_name, output)
```

### 涉及文件

| 文件 | 改动 |
|------|------|
| `core/base.py` | `Observation` 新增 `modified_files` 字段 |
| `tools/file_tool.py` | Write 已填充 ✅ |
| `tools/file_edit_tool.py` | Edit 填充 `modified_files` |
| `server/services/event_bus.py` | 优先读 metadata，正则兜底 |
| `agent/core.py` | observation 构造时传递 modified_files |

---

## 实施顺序

```
Batch 1: Stats — AgentConfig.stats_collector + core.py 直接 record
Batch 2: Stats — 简化 recorder API + 移除 EventBus 调用
Batch 3: Memory — asyncio task + 优雅关闭
Batch 4: Diff — Observation.modified_files + Edit 填充 + EventBus 优先读取
```

每批 ≤3 文件。每批 commit → 反思 → 继续。

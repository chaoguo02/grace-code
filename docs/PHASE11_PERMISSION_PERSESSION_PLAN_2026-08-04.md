# Per-Session PermissionPipeline — CC 对齐实现计划（v2）

> 日期：2026-08-04
> v2：websearch 调研 CC 并发模型后重写——去除"修复 scoped()"补丁式思路，改为对齐 CC 的隔离模型

---

## 一、调研结论——CC 的真实并发模型

### CC 没有"per-session 锁"，因为它不需要

```
CC: queryLoop() = async generator，整个 session 跑在一个事件循环单线程
    多 session 并行 = 进程级隔离（每 session 一个 claude -p 子进程 / worktree）
    单线程 → 无并发 → 无锁
```

CC 的并发隔离靠**进程边界**，不是锁。这是关键认知——我们的锁问题源于"多 session 共享全局 pipeline"，而 CC 压根不共享。

### CC 的 denial tracking 是模块级 Map

```
denialTracking.ts: maxConsecutive=3, maxTotal=20
  状态 = 模块级 Map（session 维度），不是 per-pipeline 实例
  per-session 通过 SDKPermissionDenial[] 暴露
```

CC 的 per-session 隔离是**数据维度**（Map keyed by session），不是**对象实例维度**（每 session 一个 pipeline 对象）。加上单线程，天然无竞争。

### CC 的 hook 触发一次

```
permissions.ts 7-step cascade:
  1. bypass-immune safety checks
  2. bypass mode check
  3. always-allow rules
  4. tool.checkPermissions()
  5. executePermissionRequestHooks()  ← PreToolUse hook, 一次
  6. ask-rule check
  7. default by permissionMode
```

CC 没有"外层 hook + 内层 hook"双重结构——hook 在 pipeline 内触发一次。

### CC 的 session 隔离机制

```
- 每个 session 一个进程（claude -p）/ 事件循环 / worktree → 天然隔离
- "session 规则" = PermissionUpdate destination='session'（不是独立 pipeline 实例）
- plan mode 切换 = applyPermissionUpdate({type:'setMode', destination:'session'})
```

---

## 二、我们的问题根源

```
我们: assemble() 造全局 PermissionPipeline（单例）
     _RealHooks._pipeline_for() 每 session scoped 浅拷贝（共享 _state_lock）
     → 多 session 并发 → 共享锁串行化 + counters 状态泄漏风险

根源: "共享全局单例 + 浅拷贝复制" 模式 —— CC 根本没有这个模式
      CC 是"每 session 独立对象/进程"
```

**给 scoped() 补独立锁 = 给错误架构打补丁。** 正确做法是去掉"全局单例 + scoped 复制"，改为每 session 独立构造。

---

## 三、目标架构——每 session 独立 PermissionPipeline

```
assemble() → 不造全局 PermissionPipeline
             → 只保存"规则来源"（settings 解析结果），不实例化

_RealHooks:
  _permission_pipelines: dict[session_id, PermissionPipeline]   ← 每 session 一个
  _session_lock: threading.Lock                                  ← 只保护这个 dict 的增删，不保护权限逻辑

  def _pipeline_for(session_id):
      if not session_id: return None
      with _session_lock:  # 仅 dict 访问
          if session_id in _permission_pipelines:
              return _permission_pipelines[session_id]
          # 每 session 全新构造（CC 进程隔离的进程内等价）
          pipeline = PermissionPipeline(
              rules=[深拷贝基座规则],
              approval_mode=AUTO,
              project_root=repo_root,
              web_confirm_callback=该 session 的 cb,
              # __init__ 天然新建 RLock + 空 counters
          )
          _permission_pipelines[session_id] = pipeline
          return pipeline
```

### 关键点

1. **每 session 一个 `PermissionPipeline` 对象**——`__init__` 天然新建 `_state_lock`(RLock) + `_denial_counters={}` + `_total_denials=0`。无 scoped，无 copy，无共享锁。
2. **`_session_lock` 只保护 dict 增删**——不参与权限逻辑，权限检查在 pipeline 内部自己的锁上。
3. **规则从基座深拷贝传入构造**——不是 copy.copy 共享。
4. **counters 从零开始**——CC session 语义（20 总/3 连续 per session）。

### CC 对齐

| CC | 我们（v2） |
|---|---|
| 每 session 一个进程 | 每 session 一个 PermissionPipeline 对象 |
| 单线程无锁 | 每 pipeline 自己的 RLock（进程内多线程需要） |
| denial Map keyed by session | 每 pipeline 自己的 counters（等价的 session 隔离） |
| PermissionUpdate destination=session | `_pipeline_for()` 按 session 独立构造 |
| hook 一次 | `_RealHooks.check()` 外层 hook 一次 + pipeline 内不再 attach dispatcher（保持一次） |

---

## 四、hook 双重触发的最终确认

### 现状

```
_RealHooks.check():
  1. hook_result = dispatcher.dispatch("PreToolUse", hook_input)   ← 外层 hook（cwd 待填）
  2. permission gate → pipeline.check(tool, params)
     → _layer2_hooks() 若 dispatcher attach 会再触发 hook   ← 内层 hook

当前: assemble() 的 _permission_pipeline 从未 attach hook_dispatcher
      → _layer2_hooks 直接返回空（if self._hook_dispatcher is None）
      → hook 只触发一次（外层）✅
```

### 风险

如果未来有人给 `_permission_pipeline.attach_hook_dispatcher(dispatcher)`，就会**双重触发**。

### 修正（v2）

保持 `_permission_pipeline` 不 attach dispatcher。hook 只在外层 `_RealHooks.check()` 触发一次。权限 pipeline 不承担 hook 职责——**各司其职**：
- `_RealHooks`（外层）= hook 检查（should it run?）
- `PermissionPipeline` = 权限规则 + mode + 交互（may it run?）

这符合 CC：hook 在 pipeline 内一次（第 5 步），我们的 hook 在 `_RealHooks` 外层一次，语义等价。

---

## 五、cwd 预留接口

### CC 的 cwd 来源

```
CC: hook input { cwd } = 当前进程的工作目录（repo root 或 worktree）
    每 session 一个进程 → cwd = 该进程启动目录
```

### 我们的进程内多 session

没有"进程 cwd"概念——需要显式传。

### 方案（v2）

```python
# runtime_core/execution.py
@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    ...
    workspace: str = ""  # Phase 12: hook cwd / tool workspace（repo root 或 worktree）
```

```python
# native_step_loop.py — 构造 hook input
hook_input = PreToolUseInput(
    tool_name=tc.name, tool_input=tc.params, tool_use_id=tc.id,
    session_id=str(context.session_id) if context.session_id else "",
    cwd=context.workspace or "",   # ← 预留
)
```

`_execute_native()` 构造 `RuntimeExecution` 时填 `workspace=repo_root`。

默认空字符串（当前行为不变），Phase 12 填 repo/worktree。

---

## 六、实现步骤

| Step | 内容 | 文件 | 行数 |
|---|---|---|---|
| 1 | `_RealHooks` 改为 dict[session_id, Pipeline] + 每 session 直接构造 | `composition/runtime_composition.py` | +20 |
| 2 | 基座规则提取（不实例化全局 pipeline，只留规则来源） | `composition/runtime_composition.py` | +10 |
| 3 | `RuntimeExecution.workspace` 字段 | `runtime_core/execution.py` | +1 |
| 4 | StepLoop hook input 填 cwd | `runtime_core/native_step_loop.py` | +2 |
| 5 | `_execute_native()` 填 workspace | `server/services/chat_pipeline.py` | +1 |
| 6 | 并发隔离测试 | `tests/runtime_core/test_permission_concurrency.py` | +40 |
| 7 | 回归 | — | — |

### 并发测试设计

```python
def test_two_sessions_independent_pipelines():
    """session-A 拒绝 3 次 → session-B 完全不受影响（独立 pipeline 对象）。"""
    comp = _assemble(...)
    hooks = comp.runtime_ports.hooks
    
    hooks.register_session_confirm("A", deny_cb)
    hooks.register_session_confirm("B", allow_cb)
    
    # session-A 连续拒绝
    for _ in range(3):
        r = hooks.check("PreToolUse", input_with(session_id="A"), tool_name="Write")
        assert not r.allowed
    
    # session-B 应放行（不同 pipeline，counter 独立）
    r = hooks.check("PreToolUse", input_with(session_id="B"), tool_name="Write")
    assert r.allowed

def test_pipelines_have_distinct_locks():
    """两个 session 的 pipeline 锁不同（不共享）。"""
    comp = _assemble(...)
    hooks = comp.runtime_ports.hooks
    pA = hooks._pipeline_for("A")
    pB = hooks._pipeline_for("B")
    assert pA is not pB
    assert pA._state_lock is not pB._state_lock
```

---

## 七、设计确认

1. **去掉 scoped() 补丁思路**——`_RealHooks` 直接每 session 构造，不复制
2. **保持 hook 一次触发**——`_permission_pipeline` 不 attach dispatcher
3. **cwd 用 `RuntimeExecution.workspace`**——默认空，Phase 12 填
4. **`scoped()` / `for_agent()` 不修**——那是 child agent 的职责，Phase 11 不做；它们共享锁的问题在 child agent 场景是"parent 和 child 共享规则快照"，语义与 CC 的 fork 不同，独立处理

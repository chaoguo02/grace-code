# 批判性反思：全部改进的自我审视

> 不美化，不回避。逐一审视今天所有改动的设计质量。

---

## 一、做得好的

### 1. 管线重构 (G4) — 真正的架构改进

`deny → ask → allow` 优先级重排从根本上解决了 ask 规则的定位问题。不再是"allow 之后的兜底"，而是 "Phase 1 bypass-immune"。`_force_interactive` 标记 + 继续管线的模式让 plan/dontAsk 能正确拦截 ask 规则。

**设计质量: 9/10。** 唯一扣分：`_force_interactive` 和 `_decision_reason` 是实例属性而非局部变量，语义上它们是"本次 check() 调用的临时状态"。

### 2. ApprovalBroker — 简洁可靠

`threading.Event` + `publish_raw` + HTTP resolve 的三角模型比 CC 的 stdin 阻塞更灵活。代码量小（~170 行），线程安全正确。

**设计质量: 9/10。**

### 3. 子代理 WS 路由 (P3) — 根因修复

不重写 session_id，而是透传 `child_session_id` 让前端区分渲染。EventBus 翻译层统一处理，前端 WsEventBlock 统一缩进。语义清晰。

**设计质量: 8/10。** 扣分：`child_session_id` 是动态属性挂在 Event 上，不是显式字段。

### 4. ExitPlanMode contract — 从正则到结构化

删掉 `_extract_plan_contract()` 的正则，改用 tool function calling 的 JSON schema。contract 从 tool params → metadata → RunResult.contract → plan_ready 全链路结构化。

**设计质量: 9/10。**

---

## 二、做得一般，可以接受

### 5. MCP 模块 (G6) — 骨架完整但未测试

Protocol + Transport + Registry + Tool Wrapper 四层架构正确。但：
- `HttpTransport` 的 send/receive 分离是抽象层的必要代价，底层仍是一次 POST
- 后台连接线程 fire-and-forget，没有重试
- 没有经过真实 MCP 服务器的端到端测试

**设计质量: 7/10。**

### 6. 完成守卫改进 — 功能正确但 O(n×m)

basename set intersection 比 endswith 好，但 `list_child_sessions` + `list_child_sessions` 的递归仍然是 O(n²)。对于深层嵌套 session 树会有性能问题。

**设计质量: 7/10。**

### 7. SessionTree — UI 正确但数据过重

`GET /tree` 返回完整递归树，包含所有后代的所有字段。如果 session 树很深很大，响应体积可能很大。应该支持 `?depth=N` 限制。

**设计质量: 7/10。**

---

## 三、做得不好，有技术债务

### 8. `is_web_mode` 标记 — 全局标志语义弱

```python
self._runtime._is_web_mode = True
```

`SessionRuntime` 不应该知道自己运行在 Web 还是 CLI 模式。这是环境信息，应该通过依赖注入（回调工厂）传递，而不是一个布尔标志。

**正确做法：** `SessionRuntime` 接收一个 `WebCallbackFactory` 或 `is_interactive: bool`，子代理通过它判断是否需要 web callback。

**设计质量: 5/10。**

### 9. PlanRevisionService — 文件存储是权宜之计

JSON 文件存储简单但：
- 并发写入不安全（两个请求同时写会覆盖）
- 没有事务
- 大文件性能退化
- 没有迁移路径

**正确做法：** SQLite 表 + 事务。但为了快速交付用了 JSON 文件。

**设计质量: 5/10。**

### 10. Worktree resolve — 同步阻塞 API 线程

```python
result = service._runtime.resolve_worktree(session_id, child_id, action)
```

`apply_worktree()` 操作文件系统（git merge），可能耗时数秒。API 线程同步等待会导致 HTTP 请求超时。

**正确做法：** 命令入队列 → 后台线程处理 → WS 推送结果。

**设计质量: 4/10。**

### 11. 后台子代理进度 — 事件匹配靠猜测

```typescript
if ((ev.type === "tool_call" || ev.type === "observation") && ev.name) {
    // update ALL running background agents' tool count
    for (const key of Object.keys(updated)) {
        if (updated[key].status === "running") {
            updated[key] = { ...updated[key], toolCount: updated[key].toolCount + 1 };
        }
    }
}
```

如果有多个后台子代理同时运行，无法区分事件属于哪个子代理。`child_session_id` 字段存在但这里没有用它来做精确匹配。

**设计质量: 4/10。**

---

## 四、值得重新审视的架构决策

### 12. Plan 和 Build 共享 session_id

`approvals.py:84` 复用同一个 session_id。好处是上下文连续（plan 阶段的探索结果 build 阶段可见）。坏处是 SessionTree 无法区分 plan 和 build 阶段，前端 timeline 混在一起。

**CC 也这么做，但这是 CC 的设计缺陷，不是我们应该模仿的。** 更好的设计：plan session → 审批 → 创建新 build session（parent=plan session），plan 的 summary 作为 context 注入。

### 13. 权限管线的实例属性污染

`_force_interactive`、`_decision_reason`、`_terminate_session`、`_pending_hook_updates` 都是实例属性，但语义上是"本次 check() 调用的临时状态"。如果未来的代码在 `check()` 之外访问这些属性，行为不可预测。

**正确做法：** 这些应该是局部变量，通过返回值传递。

### 14. EventBus 缺少结构化事件类型

`_translate_event` 手动构建 dict，没有 TypedDict 或 dataclass。`child_session_id` 是用 `getattr(event, "child_session_id", None)` 访问的动态属性。前端 `WsMessage` 接口有 20+ 个可选字段。

**正确做法：** 使用 dataclass 或 Pydantic 定义事件类型，确保类型安全。

---

## 五、总结评分

| 维度 | 分数 | 说明 |
|------|------|------|
| 功能完整度 | 9/10 | 核心链路完整，边缘情况覆盖 |
| 设计一致性 | 7/10 | G4 管线重构很好，但 MCP/Worktree 有耦合 |
| 代码质量 | 7/10 | 大部分干净，但实例属性污染、文件存储有待改进 |
| CC 对齐度 | 9/10 | 90%+ 对齐，差异都是有意的设计选择 |
| 可维护性 | 6/10 | 快速迭代积累的技术债务需要偿还 |
| **综合** | **7.6/10** | |

### 优先级最高的 3 个技术债务

1. **Worktree 异步化** — API 不应该同步等 git 操作
2. **PlanRevision SQLite 迁移** — JSON 文件不适合生产
3. **EventBus 类型化** — 动态属性 → 结构化类型

### 一句话总结

**管线设计和 CC 对齐度达到了生产级别，但基础设施层（存储、事件类型、异步模型）有快速迭代留下的技术债务。核心是正确的，边缘需要打磨。**

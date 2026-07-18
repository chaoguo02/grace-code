# Claude Code 会话 / 事件 / 前端可视化差距分析

本文聚焦一个具体问题：

> 在 `grace-code` 的 Web 前端里，新建会话后发送一个问题，前端没有稳定展示每一次 thought / action / observation 的过程。

这不是单纯的前端渲染问题，而是当前 `grace-code` 的 **会话状态模型、事件传输模型、审批暂停模型** 与 Claude Code 公开设计基线仍存在差距。

---

## 1. Claude Code 官方公开基线（调研结论）

以下结论只基于 Claude / Anthropic 官方文档与官方博客。

## 1.1 Claude Code 的 session 是可恢复、可续接、可分叉的，不是一次性直播流

官方文档明确支持：

- `claude -c` 继续最近会话
- `claude -r "<session>"` 按 session ID 恢复会话
- `--fork-session` 从已有会话分叉
- `--no-session-persistence` 才会关闭落盘持久化

来源：

- CLI reference  
  https://code.claude.com/docs/en/cli-usage

尤其是：

- `claude -r "<session>"` resume by ID
- `--no-session-persistence` 才禁用保存

这说明 Claude Code 的正常基线是：

- session 默认持续保存
- 前端 / CLI 可以稍后恢复
- “过程”不应只依赖一次性实时流

## 1.2 Claude Code SDK 的消息流是强类型、带 session_id 的

官方 SDK 文档说明：

- SDK query 会持续产出结构化 message
- `system/init` 消息里带 `session_id`
- 后续可以拿这个 `session_id` 继续恢复

来源：

- Agent SDK overview  
  https://code.claude.com/docs/en/agent-sdk/overview

文档示例明确展示：

- 先在 `SystemMessage subtype == "init"` 中取 `session_id`
- 然后用 `resume=session_id` 继续跑后续 query

这说明 Claude Code 的公开接口基线是：

- session identity 是一等对象
- 输出流不是“匿名 terminal 文本”
- 事件 / 消息天然带可恢复语义

## 1.3 Claude Code 的后台会话与 Agent view 建立在“持续状态”上，而不是依赖附着中的终端

官方 Agent view 文档说明：

- session 可以在没有终端附着时继续运行
- attach/detach 不会中断后台 session
- session 的 transcript 和 state 会保留在磁盘
- 再次 attach / peek / reply 时，可以从原状态恢复

来源：

- Agent view docs  
  https://code.claude.com/docs/en/agent-view
- Agent view blog  
  https://claude.com/blog/agent-view-in-claude-code
- Desktop redesign blog  
  https://claude.com/blog/claude-code-desktop-redesign

其中官方文档明确写到：

- “Sessions keep running in the background without a terminal attached.”
- “The transcript and state stay on disk...”
- 再 attach 时会接着继续

这意味着 Claude Code 的可视化/调度能力，并不是“WebSocket 在线才有过程”，而是：

- 先有持久会话状态
- 再有附着式实时观察

## 1.4 Claude Code 的审批 / 澄清问题是显式暂停点，不是终端专属交互

官方 SDK 文档明确说明：

- 工具审批和澄清问题都会触发 `canUseTool`
- 回调可以无限期 pending
- 执行会暂停，直到用户响应
- 如果用户响应时间很长，TypeScript SDK 还支持 defer，进程退出后稍后恢复

来源：

- Handle approvals and user input  
  https://code.claude.com/docs/en/agent-sdk/user-input

这说明 Claude Code 的公开设计是：

- “等待用户输入”是 runtime 一等状态
- 不是 `input()` 这种 UI 绑定行为
- 前端 / 桌面 / SDK 都能承接这一暂停点

## 1.5 Claude Code 的 UI 允许不同透明度展示，但底层 session 语义不变

桌面重设计博客明确提到：

- Verbose / Normal / Summary 三种视图
- 可以看完整工具调用，或只看结果

来源：

- Desktop redesign blog  
  https://claude.com/blog/claude-code-desktop-redesign

这说明：

- “是否展示详细 thought/tool/result” 是视图策略
- 不是 runtime 是否保留这些事实

换句话说，Claude Code 先保证状态和过程是可恢复、可观察的，再由 UI 决定展示层级。

---

## 2. 对照 grace-code 当前实现的根本问题

## 2.1 当前 WebSocket 是“纯直播”，不是 Claude Code 风格的可恢复观察面

当前 `grace-code` 的 EventBus：

- `SessionRuntime` 通过 `event_callback=self._event_bus.publish` 发事件  
  [server/services/agent_service.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/services/agent_service.py:123)
- WebSocket 订阅后接收实时推送  
  [server/routers/websocket.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/routers/websocket.py:49)

但 EventBus 本身：

- 没有 replay / backlog / cursor
- 没有“新订阅者先补发历史事件”
- 只是在 subscriber 在线时把 event 推出去

关键位置：

- [server/services/event_bus.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/services/event_bus.py:240)
- [server/services/event_bus.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/services/event_bus.py:258)

这和 Claude Code 的公开基线不同：

- Claude Code 的 session/transcript/state 是可恢复的
- 实时流只是观察渠道，不是唯一事实源

## 2.2 当前前端发送消息时没有等待 WS ready，天然会丢第一轮事件

前端链路：

- session 切换后，`ChatView` 才 `connectWs(activeId)`  
  [web/src/components/ChatView.tsx](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/web/src/components/ChatView.tsx:14)
- 发送消息时，`sendChat()` 直接 POST  
  [web/src/stores/chatStore.ts](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/web/src/stores/chatStore.ts:72)

后端注释甚至已经明确要求：

- 先连 WebSocket
- 再调用消息接口

见：

- [server/routers/sessions.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/routers/sessions.py:300)

但当前前端没有任何：

- websocket connected handshake
- ready promise
- send barrier

所以新会话第一轮 thought / tool_call / observation 很容易在订阅建立前就丢掉。

这和 Claude Code 的基线不同：

- Claude Code 的会话不是“看直播才有过程”
- 即便 UI稍后 attach，也应该还能看到之前发生过什么

## 2.3 当前 EventBus 甚至没有按 session_id 精确路由事件

这是当前实现里最危险的架构问题之一。

`EventBus.publish()` 现在是：

- 先 `_translate_event(event)`
- 然后遍历 `self._sessions.values()`
- 只要某 session 有 subscriber，就给它广播

位置：

- [server/services/event_bus.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/services/event_bus.py:240)

这意味着：

- 它并没有真正从 event 中提取所属 `session_id`
- 当前更像“广播给所有在线订阅者”

单 session MVP 下不一定立刻暴雷，但一旦多 session 并行：

- timeline 串流
- session 污染
- 前端看到别的会话事件

这与 Claude Code Agent view / background sessions 的公开语义显著不符。

Claude Code 的官方基线非常明确：

- session 是独立对象
- 可以 attach 某一个 session
- 可以查看某一个 session 的状态与 transcript

而不是“所有事件广播后靠前端猜”。

## 2.4 当前 `/events` 查询层是存在的，但前端没有把它作为可靠补偿面

`SessionService.get_events()` 已经能从 log 目录扫描 JSONL：

- [server/services/session_service.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/services/session_service.py:133)

而且后端也提供了：

- `GET /api/sessions/{session_id}/events`

见：

- [server/routers/sessions.py](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/server/routers/sessions.py:229)

但前端当前只做了：

- 初始加载 persisted messages
- 运行中看 WS
- 完成后并不会补拉 `/events`

相关位置：

- [web/src/stores/chatStore.ts](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/web/src/stores/chatStore.ts:31)
- [web/src/stores/chatStore.ts](/abs/path/D:/StudyProjects/ProjectBench/forge-agent/web/src/stores/chatStore.ts:86)

这导致 timeline 不是“历史事实 + 实时增量”，而是：

- 历史消息（message）
- 瞬时事件（ws）

只要瞬时事件漏了，就永远漏了。

这与 Claude Code 的会话/状态模型不一致。

## 2.5 当前审批链仍明显带 UI 绑定残留

虽然 Web 端已经加了 approval 路由，但整体项目里审批设计仍没完全从终端交互解耦：

- `entry/cli.py` 中仍有 `input(...)`
- `entry/renderer.py` 中仍有 `input(...)`

这说明当前 runtime 还没有完全收束到：

- runtime 产生 “需要审批”
- UI 决定如何展示
- 用户响应后恢复运行

而 Claude Code 的公开语义正是这个方向。

来源：

- https://code.claude.com/docs/en/agent-sdk/user-input

---

## 3. Claude Code 是如何从设计上避免这类问题的

这里不声称知道其内部未公开实现，只基于官方公开语义作合理推断。

## 3.1 Session / transcript / state 是一等事实源

官方明确公开了：

- session_id
- resume
- fork-session
- no-session-persistence
- background sessions state on disk

因此可以合理推断：

- UI 不是唯一过程承载者
- session state 和 transcript 才是过程事实源
- attach / view / desktop / agent view 都建立在同一底层 session 之上

**对 grace-code 的启发：**

- WebSocket 只能是增量观察层
- 事实源必须是 `SessionStore + EventLog + persistent runtime state`

## 3.2 实时流必须可被 session identity 约束

官方 SDK message schema 和 CLI/agent-view 语义都强调 session identity。

因此可以合理推断：

- 任何实时流都必须强绑定 session_id
- attach 某 session，只看到该 session 相关事件
- 后台 session 与前台 attach 之间存在明确映射

**对 grace-code 的启发：**

- EventBus 必须从 publish 开始就 session-scoped
- 不能广播后再靠 UI 区分

## 3.3 审批 / 用户输入是 runtime 状态，不是 terminal 操作

官方 SDK 说得非常明确：

- `canUseTool` 会暂停执行
- 可以等待很久
- 可以 defer 后稍后恢复

**对 grace-code 的启发：**

- approval 必须成为 session runtime 的显式 paused state
- Web / CLI / future desktop 都只是审批 UI
- 不能把 terminal `input()` 当成审批系统本身

## 3.4 视图层可以有不同透明度，但底层事件不能丢

Claude Code 桌面支持 Verbose / Summary 等不同透明度视图。

因此合理推断：

- 详细过程是底层可用的
- UI 只是决定是否显示 thought/tool/result

**对 grace-code 的启发：**

- 先把完整 timeline 收齐
- 再做 Summary / Normal / Verbose 切换
- 不能因为 MVP 简化，就让底层事件链先变成不可靠

---

## 4. grace-code 目前还缺多少层

按优先级分为 P0 / P1 / P2。

## P0：必须先补，不然“过程展示”永远不稳

### P0-1. EventBus 改成严格 session-scoped

当前缺口：

- publish 不按 session_id 路由
- 事件被广播给所有在线 subscriber

需要改成：

- 每个 runtime event 必须带所属 session_id
- `publish(session_id, event)` 或可可靠解析出 session_id
- 只发给对应 session 的 subscriber

### P0-2. 实时事件必须支持补偿，不可纯直播

当前缺口：

- 订阅前产生的事件会丢

需要改成：

- 前端打开 session 时先拉历史 `/events`
- WS 只负责增量
- 或 EventBus 自带 ring buffer + subscriber replay

更接近 Claude Code 的方式是：

- 持久事实源 + attach 增量

### P0-3. 发送消息前必须等待连接 ready

当前缺口：

- sendChat 不等待 websocket connected

需要改成：

- 新会话创建后，WS connected 才允许发第一条消息
- 或后端允许“执行先开始，前端稍后通过 /events 追平”

最稳的做法是两者都做。

### P0-4. timeline 需要从 event log 恢复，而不是只从 messages 恢复

当前缺口：

- messages 只能复原 conversation
- 不能复原完整 ReAct trace

需要改成：

- timeline = persisted events + live events

---

## P1：第二层收口，决定 Web 是否真正像 Claude Code

### P1-1. 审批状态收束为 runtime paused state

要从：

- 终端 prompt / input 驱动

变成：

- runtime paused
- approval.required event
- UI 响应
- runtime resume

### P1-2. 前端要区分 message stream 与 execution timeline

当前缺口：

- `timeline` 混合了 persisted messages 与 ws events

需要改成：

- conversation panel
- execution timeline panel
- maybe merged view，但底层结构分开

Claude Code 的设计语义里，本来就有：

- transcript / conversation
- session state / status / approvals
- tool transparency layer

### P1-3. session detail 页要能表达“当前卡在哪”

Claude Code agent view 强调：

- 哪些正在跑
- 哪些在等你输入
- 哪些已完成

`grace-code` 现在缺：

- blocked on approval
- running tool
- waiting subagent
- completed with preserved worktree

这层状态投影。

---

## P2：第三层增强，向 Claude Code 体验靠拢

### P2-1. 统一 attach / peek / resume 语义

Claude Code 公开语义里：

- attach 某 session
- detached 继续跑
- 之后再回来

`grace-code` 后面也应逐步形成：

- list sessions
- attach session
- inspect current step
- fetch missed timeline

### P2-2. 支持多透明度视图

先有：

- full timeline

再叠加：

- summary only
- tools only
- verbose

### P2-3. 将 subagent / worktree 纳入统一 timeline

当前 `grace-code` 的 subagent/worktree 是强项，但 Web 还没把它们完整投影成前端一等视图。

长期应该做到：

- child session start
- child session done
- child result summary
- worktree preserved / inspect / apply / discard

都进入同一 session timeline / side panel。

---

## 5. 建议的改造顺序

### Batch A：把事件链从“不可靠直播”改成“可恢复 timeline”

1. EventBus session-scoped
2. 事件统一携带 session_id
3. 前端打开 session 先拉 `/events`
4. WS 只接增量
5. 完成后再补拉一次 `/events`

### Batch B：把审批链从“终端行为”改成“runtime paused state”

1. approval.required event
2. pending approval store
3. approve/reject/resume API
4. Web / CLI 都走同一恢复链

### Batch C：把 timeline / conversation / session state 拆清

1. conversation messages
2. execution events
3. session status
4. child/subagent/worktree state

### Batch D：再做前端展示层增强

1. verbose / normal / summary
2. agent view 风格 session state board
3. subagent / worktree panel

---

## 6. 总结

当前 `grace-code` 在 Web 这条线上最本质的问题不是“前端没把块渲染出来”，而是：

**你们现在的事件链还停留在“有 WebSocket 就算有过程展示”的阶段；而 Claude Code 的公开基线已经是“session/state/transcript 持续存在，实时流只是附着观察层”。**

因此，要真正向 Claude Code 靠拢，最先要补的不是更多 UI，而是这 4 件事：

1. session-scoped event routing
2. replay / history-backed timeline
3. send-before-ready race elimination
4. runtime-native approval pause/resume

这些收完之后，你们前端才能稳定地看到每一步 thought / action / observation；否则只是偶尔看起来能跑，但本质上仍然不可靠。


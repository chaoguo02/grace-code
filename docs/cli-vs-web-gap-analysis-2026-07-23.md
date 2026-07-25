# CLI vs Web 模式完整差距分析

> 审计日期：2026-07-23 | 基于 master 分支 `df4d4fc`

## 总体评估

Web 模式已达到 CLI 约 **70%** 的功能覆盖。核心 agent 执行路径（ReActAgent → TaskStateMachine → EvidenceLedger）共享同一代码，两边行为一致。差距集中在**会话管理**、**自动维护**、**MCP 初始化时序**三个方面。

---

## P0 — 立即修复 (1 项)

| # | 问题 | 详情 |
|---|---|---|
| 1 | **Web 无自动压缩** | `compact_session_async()` 存在但从未在 chat 完成后自动调用。CLI 每轮结束后检查 `config.context.auto_compact_after_round` → 触发压缩。Web 端长会话会超出 context window → 截断/失败。<br>文件: `agent_service.py:789` vs `chat.py:393-408` |

---

## P1 — 显著功能降级 (8 项)

### 已修复 (记忆系统)
~~MemoryContext 缺失、HookDispatcher 缺失、consolidation 缺失~~ → `df4d4fc` 已修复

### 待修复

| # | 问题 | CLI 行为 | Web 现状 | 文件 |
|---|---|---|---|---|
| 2 | **无 SessionState** | `SessionState` 跟踪 `active_task`、`completed_tasks`、`rolling_summary`、`compaction_count` | 完全不跟踪 | `chat.py:121-125` vs `agent_service.py` |
| 3 | **无 GoalStore/verify** | `--verify` 脚本 + `GoalStore` + `/goal` 命令 | 无验证回调 | `cli.py:570-587` |
| 4 | **无 Skill fork 执行** | `/skill-name` 触发 forked subagent 上下文 | 无此机制 | `chat.py:359-384` |
| 5 | **MCP 初始化竞态** | 同步 `mcp_integration.initialize()` → 确保就绪 | 后台线程连接 → agent 可能在 MCP 就绪前启动 | `cli.py:559-567` vs `agent_service.py:160-176` |
| 6 | **无 --plan-action** | `--plan-action review/save/execute` 控制 plan 批处理 | 无对应 API 参数 | `cli.py:332-338` |
| 7 | **无 --delegate-to** | 显式委托到指定 agent | 无对应 API 参数 | `cli.py:346-351` |
| 8 | **无 --agents** | 会话级自定义 agent 定义 (JSON) | 无 | `cli.py:352-358` |
| 9 | **Cache stats 未推送到前端** | `renderer.on_round_end(cache_stats=...)` | `ChatPipeline.finish()` 不发送 | `chat.py:311` vs `chat_pipeline.py:274-340` |

---

## P2 — 增强项 (10 项)

| # | 问题 | 详情 |
|---|---|---|
| 10 | `artifact_threshold_tokens` 未设置 | CLI 从 config 读取，Web 用默认值 |
| 11 | `thought_callback` / `token_callback` 缺失 | CLI 实时更新 token 计数，Web 不推送 |
| 12 | `--read/--write` 路径 ACL 无等效 | CLI 有文件读写沙箱 |
| 13 | `--sandbox` (Docker) 无等效 | CLI 有容器沙箱 |
| 14 | `--replan` / `--max-replans` 无等效 | |
| 15 | 历史/日志浏览命令无等效 | `/history`, `/log` CLI 命令 |
| 16 | MCP 服务器管理 API 缺失 | `mcp add/list/get/remove` 无 Web 端点 |
| 17 | MCP 资源/提示检查缺失 | `/mcp` 命令显示服务器状态 |
| 18 | `/stats` 命令无等效 | CLI 有 stats 打印 |
| 19 | 无压缩反复触发检测 | CLI 每轮重置 thrashing 计数器 |

---

## Web 模式独有的优势 (5 项)

| # | 功能 | 详情 |
|---|---|---|
| W1 | StatsRecorder 逐工具统计 | CLI 无此能力 |
| W2 | 配置热重载 `_maybe_reload_rules()` | CLI 需重启 |
| W3 | TOCTOU 并发防护 `try_acquire_session()` | CLI 单进程不需要 |
| W4 | @mention 文件注入 `resolve_mentions()` | CLI 不支持 |
| W5 | Plan 文件管理 API | Web 特有 |

---

## 修复优先级

```
立即 (P0):
  1. 自动压缩 — ChatPipeline.finish() 后触发 compact_session_async()

近期 (P1):
  2. MCP 初始化竞态 — gate agent 直到 MCP 就绪
  3. SessionState — 创建 lightweight session tracker
  4. Cache stats → WS 事件

后续 (P2):
  5. Skill fork / delegate-to / agents / plan-action API 参数
  6. 增强项逐项实现
```

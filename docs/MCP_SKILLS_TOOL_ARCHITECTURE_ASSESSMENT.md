# MCP / Skills / Tool 架构评估与修复路线

> 日期: 2026-07-30  
> 范围: `mcp/`, `agent/mcp/`, `skills/`, `tools/`, `core/base.py`, `entry/bootstrap/`, `server/services/`  
> 方法: 逐文件代码审计 + Web 调研 Claude Code 正确实现

## 0. 整改结果（2026-07-30）

本文第 1 节记录的是整改前基线。当前生产链路已经收敛为单路：

- Tool：`tools/factory.py::build_tool()` 是动态工具唯一工厂；
  `tools/pool.py::assemble_tool_pool()` 是唯一池装配器。运行时只接受
  `core.base.BaseTool`，已删除 MCP `ConcreteTool` 和 proxy 双转换。
- Tool schema 固定按 builtin、MCP 两个分区排序；builtin 前缀不会因 MCP
  增删而改变。MCP 支持 `tst`、`tst-auto`、`standard` 三种加载模式，
  默认 `tst`，激活状态只存于 `MCPToolProps`。
- Skills：`SkillRegistry.for_project()` 统一 Managed、User、Project、
  Additional、Legacy、Bundled、MCP 七类源，并行发现、realpath 去重、
  按优先级合并。L1 metadata、L2 body、L3 resources 分层读取。
- MCP skill 标记为不可信，禁止 inline command；本地 skill 才允许通过
  Runtime 执行动态命令。
- Skills live reload 使用 300ms watcher 和源 fingerprint；registry
  `close()` 会停止 watcher、清 active buffer。Compaction 会恢复最近 skill，
  单 skill 约 5000 tokens、总计约 25000 tokens。
- MCP 唯一实现位于 `agent/mcp/`。watchdog 每 30 秒检查一次；
  60 秒滑动窗口最多重启 5 次，退避为 1/2/4/8/16 秒。重启总是创建
  新 bridge，并比较 initialize/serverInfo 与本地可执行文件 fingerprint。
- MCP shutdown 先 drain in-flight calls，再关闭 session/transport；stdio
  无法退出时执行 terminate → wait(5s) → kill，并记录
  `leaked_operation`。
- `ToolRegistry` 显式持有 artifact/evidence/skill/capability 依赖，
  derived registry 不拥有生命周期；根 registry 是 Tool、Skills、MCP
  唯一关闭入口。
- 已删除 `mcp/protocol.py`、`mcp/registry.py`、`mcp/transport.py`、
  `tools/mcp_tool.py`、`agent/mcp/tool_types.py`。

当前保留的 compatibility 仅是命名导出（例如 Python snake_case 与文档中的
camelCase 别名），不包含第二套执行、缓存、刷新或生命周期策略。

---

## 1. 问题全景（整改前基线）

### 1.1 死代码：两套并行的 MCP 实现

```
Legacy 层 (未连接)                    Runtime 层 (活跃)
─────────────────                    ─────────────────
mcp/protocol.py  McpClient           agent/mcp/client.py    MCPToolBridge
mcp/transport.py StdioTransport      agent/mcp/sync_bridge  SyncMCPToolManager
mcp/registry.py  McpRegistry         agent/mcp/tool_adapter adapt_mcp_tools
tools/mcp_tool   McpToolWrapper      agent/mcp/config       load_mcp_config
                                     agent/session/mcp_integration MCPToolIntegration
```

**Legacy 层 (`mcp/`) 是完整实现但目前零调用。** `McpRegistry`/`McpClient`/`StdioTransport`/`McpToolWrapper` 4 个文件 + 约 800 行代码未被当前运行时引用。

### 1.2 收尾工作缺失

```
子系统         连接         使用         关闭/清理        恢复
───────        ────         ────         ────────         ────
MCP            ✅           ✅           ❌ wait()无超时    ❌ 无健康检查
Skills         ✅           ✅           ❌ 无unload       ❌ 无重载
Tools          ✅           ✅           ❌ 无shutdown     ❌ 无deactivate
Pipeline       ✅           ✅           ⚠️ filtered()丢cb  N/A
```

### 1.3 具体缺陷 (按严重度排序)

#### 🔴 Critical

| # | 文件:行 | 问题 |
|---|---------|------|
| C1 | `agent_service.py:947` | MCP readiness gate 检查不存在的 `_connected` 属性 (应是 `_initialized`)，永远 pass |
| C2 | `mcp/transport.py:110` | `await self._process.wait()` 无 timeout → 僵尸进程挂死 shutdown |
| C3 | `agent/mcp/client.py:452` | class-level `_next_id` 无锁 → 并发 MCP 调用 request ID 冲突 |
| C4 | `agent/mcp/sync_bridge.py:92` | daemon thread → 硬退出时 MCP 子进程变孤儿进程 |
| C5 | `core/policy_registry.py:329-355` | Skill modifier 跨 turn 累积不清除 |
| C6 | `core/base.py:738-742` | `filtered()` 创建新 registry 不传 `capability_registry` |
| C7 | `core/tool_execution.py:169-232` | 仅 MCP 工具有 resource governance + timeout，ShellTool 无保护 |

#### 🟡 High

| # | 文件:行 | 问题 |
|---|---------|------|
| H1 | `agent/mcp/client.py:189-201` | 非超时异常被吞成 `MCPCallResult(is_error=True)`，重试逻辑不触发 |
| H2 | `agent/mcp/client.py:99` | transport 失败后 `is_connected` 仍为 True |
| H3 | `agent/mcp/sync_bridge.py:241-242` | `join(5s)` 超时后仍调 `_loop.close()` |
| H4 | `core/base.py:625-641` | 无 `unregister()`/`remove()` |
| H5 | `skills/registry.py:235-236` | frontmatter YAML 解析失败静默吞掉 |
| H6 | `entry/chat.py:344-389` | fork skill 永远走 subagent，忽略 `context` 声明 |

### 1.4 架构腐化指标

- **私有属性跨层访问**: `_skill_registry`, `_artifact_store_ref`, `_evidence_ledger_ref` 通过 `getattr(registry, "_xxx", None)` 跨 PolicyAwareToolRegistry wrapper 访问
- **Monkey-patching**: `build_registry()` 给 ToolRegistry 打补丁而非通过构造器注入
- **无统一接口**: 每个子系统有自己的生命周期方法名 — `disconnect_all()`, `close_all()`, `shutdown()`, `dispose()`, `cleanup()` — 无 `close()` 协议

---

## 2. Claude Code 的正确实现模式

以下模式来自 Claude Code 2025 源码分析及社区文档。

### 2.1 Tool 系统

```
┌─────────────────────────────────────────────────────┐
│                  assembleToolPool()                  │
│                                                     │
│  getAllBaseTools()    getTools()     MCP Tools       │
│  (compile-time)    (runtime filter) (external)      │
│        │                  │              │          │
│        ▼                  ▼              ▼          │
│  ┌──────────┐    ┌──────────────┐  ┌──────────┐    │
│  │ ~31 core │    │ feature-gated │  │ MCP      │    │
│  │ tools    │    │ (dead-code   │  │ servers  │    │
│  │ always   │    │  eliminated) │  │          │    │
│  │ loaded   │    │              │  │          │    │
│  └──────────┘    └──────────────┘  └──────────┘    │
│        │                  │              │          │
│        └──────────────────┴──────────────┘          │
│                         │                           │
│                    去重 + 排序                        │
│              (built-in prefix 稳定缓存)               │
│                         │                           │
│                         ▼                           │
│              ┌──────────────────┐                   │
│              │  Tool[] (flat)   │                   │
│              │  + deferred MCP  │                   │
│              │  + ToolSearch    │                   │
│              └──────────────────┘                   │
└─────────────────────────────────────────────────────┘
```

**关键设计决策:**

1. **`buildTool()` 工厂 + `TOOL_DEFAULTS`** — 每个工具通过工厂创建，注入 fail-closed 默认值:
   - `isConcurrencySafe: false` (假设不安全)
   - `isReadOnly: false` (假设需要权限)
   - `checkPermissions: allow` (由 8 层权限链最终决定)

2. **缓存分区排序** — built-in tools 排在前面 (稳定前缀)，MCP tools 排在后面。添加/删除 MCP tool 不使 built-in 的缓存失效。`$0.003/call → $0.036/call` 的差距。

3. **渐进式加载 (ToolSearch)** — 3 种模式:
   - `tst` (默认): MCP tools 只发名字 + searchHint。模型必须调用 `ToolSearchTool` 获取完整 schema
   - `tst-auto`: schema tokens > 10% context_window 时自动启用
   - `standard`: 全量发送

4. **编译期死代码消除** — Bun `feature('FLAG')` 宏物理删除未启用的工具代码。不是 `if(enabled)` 分支，而是 `require()` 调用本身被删除。

### 2.2 Skills 系统

```
┌───────────────────────────────────────────────────────┐
│                 Progressive Disclosure                 │
│                                                       │
│  L1: Metadata      启动时         ~100 tokens         │
│      name + description 注入 system prompt            │
│                                                       │
│  L2: Instructions  触发时         ~1500-5000 tokens   │
│      SKILL.md 完整 body                                │
│                                                       │
│  L3: Resources     执行中        无限                  │
│      scripts/, templates/, references/               │
└───────────────────────────────────────────────────────┘

7 个源 → 并行加载 → realpath 去重 → 按优先级合并:
  1. Managed (企业控制)
  2. User (~/.claude/skills/)
  3. Project (.claude/skills/)
  4. Additional Dirs (--add-dir)
  5. Legacy (.claude/commands/)
  6. Bundled (编译进二进制)
  7. MCP (远程, 不可信)
```

**关键设计决策:**

1. **纯 LLM 选择** — 无正则、无 embedding、无分类器。Model 通过 description 自然语言理解匹配
2. **`paths` 条件激活** — `discoverSkillDirsForPaths()` 在 model 触碰文件时动态发现嵌套 skill
3. **MCP 安全边界** — MCP 来源的 skill 不执行 inline shell 命令。本地 skill 可以
4. **Compaction 感知** — auto-compaction 后重新注入最近调用的 skill (前 5000 tokens，总计 ≤25000)
5. **Live Reload** — chokidar watch + 300ms debounce + `ConfigChange` hooks + 清 memoization cache

### 2.3 MCP 生命周期

```
┌──────────────────────────────────────────────────┐
│              MCP Server Lifecycle                 │
│                                                  │
│  connect → initialize → tools/list ⇄ tools/call  │
│     │                                    │       │
│     └── Watchdog 监控 ───────────────────┘       │
│          ├─ 健康检查: 30s 间隔                    │
│          ├─ 滑动窗口: 5 restarts / 60s           │
│          ├─ 指数退避: 1s→2s→4s→8s→16s           │
│          └─ 放弃后: mark failed                  │
│                                                  │
│  shutdown:                                        │
│    1. drain in-flight calls (with timeout)       │
│    2. close transport                             │
│    3. kill subprocess (with timeout)              │
│    4. force kill if still alive                   │
│    5. log leaked_operation                       │
└──────────────────────────────────────────────────┘
```

**关键设计决策:**

1. **Watchdog + 滑动窗口** — 不是无限重试。5 次/60s 后放弃
2. **`notifications/tools/list_changed`** — server 端工具变更时推送通知，client 端重新发现
3. **Fingerprint 版本检测** — `initialize` 响应中嵌入版本指纹。重连时对比，不匹配则 force respawn
4. **Ephemeral subprocess** — 每次 cycle 重建 subprocess。代价 ~200ms，收益是"磁盘版本 = 内存版本"
5. **SIGTERM → wait(5s) → SIGKILL** — 两阶段进程终止。不能优雅退出的记录 `leaked_operation`

---

## 3. 正确链路图

### 3.1 工具注册链路 (当前 vs 目标)

```
当前 (手动 import 列表):                      目标 (工厂 + 自动发现):
─────────────────────                        ──────────────────────
build_registry()                             ToolRegistry.__init__()
  │                                            │
  ├─ from tools.X import ToolA                 ├─ _discover_builtins()
  ├─ registry.register(ToolA())               │    glob("tools/**/Tool.py")
  ├─ from tools.Y import ToolB                 │    → build_tool(module)
  ├─ registry.register(ToolB())               │
  ├─ ... (100+ 行重复)                          ├─ _discover_mcp()
  └─ monkey-patch private attrs                │    → mcp.list_tools()
                                               │    → build_tool(mcp_def)
问题:                                            │
- 每个工具手动 import+register                   ├─ _apply_filters()
- 私有属性 monkey-patch                          │    → deny_rules
- 无去重                                         │    → isEnabled()
- 无编译期消除                                    │    → feature_gates
                                               │
                                               └─ _sort_for_cache()
                                                    → built-in prefix
                                                    → MCP suffix

                                               解决:
                                               - 工具即模块 (convention over config)
                                               - build_tool() 工厂统一默认值
                                               - 缓存感知排序
```

### 3.2 Skill 生命周期链路 (当前 vs 目标)

```
当前:                                        目标 (CC-aligned):
─────                                        ────────────────────
SkillRegistry.__init__()                     SkillRegistry.__init__()
  │                                            │
  ├─ _discover() 一次性扫描                      ├─ _discover() 
  │                                            │
  ├─ load_and_render()                          ├─ load_and_render()
  │   ├─ 读 SKILL.md                            │   ├─ L1: metadata (启动时)
  │   ├─ 展开 !cmd                               │   ├─ L2: body (触发时)
  │   └─ $ARGUMENTS 替换                         │   ├─ L3: resources (执行中)
  │                                            │   └─ skillChangeDetector (watch)
  ├─ activate() → SkillContextBuffer            │
  │                                            ├─ activate()
  └─ ❌ 无 deactivate/clear                     │   ├─ 注入到 context
     ❌ 无 shutdown                             │   └─ 注册 allowed-tools 覆盖
     ❌ 无 hot-reload                           │
     ❌ modifier 跨 turn 泄漏                     ├─ deactivate()
                                               │   ├─ 恢复 tool 权限
                                               │   ├─ 恢复 model 覆盖
                                               │   └─ 清 context buffer
                                               │
                                               ├─ compaction 感知
                                               │   └─ 重新注入最近 skill
                                               │
                                               └─ shutdown()
                                                   └─ 清所有 state

核心修复:
- activate/deactivate 对称
- modifier 在 turn 边界自动恢复
- mtime 检测 + 热重载
```

### 3.3 MCP 生命周期链路 (当前 vs 目标)

```
当前:                                        目标:
─────                                        ────
McpRegistry.connect_all()                    MCPManager.start()
  │                                            │
  ├─ 一次性连接所有 server                        ├─ 连接所有 server
  │   ├─ connect()                              │   ├─ connect()
  │   ├─ initialize()                           │   ├─ initialize()
  │   └─ fetch_tools()                          │   └─ discover_tools() + 缓存
  │                                            │
  ├─ ❌ 失败后无重试                               ├─ Watchdog 启动
  ├─ ❌ 无健康检查                                 │   ├─ 30s 间隔 ping
  ├─ ❌ crash 后工具不可用                          │   ├─ 滑动窗口重试
  │                                            │   └─ 指数退避
  ├─ 使用: call_tool()                           │
  │   ├─ sync_bridge 超时                         ├─ 使用: call_tool()
  │   └─ ❌ transport 失败不标记断开                   │   ├─ 统一超时
  │                                            │   └─ ✅ 失败标记 + 自动重连
  ├─ disconnect_all()                           │
  │   ├─ close transport                        ├─ shutdown()
  │   └─ ❌ wait() 无超时, 可能永久挂起                 │   ├─ drain in-flight (timeout)
  │                                            │   ├─ close transport
                                               │   ├─ proc.kill() + wait(5s)
                                               │   ├─ proc.kill() again if alive
                                               │   └─ log leaked_operation

核心修复:
- Watchdog + 滑动窗口重试
- 健康检查 (tools/list ping)
- 两阶段子进程终止 + 超时
- 连接状态正确标记
```

### 3.4 全局关闭链路

```
                    AgentService.shutdown()
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    governor.shutdown()  cancel_all()   backend.close()
    (停止新申请)         (取消运行中)    (关闭 LLM 连接)
           │               │               │
           └───────────────┼───────────────┘
                           │
                           ▼
                  runtime.dispose()
                  ┌────────┼────────┐
                  ▼        ▼        ▼
           cancel_tokens  drain   shutdown
           (取消令牌树)   (join   (shared
                         threads)  executor)
                           │
                           ▼
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        MCP.shutdown()  Skills.clear() Tools.shutdown()
        (drain+kill)    (恢复modifier)  (cancel futures)
                           │
                           ▼
                   memory + MCP disconnect
```

---

## 4. 修复路线图

### Phase A: 消除死代码 + 统一接口 (低风险)

| 步骤 | 内容 |
|------|------|
| A1 | 删除 `mcp/protocol.py`, `mcp/registry.py`, `mcp/transport.py`, `tools/mcp_tool.py` (legacy MCP 层) |
| A2 | 定义 `Closeable` 协议: `close(timeout: float) -> None` |
| A3 | 所有子系统实现 `Closeable`: MCPManager, SkillRegistry, ToolRegistry, StreamingToolExecutor |
| A4 | `build_registry()` 去 monkey-patch: `_artifact_store_ref` 等改为构造器参数 |

### Phase B: 修复生命周期 (中风险)

| 步骤 | 内容 |
|------|------|
| B1 | MCP: 添加 Watchdog + 滑动窗口重试 + `tools/list` 健康检查 |
| B2 | MCP: 修复 `_connected` bug + 两阶段子进程终止 |
| B3 | Skills: 添加 `deactivate()` — turn 边界恢复 tool/model 覆盖 |
| B4 | Skills: 添加 `SkillChangeDetector` — mtime 检测 + 热重载 |
| B5 | Tools: 添加 `unregister()` + `shutdown()` |
| B6 | 统一 shutdown 顺序: governor → cancel → drain → close(transport) → close(executor) → close(mcp) |

### Phase C: 架构升级 (高风险)

| 步骤 | 内容 |
|------|------|
| C1 | `build_tool()` 工厂模式 — 替代手动 import+register |
| C2 | 工具缓存分区排序 — built-in 前缀稳定 prompt cache |
| C3 | 渐进式 MCP tool 加载 — ToolSearch 模式 |
| C4 | `filtered()` 修复 — 传递 `capability_registry` |
| C5 | 非 MCP 工具也接入 ResourceGovernor + timeout |

---

## 5. 反思

### 为什么这些问题会积累？

1. **两套 MCP 实现是"重写但未删除旧代码"的典型**。新实现 (`agent/mcp/`) 建好后，旧实现 (`mcp/`) 没有被标记 deprecated 或删除。随着时间推移，维护者不再记得旧代码的存在。

2. **"只管开始，不管结束"是系统性问题**。不仅是 MCP/Skills/Tools，连 ResourceGovernor 的 Phase 0-4 改造也显示：系统的 "startup" 路径有精心设计的顺序，但 "shutdown" 路径是事后拼接的。

3. **私有属性跨层访问是架构边界模糊的症状**。`_skill_registry` 通过 `getattr(registry, "_skill_registry", None)` 跨 3 层 wrapper 访问——每层都可能丢失这个属性。正确的做法是通过接口显式传递。

4. **Claude Code 的 build_tool() + assembleToolPool() 模式揭示了关键洞见**: 工具注册不应该是手动的 import 列表，而应该是"约定大于配置"的自动发现。每个工具目录有统一结构 (`Tool.ts`, `prompt.ts`, `UI.tsx`)，注册是自动的。

### 哪些是真正的硬骨头？

- **Skill modifier 跨 turn 累积** 是最隐蔽的 bug——它不会 crash，不会报错，只是让 Skill 的 allowed-tools/disallowed-tools 限制逐渐偏离预期。
- **MCP 僵尸进程挂死 shutdown** 在生产环境中是真实风险——一旦发生，进程无法优雅退出，需要外部 kill。
- **`filtered()` 丢失 `capability_registry`** 意味着特定场景下工具拦截静默失效——这是安全漏洞。

### 哪些可以先放放？

- **编译期死代码消除** (Bun feature macros) 在这个 Python 项目中不适用——Python 没有编译期宏。可以用模块级别的 `__all__` + 显式导出替代。
- **渐进式 MCP tool 加载 (ToolSearch)** 是优化而非正确性问题。当前 MCP tool 数量少时可以延后。

---

## 参考

- [Claude Code Tool System Architecture](https://github.com/openedclaude/claude-reviews-claude/blob/main/architecture/02-tool-system.md)
- [Claude Code 工具系统深度拆解](https://gitcode.csdn.net/69ec241c54b52172bc6feb28.html)
- [Claude Code Skills Framework](https://www.cubic.dev/wikis/davila7/claude-code-templates?page=skills-framework)
- [Claude Code MCP Reliability Playbook](https://github.com/jeremylongshore/claude-code-plugins-plus-skills/blob/main/000-docs/198-DR-SOPS-03-mcp-reliability.md)
- [MCP stdio auto-reconnect Issue](https://github.com/anthropics/claude-code/issues/43177)
- [MCP subprocess respawn Issue](https://github.com/anthropics/claude-code/issues/59500)
- [Agent Skills, Stripped of Hype](https://stevekinney.com/writing/agent-skills)

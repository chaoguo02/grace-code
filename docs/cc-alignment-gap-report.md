# Forge Agent — CC 对齐差距报告

> 综合 WebSearch（Claude Code 官方文档 + 源码分析）+ explore agent 代码审查 + 直接代码阅读的结果。

---

## 一、工具命名与参数（Phase 3 — 已完成）

### ✅ 已正确实现

| 项目 | 状态 | 说明 |
|------|------|------|
| 11 个核心工具 CC 命名 | ✅ 已对齐 | Read, Write, Edit, Grep, Glob, Bash, WebSearch, WebFetch, Agent, ReportFindings, Skill |
| 别名系统 | ✅ 已对齐 | `BaseTool.aliases` tuple，兼容旧名 |
| Read offset/limit | ✅ 已对齐 | CC 参数完全匹配 |
| Grep 9 参数 | ✅ 已对齐 | output_mode / -A / -B / -C / glob / type / head_limit / -i / multiline |
| WebSearch allowed_domains | ✅ 已对齐 | CC 参数完全匹配 |
| WebFetch prompt | ✅ 已对齐 | CC 参数完全匹配 |
| 权限规则 builtin allow_tools | ✅ 已修复 | 旧名→新名映射（file_read→Read 等） |
| _TOOL_ALIASES 映射 | ✅ 已对齐 | 所有遗留名→CC 规范名 |
| agent 系统提示词 | ✅ 已修复 | file_read→Read, search_text→Grep 等 |
| ExitPlanMode intent_override | ✅ 已修复 | 改为硬编码 "edit"，不再循环审批 |

---

## 二、Subagent 状态机（核心流程）

### ✅ 已正确实现

| 组件 | 状态 | 说明 |
|------|------|------|
| AgentTool 名称 "Agent" | ✅ | 别名 "task" 兼容 |
| AgentDefinition 定义解析 | ✅ | `.md` + YAML frontmatter |
| 三层发现优先级 | ✅ | project > user > built-in |
| mtime 缓存 | ✅ | 自动重载 |
| 工具别名解析 resolve_tool_name | ✅ | 遗留名→规范名 |
| DelegationPolicy allowlist | ✅ | 只有白名单中的子代理可 spawn |
| DelegationScope READ_ONLY/ANY | ✅ | 分析型父代理无法派发写子代理 |
| AgentDepth 嵌套限制 | ✅ | MAX_SUBAGENT_DEPTH = 5 |
| ContextOrigin FRESH / PARENT_SNAPSHOT | ✅ | fresh = 隔离, fork = 继承 |
| ExecutionPlacement FOREGROUND/BACKGROUND | ✅ | 前台/后台执行 |
| spawn_agent 统一入口 | ✅ | named + fork 两条路径 |

### ❌ 未实现（需新增）

| 组件 | 说明 |
|------|------|
| `--agents` CLI JSON 传递 | CC 支持 session 级 agent 定义，我们只有文件 |
| 嵌套 subagent 再 spawn 子代理 | CC 2026年6月支持 3 层声明式嵌套 |
| 权限系统 `Agent(name)` deny 语法 | CC 用 `permissions.deny: ["Agent(explore)"]` |
| Agent 级别的 hook 执行 | 解析了 hooks 字段但未接入 hook dispatcher |
| Agent 级别的 MCP 服务器 | 解析了 mcp_servers 字段但未接入 MCP 集成 |
| Agent tool `Agent(worker,researcher)` 限制语法 | tools 字段中声明可 spawn 的子代理类型 |
| color 字段 UI 显示 | 解析了但未在任何 UI 中使用 |
| memory 持久化记忆 | 解析了但未接入 MemoryStore |
| initial_prompt 自动注入 | 解析了但未在 --agent 启动时注入 |
| skills 预加载 | 解析了但未接入 SkillRegistry |
| effort 推理力度 | 解析了但未传入 LLM backend |
| permission_mode 权限覆盖 | 解析了但未在 permission pipeline 中生效 |

### ⚠️ 已实现但错误（需修正）

| 组件 | 问题 | 影响 |
|------|------|------|
| MCP tool 发放逻辑 | `_mcp_tool_names_for_spec` 用 `spec.intent is EDIT` 判断 |
不清楚我们前面改的是对的还是需要进一步修正——按 CC 模式，MCP 应该从 tools/mcpServers 字段声明，不是从 intent 推断 | 自定义 agent 可能拿不到 MCP |
| Background 字段语义 | 我们存为 `bool`，CC 的 background 控制默认执行模式（auto/foreground/background） | 语义差异 |
| permission_mode 只是空壳 | 字段已添加并在 AgentDefinition 中存储，但从没有被 PermissionPipeline 消费 | 设置 permission_mode: plan 不会生效 |
| tools 字段缺失 `Agent(agent_type)` 解析 | Agent 的 tools 字段应该能限制 `Agent(researcher, worker)` | 无法在 agent 级别限制子代理类型 |
| agent_factory.py 遗留 _MODE_MAP fallback | 新的 registry.get() 优先查找，但 fallback 到硬编码映射 | 自定义 primary agent 如果名字不在映射中，通过 fallback 查找 build/plan 会得到错误的 spec |

---

## 三、AgentDefinition 字段完整覆盖

### ✅ 已正确实现的基础字段

| 字段 | 来源 | 说明 |
|------|------|------|
| name | frontmatter | ✅ |
| description | frontmatter | ✅ |
| intent | frontmatter (required) | ✅ |
| tools | frontmatter | ✅ |
| disallowed_tools | frontmatter | ✅ |
| allowedSubagents | frontmatter | ✅ via DelegationPolicy |
| model | frontmatter | ✅ CC-aligned (string) |
| maxTurns / max_turns | frontmatter | ✅ |
| maxTokens / max_tokens | frontmatter | ✅ |
| workspace_mode (isolation) | frontmatter | ✅ |
| visibility | frontmatter | ✅ |
| delegation_scope | frontmatter | ✅ |
| system_prompt | frontmatter body | ✅ |
| required_tools | frontmatter | ✅ forge-agent 扩展 |
| completion_requires | frontmatter | ✅ forge-agent 扩展 |

### ❌ 未实现（已解析但未消费）

| 字段 | 存储位置 | 应该消费的位置 | 现状 |
|------|----------|---------------|------|
| permission_mode | AgentDefinition.permission_mode | PermissionPipeline / PhasePolicy | 已解析，未接入 |
| mcp_servers | AgentDefinition.mcp_servers | MCPToolIntegration | 已解析，未接入 |
| skills | AgentDefinition.skills | SkillRegistry | 已解析，未接入 |
| memory | AgentDefinition.memory | MemoryStore | 已解析，未接入 |
| hooks | AgentDefinition.hooks | HookDispatcher | 已解析，未接入 |
| background | AgentDefinition.background | ExecutionPlacement | 已解析，未接入 spawn_agent 默认值 |
| effort | AgentDefinition.effort | LLMBackend | 已解析，未接入 |
| color | AgentDefinition.color | CLI renderer | 已解析，未接入 |
| initial_prompt | AgentDefinition.initial_prompt | --agent 启动时的首条消息 | 已解析，未接入 |

---

## 四、硬编码消除（Batch 9 已做）

### ✅ 已消除的硬编码

| 位置 | 旧代码 | 新代码 |
|------|--------|--------|
| agent_factory.py | `_MODE_MAP["plan"]` → `for_plan` | `spec.intent is ANALYSIS` |
| agent_factory.py | `if _resolved == "plan":` validate | `if spec.permission_mode == "plan":` |
| runtime_prompt_builder.py | `if spec.name == "plan":` | `if spec.permission_mode == "plan":` |
| runtime.py | `if spec.name not in {"build","general"}:` | `if spec.intent is EDIT:` |
| task_tool.py | `if subagent_type == "code-reviewer":` | `if definition.required_tools:` |
| agent_definition.py | `AgentModel("sonnet") raises` | string 接受任意 model |

### ⚠️ 残留问题

| 位置 | 问题 | 建议 |
|------|------|------|
| agent_factory.py:115-118 | `_MODE_MAP` fallback 仍在，映射 "auto"→"build", "react"→"build" | 保持兼容性，但应考虑通过 registry 查询 |
| entry/cli.py:504-506 | 我们现在用 `AgentRegistryV2(project_dir)` 构建，但构造时如果 .md 文件有错误会抛 AgentDefinitionError | 已 catch，但 registry 可能部分加载 |

---

## 五、总结

```
✅ 已正确实现:       ~30 项（工具命名、参数、子代理状态机、权限规则）
❌ 未实现（需新增）:  6 项（--agents CLI、嵌套 spawn、Agent() deny 语法、
                      Agent() tools 限制、Agent 级别 hook/MCP/color/memory/
                      initial_prompt/skills/effort/permission_mode 接入）
⚠️ 已实现但错误:      4 项（MCP intent 判断、background 语义、permission_mode
                      空壳、_MODE_MAP fallback 误导）
```

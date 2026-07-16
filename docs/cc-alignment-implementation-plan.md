# CC 对齐 — 未实现 & 错误实现修正方案

> 依据来源：Claude Code 官方文档 [sub-agents](https://code.claude.com/docs/en/sub-agents)、[permissions](https://code.claude.com/docs/en/permissions)、[hooks](https://code.claude.com/docs/en/hooks)、[memory](https://code.claude.com/docs/en/memory)
> 调研日期：2026-07-16

---

## 批次划分总览

| 批次 | 主题 | 文件数 | 优先级 |
|------|------|--------|--------|
| C1 | permission_mode 接入 PermissionPipeline | 2 | 🔴 高 |
| C2 | mcp_servers 接入 + MCP intent 修正 | 2 | 🔴 高 |
| C3 | background 语义修正 + initial_prompt 接入 | 3 | 🟡 中 |
| C4 | hooks 接入 HookDispatcher | 2 | 🟡 中 |
| C5 | skills 预加载 + memory 接入 | 2 | 🟡 中 |
| C6 | effort + color 接入 | 2 | 🟢 低 |
| C7 | Agent() deny 语法 + Agent(agent_type) 限制 + --agents CLI | 3 | 🟢 低 |

---

## Batch C1: permission_mode 接入 PermissionPipeline

### 依据（CC 官方文档）

> `permissionMode`: `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`, `manual`
> Controls how the subagent handles permission prompts. Subagents inherit the permission context from the main conversation and can override the mode.

### 现状问题

`AgentDefinition.permission_mode` 已经解析并存储，但 `PolicyAwareToolRegistry` / `PermissionPipeline` 完全不消费它。设置 `permission_mode: plan` 的 agent 仍然能调用 Write/Edit。

### 修改方案

#### 文件 1: `agent/v2/registry_builder.py`

在 `build_registry_for_session()` 中，将 `spec.permission_mode` 传递给 `PolicyAwareToolRegistry`:

```python
# 在 wrapped = PolicyAwareToolRegistry(...) 之前
if spec.permission_mode:
    phase_policy = PhasePolicy(
        allowed_tools=frozenset(registry.tool_names),
        permission_mode=spec.permission_mode,  # 新增参数
    )
```

#### 文件 2: `agent/policy.py` — PhasePolicy / PolicyAwareToolRegistry

```python
@dataclass(frozen=True)
class PhasePolicy:
    allowed_tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    permission_mode: str = ""  # 新增

    def is_tool_allowed(self, tool_name: str) -> bool:
        if self.permission_mode == "plan" and tool_name in {"Write", "Edit", "Bash"}:
            return False  # plan mode: no write tools
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False
        return True
```

在 `PolicyAwareToolRegistry.execute_tool()` 中，在调用 `PermissionPipeline` 前先检查 `phase_policy.permission_mode`：
- `"plan"` → 拒绝所有 Write/Edit/Bash
- `"acceptEdits"` → 自动批准 Write/Edit（但 Bash 仍需确认）
- `"dontAsk"` → 对非 allowlist 工具静默拒绝
- `"bypassPermissions"` → 跳过 pipeline
- `""` 或 `"default"` → 现有行为不变

### 测试

```python
# 验证 permission_mode=plan 拒绝 Write
spec = AgentDefinition(name="plan-agent", ..., permission_mode="plan")
registry = build_registry_for_session(spec, session, ...)
result = registry.execute_tool("Write", {"path": "test.txt", "content": "x"})
assert not result.success  # plan mode blocks writes
```

---

## Batch C2: mcp_servers 接入 + MCP intent 修正

### 依据（CC 官方文档）

> `mcpServers`: MCP servers available to this subagent. Each entry is either a server name referencing an already-configured server (e.g., "slack") or an inline definition.
> Inline definitions: connected when the subagent starts, disconnected when it finishes.
> String references: share the parent session's connection.

### 现状问题

两个问题：
1. `_mcp_tool_names_for_spec` 用 `spec.intent is EDIT` 判断——但 CC 的 MCP 应该是通过 `tools` 或 `mcpServers` 字段声明的
2. `spec.mcp_servers` 已解析但从未传递到 `MCPToolIntegration`

### 修改方案

#### 文件 1: `agent/v2/runtime.py` — `_mcp_tool_names_for_spec`

```python
def _mcp_tool_names_for_spec(self, spec: AgentDefinition) -> frozenset[str]:
    if self._mcp_integration is None:
        return frozenset()
    # CC-aligned: MCP tools come from mcpServers frontmatter declaration,
    # NOT from intent. If spec has mcp_servers, resolve them.
    if spec.mcp_servers:
        # Return tools from the named servers
        server_tools = set()
        for entry in spec.mcp_servers:
            if isinstance(entry, str):
                # Reference by name — tools from existing server
                tools = self._mcp_integration.server_tools.get(entry, [])
                server_tools.update(tools)
            elif isinstance(entry, dict):
                # Inline definition — tools would be registered at connect time
                pass
        return frozenset(server_tools)
    # Fallback for backward compatibility: EDIT-intent agents get MCP
    if spec.intent is TaskIntent.EDIT:
        raw_names = getattr(self._mcp_integration, "tool_names", frozenset())
        from agent.capability_registry import CapabilityState
        return frozenset(
            n for n in raw_names
            if self._capability_registry.state_for(n) is CapabilityState.AVAILABLE
        )
    return frozenset()
```

#### 文件 2: `agent/v2/mcp_integration.py` — 支持 agent-scoped 连接

在 `MCPToolIntegration` 中新增方法：

```python
def connect_agent_servers(self, spec: AgentDefinition) -> list[str]:
    """Connect MCP servers declared in an agent's mcpServers frontmatter.
    
    Returns list of newly registered tool names.
    """
    if not spec.mcp_servers:
        return []
    new_tools = []
    for entry in spec.mcp_servers:
        if isinstance(entry, dict):
            for name, config in entry.items():
                self._connect_server(name, config)
                new_tools.extend(self.server_tools.get(name, []))
    return new_tools

def disconnect_agent_servers(self, spec: AgentDefinition) -> None:
    """Disconnect agent-scoped MCP servers when agent finishes."""
    if not spec.mcp_servers:
        return
    for entry in spec.mcp_servers:
        if isinstance(entry, dict):
            for name in entry:
                self._disconnect_server(name)
```

在 `SessionRuntime.spawn_agent()` 中，在 child 启动时调用 `connect_agent_servers()`，完成后调用 `disconnect_agent_servers()`。

### 测试

```python
# 验证 mcp_servers 声明的 agent 能获取 MCP 工具
spec = AgentDefinition(name="db-agent", ..., mcp_servers=("db-server",))
tools = runtime._mcp_tool_names_for_spec(spec)
assert "db_query" in tools  # db-server 的工具
```

---

## Batch C3: background 语义修正 + initial_prompt 接入

### 依据（CC 官方文档）

> `background`: Set to `true` to always run this subagent as a background task. When unset, Claude chooses (default: background since v2.1.198).
> `initialPrompt`: Auto-submitted as the first user turn when this agent runs as the main session agent (via `--agent` or the `agent` setting). Prepended to any user-provided prompt.

### 现状问题

1. `background` 存为 `bool`，但我们的 `ExecutionPlacement` 有三个值（AUTO/FOREGROUND/BACKGROUND），且 `AgentSpawnRequest` 不接受 definition 的 background 作为默认值
2. `initial_prompt` 已解析但从未在 `entry/cli.py` 的 `--agent` 启动时注入

### 修改方案

#### 文件 1: `agent/v2/runtime.py` — `spawn_agent()`

在创建 `AgentSpawnRequest.named()` 时，用 definition.background 作为默认 `execution_placement`:

```python
if request.definition and request.definition.background and request.execution_placement is ExecutionPlacement.AUTO:
    # 修正 execution_placement 为 BACKGROUND
    object.__setattr__(request, "execution_placement", ExecutionPlacement.BACKGROUND)
```

或在 `AgentSpawnRequest.named()` 中处理：

```python
@classmethod
def named(cls, *, definition, description, prompt,
          execution_placement=ExecutionPlacement.AUTO):
    if definition.background and execution_placement is ExecutionPlacement.AUTO:
        execution_placement = ExecutionPlacement.BACKGROUND
    ...
```

#### 文件 2: `agent/v2/models.py` — `AgentSpawnRequest.named()`

直接将 background 映射到 execution_placement：

```python
@classmethod
def named(cls, *, definition, description, prompt,
          execution_placement=None):
    if execution_placement is None:
        execution_placement = (
            ExecutionPlacement.BACKGROUND
            if definition.background
            else ExecutionPlacement.FOREGROUND
        )
    ...
```

#### 文件 3: `entry/cli.py` — initial_prompt 注入

在 `run()` 命令中，通过 `--agent` 启动时，查找到 definition 的 initial_prompt 并注入：

```python
# 在 AgentRegistryV2 查找之后，_run_v2_mode 之前
spec = _agent_registry.get(agent_name)
if spec.initial_prompt:
    # 注入到 description 前面
    description = f"{spec.initial_prompt}\n\n{description}"
```

### 测试

```python
# background: true → spawn with BACKGROUND placement
spec = AgentDefinition(name="bg-agent", ..., background=True)
req = AgentSpawnRequest.named(definition=spec, description="x", prompt="x")
assert req.execution_placement is ExecutionPlacement.BACKGROUND
```

---

## Batch C4: hooks 接入 HookDispatcher

### 依据（CC 官方文档）

> `hooks`: Lifecycle hooks scoped to this subagent. Supported events: PreToolUse, PostToolUse, Stop.
> Frontmatter hooks fire when the agent is spawned as a subagent through the Agent tool or an @-mention.

### 现状问题

`hooks` 字段已解析为 `tuple[dict, ...]` 存储在 `AgentDefinition.hooks` 中，但从未传递给 `HookDispatcher`。全局 hook 系统工作正常（在 `hitl/settings_loader.py` 中配置），但 agent 级别的 hooks 从未注册。

### 修改方案

#### 文件 1: `agent/v2/runtime.py` — agent 级别 hooks 注册

在 `_build_registry_for_session()` 或 `spawn_agent()` 中，将 spec.hooks 注册到 `self._hook_dispatcher`:

```python
def _register_agent_hooks(self, spec: AgentDefinition) -> None:
    """Register agent-scoped lifecycle hooks from frontmatter."""
    if not spec.hooks or self._hook_dispatcher is None:
        return
    for hook_group in spec.hooks:
        # hook_group format: {"PreToolUse": [{"matcher": "Bash", ...}], ...}
        for event_name, hooks_list in hook_group.items():
            if event_name in ("PreToolUse", "PostToolUse", "SubagentStop"):
                self._hook_dispatcher.register(event_name, hooks_list)
```

在 agent 执行完成后清理：

```python
def _unregister_agent_hooks(self, spec: AgentDefinition) -> None:
    if not spec.hooks or self._hook_dispatcher is None:
        return
    for hook_group in spec.hooks:
        for event_name in hook_group:
            self._hook_dispatcher.unregister(event_name)
```

#### 文件 2: `agent/hook_dispatcher.py` — 支持动态注册/注销

新增 `register()` 和 `unregister()` 方法：

```python
def register(self, event_name: str, hooks: list[dict]) -> None:
    """Dynamically add hooks for an event."""
    if event_name not in self._hooks:
        self._hooks[event_name] = []
    self._hooks[event_name].extend(hooks)

def unregister(self, event_name: str, hooks: list[dict] | None = None) -> None:
    """Remove dynamically added hooks."""
    if hooks is None:
        self._hooks.pop(event_name, None)
    else:
        for h in hooks:
            try:
                self._hooks[event_name].remove(h)
            except (KeyError, ValueError):
                pass
```

### 测试

```python
spec = AgentDefinition(name="hooked", ..., hooks=(
    {"PreToolUse": [{"matcher": "Bash", "hooks": [...]}]},
))
runtime._register_agent_hooks(spec)
# 验证 hooks 已被注册
```

---

## Batch C5: skills 预加载 + memory 接入

### 依据（CC 官方文档）

> `skills`: Skills to preload into the subagent's context at startup. The full skill content is injected, not only the description.
> `memory`: Persistent memory scope: `user`, `project`, or `local`. Enables cross-session learning.

### 现状问题

`skills` 和 `memory` 已解析但从未使用。

### 修改方案

#### 文件 1: `agent/v2/runtime.py` 或 `agent/v2/runtime_prompt_builder.py` — skills 预加载

在 `build_runtime_messages()` 中，如果 spec.skills 非空，加载对应的 SKILL.md 内容：

```python
if spec.skills:
    from pathlib import Path
    skill_contents = []
    for skill_name in spec.skills:
        # 查找 skill 文件
        for base in (Path(self._project_root) / ".forge-agent" / "skills",
                     Path.home() / ".forge-agent" / "skills"):
            skill_path = base / f"{skill_name}.md" / "SKILL.md"
            if skill_path.exists():
                skill_contents.append(skill_path.read_text(encoding="utf-8"))
                break
    if skill_contents:
        messages.append(LLMMessage(
            role="user",
            content="[PRELOADED SKILLS]\n" + "\n---\n".join(skill_contents)
        ))
```

#### 文件 2: `agent/v2/runtime_prompt_builder.py` — memory 注入

在系统提示词中注入 memory 上下文：

```python
if spec.memory:
    memory_dir = _memory_path_for(spec)
    memory_file = memory_dir / "MEMORY.md"
    if memory_file.exists():
        memory_content = memory_file.read_text(encoding="utf-8")[:25_000]  # 25KB cap
        messages.append(LLMMessage(
            role="user",
            content=f"[AGENT MEMORY]\n{memory_content}\n\n"
                    "Review your memory above for patterns and decisions "
                    "from previous sessions. Update it after completing work."
        ))
```

辅助函数：

```python
def _memory_path_for(spec: AgentDefinition) -> Path:
    scope = spec.memory  # "user", "project", "local"
    if scope == "user":
        return Path.home() / ".forge-agent" / "agent-memory" / spec.name
    elif scope == "project":
        return Path(self._project_root) / ".forge-agent" / "agent-memory" / spec.name
    else:  # "local"
        return Path(self._project_root) / ".forge-agent" / "agent-memory-local" / spec.name
```

---

## Batch C6: effort + color 接入

### 依据（CC 官方文档）

> `effort`: Effort level when this subagent is active. Options: `low`, `medium`, `high`, `xhigh`, `max`.
> `color`: Display color for the subagent in the task list and transcript. Accepts `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, or `cyan`.

### 现状问题

两者都已解析但未消费。

### 修改方案

#### 文件 1: `agent/v2/models.py` — effort 校验增强

已经做了，但需确保值能传递到 LLM backend：

```python
# AgentDefinition.__post_init__ 中已经校验:
if self.effort and self.effort not in {"low", "medium", "high", "xhigh", "max"}:
    raise ValueError(...)
```

#### 文件 1b: `agent/core.py` 或 `llm/base.py` — effort 传递

在 `AgentConfig` 中添加 effort 字段：

```python
@dataclass
class AgentConfig:
    ...
    effort: str = ""  # low/medium/high/xhigh/max
```

在 `AgentFactory._build_agent_config()` 中设置：

```python
cfg.effort = spec.effort  # 从 AgentDefinition 传递
```

在 `LLMBackend.complete()` 中传递给 API：

```python
kwargs = {}
if effort:
    kwargs["reasoning_effort"] = effort
```

#### 文件 2: `entry/renderer.py` 或 `entry/_terminal.py` — color 显示

在 `_print_v2_result()` 或 renderer 中使用 spec.color：

```python
_color_map = {
    "red": "red", "blue": "blue", "green": "green",
    "yellow": "yellow", "purple": "magenta", "orange": "yellow",
    "pink": "magenta", "cyan": "cyan",
}
agent_color = _color_map.get(spec.color, "dim")
click.echo(getattr(click, agent_color, dim)(f"  Agent   : {spec.name}"))
```

---

## Batch C7: Agent() deny 语法 + Agent(agent_type) 限制 + --agents CLI

### 依据（CC 官方文档）

> Permission deny: `"Agent(Explore)"` — blocks spawning specific subagent type while leaving Agent tool available.
> Tools allowlist: `tools: Agent(worker, researcher)` — only those types can be spawned.
> `--agents` CLI flag: accepts JSON with same frontmatter fields for session-only definitions.

### 现状问题

三项都未实现。

### 修改方案

#### 文件 1: `hitl/permission_rule.py` — Agent(name) 匹配

在 `_extract_match_target()` 中添加 Agent 工具的支持：

```python
def _extract_match_target(tool_name: str, params: dict[str, Any]) -> str:
    name = tool_name.lower()
    ...
    if name == "agent":
        return params.get("subagent_type", "") or params.get("agent_name", "")
    ...
```

这样 `PermissionRule.parse("Agent(explore)", tier=DENY)` 会：
- `tool_name = "agent"`
- `pattern = "explore"`
- 当 Agent 工具调用 `subagent_type="explore"` 时，`_extract_match_target` 返回 `"explore"`
- `_glob_match("explore", "explore")` → 匹配 → deny

#### 文件 2: `agent/v2/agent_definition.py` + `agent/v2/models.py` — Agent(agent_type) 解析

在 `_parse_tool_list()` 中解析 `Agent(worker,researcher)` 语法：

```python
def _parse_tool_list(value: Any) -> frozenset[str]:
    ...
    # 基础工具名保持原样
    # Agent(worker,researcher) → 存储为 "Agent" 并提取子代理限制
    ...
```

在 `DelegationPolicy` 中添加从 tools 字段推导受限制子代理的逻辑：

```python
@staticmethod
def from_tools(tools: frozenset[str]) -> "DelegationPolicy":
    """Extract delegation allowlist from tools containing Agent(name) syntax."""
    for tool in tools:
        if tool.startswith("Agent(") and tool.endswith(")"):
            names = frozenset(
                n.strip() for n in tool[6:-1].split(",") if n.strip()
            )
            if names:
                return DelegationPolicy.allowlist(names)
    return DelegationPolicy.disabled()
```

#### 文件 3: `entry/cli.py` — `--agents` CLI 支持

新增 `--agents` 参数：

```python
@click.option("--agents", "agents_json", default=None,
              help="JSON string with session-only agent definitions")
```

在 `run()` 中解析并注入到 AgentRegistryV2：

```python
if agents_json:
    import json
    session_agents = json.loads(agents_json)
    for name, config in session_agents.items():
        # 构造临时 AgentDefinition 并注入 registry
        from agent.v2.models import AgentDefinition, TaskIntent, AgentKind
        from agent.v2.agent_definition import _parse_tool_list
        agent = AgentDefinition(
            name=name,
            description=config.get("description", ""),
            intent=TaskIntent(config.get("intent", "edit")),
            tools=_parse_tool_list(config.get("tools", "")),
            ...
        )
        _agent_registry._agents[name] = agent
```

---

## 执行顺序

```
C1 (permission_mode)  →  C2 (mcp_servers)  →  C3 (background+initial)
  →  C4 (hooks)  →  C5 (skills+memory)  →  C6 (effort+color)  →  C7 (deny+tools+CLI)
```

每个批次完成后：
```bash
pytest tests/test_plan_approval.py tests/test_plan_prompt_contract.py tests/test_cli_v2_orchestration.py -q
pytest tests/test_v2_runtime.py::test_build_subagent_prompt_includes_protocol ... -q
python tests/manual/verify_cc_alignment.py
git commit -m "Batch C<N>: <description>"
```

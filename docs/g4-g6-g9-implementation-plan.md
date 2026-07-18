# G4 + G6 + G9: 从根本实现 — 完整设计与实施计划

> 基于 2026-07-19 Claude Code 源码逆向 + 官方文档 + GitHub issues
> **核心原则: 不使用补丁方式，必须从根本上实现与 CC 相同的流程运转**

---

## G4: deny→ask→allow 优先级 (CC Phase 1 对齐)

### 差距分析

**CC 的真实评估顺序 (Phase 1):**

```
Step 1a: Deny Rules    → 匹配则立即阻止 (bypass-immune)
Step 1b: Ask Rules     → 匹配则始终提示 (bypass-immune, 即使 allow 规则也匹配)
Step 1c: Tool 自身检查  → tool.checkPermissions()
Step 1d-1g: 硬安全门   → requiresUserInteraction, 受保护路径

Step 2a: Permission Mode → bypassPermissions/acceptEdits/plan/dontAsk
Step 2b: Allow Rules     → 匹配则自动批准
Step 3:   canUseTool     → 默认回退到交互式确认
```

**关键设计:**
- Ask 规则在 **Phase 1** (bypass-immune)，Allow 规则在 **Phase 2** (可被 bypass)
- **评估顺序是固定的 deny→ask→allow**，不因来源优先级而改变
- Ask 规则匹配后**始终**走到 `canUseTool` 回调，即使 `bypassPermissions` 也如此
- 所有 8 个来源的规则按**行为类型**（deny/ask/allow）合并，然后按行为优先级评估

**我们的当前实现 (Layer 3):**

```
deny → session_allow → allow → ask → None
```

来源: [wuwangzhang1216 deep analysis](https://github.com/wuwangzhang1216/claude-code-source-all-in-one/blob/main/claude-code-deep-analysis/05-permission-system.en.md), [openedclaude Chapter 7](https://openedclaude.github.io/claude-reviews-claude/chapters/07-permission-pipeline), [GitHub Issue #25345](https://github.com/anthropics/claude-code/issues/25345)

### 根本性改造

当前的 `_layer3_rules()` 将 ask 放在 allow 之后，且 ask 匹配时调用 `_layer6_callback(force_interactive=True)` 作为补偿。CC 的做法是 ask 规则在管线中**先于** allow 规则评估，且 ask 命中后标记为 "需要交互确认"，然后**继续到 Phase 2**（allow 规则不检查，但 permission mode 检查）。

问题是：我们当前的 Ask 规则匹配后**直接跳 Layer 6**，完全跳过了 Layer 4 (permission mode)。这意味着 Ask 规则在 `bypassPermissions` 模式下仍然弹卡（正确），但同时也跳过了 `plan` 和 `dontAsk` 的逻辑。

**CC 的正确行为:**
- Ask 命中 → 标记 `force_interactive=True` → 继续检查 permission mode:
  - `bypassPermissions`: 仍然弹卡 (ask 是 bypass-immune) ✅ 我们正确
  - `plan`: Write/Edit 被拒绝 (plan 是只读模式) — 我们**未实现**，Ask 跳过了 Layer 4
  - `dontAsk`: deny (dontAsk 永不提示) — 我们**未实现**，Ask 跳过了 Layer 4
  - `acceptEdits`: 仍弹卡 — 我们正确
  - `default`: 仍弹卡 — 我们正确

### 实施: 重新设计管线评估

**Step 1: 重排 `_layer3_rules` 优先级**

**文件:** `hitl/pipeline.py:_layer3_rules`

```python
def _layer3_rules(self, tool_name, params):
    """
    CC-aligned Phase 1: deny → ask → allow.
    
    Ask rules are bypass-immune (Phase 1), allow rules are not (Phase 2).
    Returns (tier, matched_rule_raw) tuple, or (None, None).
    """
    # 1. Deny rules (highest — absolute safety floor, kill-switch)
    for rule in self._deny_rules:
        if rule.matches(tool_name, params):
            return (PermissionRuleTier.DENY, rule.raw)
    
    # 2. Ask rules (Phase 1 — bypass-immune, always prompts)
    #    Even if an allow rule also matches, ask takes precedence.
    for rule in self._ask_rules:
        if rule.matches(tool_name, params):
            return (PermissionRuleTier.ASK, rule.raw)
    
    # 3. Allow rules (Phase 2 — can be overridden by permission mode)
    for rule in self._allow_rules:
        if rule.matches(tool_name, params):
            return (PermissionRuleTier.ALLOW, rule.raw)
    
    # 4. Session rules (Always Allow — highest priority allow)
    for rule in self._session_rules:
        if rule.matches(tool_name, params):
            return (PermissionRuleTier.ALLOW, rule.raw)
    
    return (None, None)
```

**Step 2: 更新 `check()` 中的 Ask 处理**

Ask 命中后**不再直接跳 Layer 6**，而是标记 `_force_interactive = True` 并**继续**到 Layer 4：

```python
tier, _matched_raw = self._layer3_rules(tool_name, params)

if tier is PermissionRuleTier.DENY:
    # ... existing deny logic (unchanged) ...
    return ...

if tier is PermissionRuleTier.ALLOW:
    # Allow from Layer 3 — subject to Layer 4 permission mode override
    # (e.g. plan mode may deny even if allow rule matches)
    pass  # fall through to Layer 4

if tier is PermissionRuleTier.ASK:
    # Ask rule matched — force interactive confirmation regardless of mode
    self._force_interactive = True
    self._decision_reason = f"Matched ask rule: {_matched_raw}"
    # Fall through to Layer 4 for mode-specific handling
    # (plan/dontAsk may override the ask, bypass/acceptEdits/default will preserve it)

# tier is None → no rule matched → continue to Layer 4

# Step 4: Permission Mode
mode_result = self._layer4_permission_mode(tool_name, params)
if mode_result is not None:
    if self._force_interactive and mode_result.decision is PermissionDecision.ALLOW:
        # bypassPermissions/acceptEdits tried to auto-allow, but ask rule
        # is bypass-immune — force interactive
        mode_result = None  # fall through to Layer 6
    else:
        return mode_result

# ... Layer 4.5 and Layer 5 unchanged ...

# Step 6: callback
result = self._layer6_callback(
    tool_name, params, thought,
    force_interactive=self._force_interactive,
    decision_reason=self._decision_reason,
)
```

**Step 3: 更新 `_layer4_permission_mode`**

在 `plan` 和 `dontAsk` 模式下处理 `_force_interactive`：

```python
if mode == "plan":
    # Plan mode: Write/Edit/Bash → DENY even if ask rule matched
    if tool_name in {"Write", "Edit", "Bash"}:
        return PermissionResult(DENY, ...)
    return None

if mode == "dontAsk":
    # dontAsk: if _force_interactive (ask rule matched), DENY (never prompts)
    if self._force_interactive:
        return PermissionResult(DENY, 
            reason="dontAsk mode: ask rule blocked (non-interactive)")
    # ... existing dontAsk logic ...
```

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | `_layer3_rules` 重排 + `check()` 流程重构 + `_layer4_permission_mode` 更新 |

---

## G6: MCP 支持

### 差距分析

CC 的 MCP 集成是完整的 Model Context Protocol 客户端实现，核心组件：

| 组件 | CC 实现 | 我们 |
|------|---------|------|
| MCP 客户端 | `services/mcp/client.ts` (~3348 行) | ❌ 无 |
| 传输层 | stdio, HTTP, SSE, WebSocket, InProcess | ❌ 无 |
| 工具发现 | `fetchToolsForClient()` + LRU 缓存 | ❌ 无 |
| 工具命名 | `mcp__{server}__{tool}` 格式 | ❌ 无 |
| 权限格式 | `mcp__server__*` 通配符 | ❌ 无 |
| 懒加载 | 工具 schema 按需加载 (~85% token 节省) | ❌ 无 |
| requiresUserInteraction | MCP 工具标记 + 权限管线集成 | ✅ 已有 P1-6 基础设施 |
| 配置来源 | 7 个 scope (local/user/project/enterprise/...) | ❌ 无 |

来源: [wuwangzhang1216 MCP analysis](https://github.com/wuwangzhang1216/claude-code-source-all-in-one/blob/main/claude-code-deep-analysis/12-mcp-integration.en.md), [DeepWiki MCP Architecture](https://deepwiki.com/FlorianBruniaux/claude-code-ultimate-guide/6.1-mcp-architecture-and-tool-search)

### 根本性设计

MCP 支持需要从**协议层**开始实现，不能打补丁。核心架构分为 4 层：

```
┌─────────────────────────────────────────┐
│          Permission Layer               │
│  mcp__server__tool 格式权限规则          │
│  requiresUserInteraction 集成            │
├─────────────────────────────────────────┤
│          Tool Registry Layer            │
│  MCP 工具注册到 ToolRegistry             │
│  懒加载 schema (名→按需获取完整 schema)    │
├─────────────────────────────────────────┤
│          Transport Layer                │
│  StdioTransport / HttpTransport          │
│  连接管理 (memoize, reconnect)            │
├─────────────────────────────────────────┤
│          Protocol Layer                 │
│  JSON-RPC 2.0 (initialize, tools/list,   │
│  tools/call, notifications)              │
└─────────────────────────────────────────┘
```

### 实施步骤

**Step 1: Protocol Layer — JSON-RPC 2.0 基础**

新建 `mcp/protocol.py`:

```python
# mcp/protocol.py
"""MCP JSON-RPC 2.0 protocol implementation."""

@dataclass
class JsonRpcRequest:
    jsonrpc: str = "2.0"
    id: int = 0
    method: str = ""
    params: dict = field(default_factory=dict)

@dataclass  
class JsonRpcResponse:
    jsonrpc: str = "2.0"
    id: int = 0
    result: Any = None
    error: dict | None = None

class McpClient:
    """Base MCP client with JSON-RPC request/response handling."""
    
    def __init__(self, transport: "McpTransport"):
        self._transport = transport
        self._request_id = 0
        self._server_capabilities: dict = {}
    
    async def initialize(self) -> dict:
        """Send initialize request, return server capabilities."""
        ...
    
    async def list_tools(self) -> list[dict]:
        """Discover available tools via tools/list."""
        ...
    
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Invoke a tool via tools/call."""
        ...
```

**Step 2: Transport Layer**

新建 `mcp/transport.py`:

```python
# mcp/transport.py
"""MCP transport implementations."""

class McpTransport(ABC):
    """Abstract transport for MCP communication."""
    @abstractmethod
    async def send(self, message: bytes) -> None: ...
    @abstractmethod
    async def receive(self) -> bytes: ...

class StdioTransport(McpTransport):
    """Subprocess-based transport (spawns server as child process)."""
    def __init__(self, command: str, args: list[str]):
        self._process = None
    
    async def connect(self):
        self._process = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

class HttpTransport(McpTransport):
    """StreamableHTTP transport (POST + optional SSE)."""
    def __init__(self, url: str, headers: dict | None = None):
        self._url = url
        self._headers = headers
```

**Step 3: MCP Server Registry**

新建 `mcp/registry.py`:

```python
# mcp/registry.py
"""MCP server configuration registry."""

@dataclass
class McpServerConfig:
    name: str
    transport: str  # "stdio" | "http"
    command: str = ""       # for stdio
    args: list[str] = field(default_factory=list)
    url: str = ""           # for http
    headers: dict = field(default_factory=dict)
    enabled: bool = True

class McpRegistry:
    """Manage MCP server configurations and connections."""
    
    def __init__(self, project_root: str):
        self._servers: dict[str, McpServerConfig] = {}
        self._clients: dict[str, McpClient] = {}
        self._tools_cache: dict[str, list[dict]] = {}  # server → tools
        self._load_configs(project_root)
    
    def _load_configs(self, project_root: str):
        """Load from .mcp.json (project) and ~/.forge-agent/mcp.json (user)."""
        for config_path in [
            Path.home() / ".forge-agent" / "mcp.json",
            Path(project_root) / ".mcp.json",
        ]:
            if config_path.exists():
                data = json.loads(config_path.read_text())
                for name, cfg in data.get("mcpServers", {}).items():
                    self._servers[name] = McpServerConfig(name=name, **cfg)
    
    async def connect_all(self):
        """Connect to all enabled servers."""
        ...
    
    async def fetch_tools(self, server_name: str) -> list[dict]:
        """Fetch and cache tools from a server."""
        ...
    
    def get_all_tools(self) -> dict[str, list[dict]]:
        """Return all tools from all connected servers."""
        return self._tools_cache
```

**Step 4: MCP Tool 包装器**

新建 `tools/mcp_tool.py`:

```python
# tools/mcp_tool.py
"""MCP tool wrapper — registered in ToolRegistry as mcp__server__tool."""

class McpToolWrapper(BaseTool):
    """Wraps an MCP tool so it appears in ToolRegistry."""
    
    def __init__(self, server_name: str, tool_def: dict, mcp_client: McpClient):
        self._server = server_name
        self._tool_def = tool_def
        self._client = mcp_client
    
    @property
    def name(self) -> str:
        # CC format: mcp__{server}__{tool}
        raw_name = self._tool_def["name"]
        safe_server = re.sub(r"[^a-zA-Z0-9_-]", "_", self._server)
        safe_tool = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)
        return f"mcp__{safe_server}__{safe_tool}"
    
    @property
    def description(self) -> str:
        return self._tool_def.get("description", "")[:2048]  # CC truncation
    
    @property
    def parameters_schema(self) -> dict:
        return self._tool_def.get("inputSchema", {})
    
    @property
    def metadata(self):
        meta = ToolMetadata()
        # requiresUserInteraction from MCP annotations
        annotations = self._tool_def.get("annotations", {})
        if annotations.get("destructiveHint"):
            meta.requires_user_interaction = True
        return meta
    
    def execute(self, params: dict) -> ToolResult:
        """Delegate to MCP server via tools/call."""
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(
            self._client.call_tool(self._tool_def["name"], params)
        )
        return ToolResult(success=True, output=str(result))
```

**Step 5: 注册到 ToolRegistry**

修改 `entry/bootstrap/registry_factory.py`:

```python
def build_registry(..., mcp_registry: McpRegistry | None = None):
    registry = ToolRegistry(...)
    
    # Register MCP tools if available
    if mcp_registry is not None:
        for server_name, tools in mcp_registry.get_all_tools().items():
            for tool_def in tools:
                wrapper = McpToolWrapper(server_name, tool_def, 
                                       mcp_registry.get_client(server_name))
                registry.register(wrapper)
    
    return registry
```

**Step 6: 权限规则支持 `mcp__*` 格式**

在 `permission_rule.py` 的 glob 匹配中，`mcp__server__tool` 格式的工具名已被现有机制支持（tool_name 字段匹配）。需要确保 `_extract_match_target` 能处理 MCP 工具的参数。

**Step 7: 懒加载 (后续优化)**

CC 的懒加载机制：启动时只加载工具名，完整 schema 按需获取。可在 Phase 2 实现。

### 涉及文件

| 文件 | 内容 |
|------|------|
| `mcp/protocol.py` | **NEW** — JSON-RPC 2.0 客户端 |
| `mcp/transport.py` | **NEW** — Stdio + HTTP 传输 |
| `mcp/registry.py` | **NEW** — 服务器配置 + 连接管理 |
| `tools/mcp_tool.py` | **NEW** — MCP 工具包装器 |
| `entry/bootstrap/registry_factory.py` | 注册 MCP 工具 |
| `server/services/agent_service.py` | MCP 初始化 |

---

## G9: 8 源规则层级

### 差距分析

**CC 的 8 源架构:**

每个来源维护**三组独立数组**: `alwaysAllowRules[source]`、`alwaysAskRules[source]`、`alwaysDenyRules[source]`。

规则**合并**（非覆盖）到全局列表中，然后按**行为类型** (deny→ask→allow) 评估，不是按来源优先级评估。

```
来源 (加载顺序, 后加载的 append 到列表末尾):
  userSettings  →  alwaysDenyRules[user], alwaysAskRules[user], alwaysAllowRules[user]
  projectSettings → alwaysDenyRules[project], alwaysAskRules[project], ...
  localSettings  →  ...
  flagSettings   →  ...
  policySettings →  ... (不可删除, allowManagedPermissionRulesOnly 可清除其他来源)
  cliArg         →  ...
  command        →  ...
  session        →  ...  (最高优先级, Always Allow 按钮)

评估 (不按来源, 按行为类型):
  for each deny rule (所有来源混合):  先到先得 → 阻止
  for each ask rule (所有来源混合):   先到先得 → 提示
  for each allow rule (所有来源混合): 先到先得 → 批准
```

来源: [wuwangzhang1216 deep analysis](https://github.com/wuwangzhang1216/claude-code-source-all-in-one/blob/main/claude-code-deep-analysis/05-permission-system.en.md), [CC permission docs](https://code.claude.com/docs/en/permissions), [dev.to rules analysis](https://dev.to/rulestack/claude-code-permission-rules-how-allow-deny-and-ask-actually-match-1bj7)

**我们的当前实现:**

```
来源:
  builtin defaults
  ~/.forge-agent/settings.json (user)
  .forge-agent/settings.json  (project)
  .forge-agent/settings.local.json (local)

数据结构: 三个独立列表 (_deny_rules, _ask_rules, _allow_rules)
加载方式: rules 按 tier 分类后 append 到对应列表
评估方式: deny → session_allow → allow → ask (G4 修正后: deny → ask → allow)
```

**差距:**
- 缺少 source 标记（无法知道规则来自哪个层级）
- 缺少 session 来源（Always Allow 规则当前在 `_session_rules` 中，未按 source 标记）
- 缺少 policySettings（企业策略）
- 缺少 cliArg + flagSettings（CLI 参数）
- 缺少 command 来源（Skill allowedTools）

### 根本性改造

**核心思想:** 不改变现有规则匹配性能，只添加 source 元数据。每个规则携带 `source` 字段，合并时按 source 优先级排序，评估时按行为类型排序。

**Step 1: 重新设计 PermissionRule**

**文件:** `hitl/permission_rule.py`

```python
@dataclass(frozen=True)
class PermissionRule:
    raw: str
    tool_name: str
    pattern: str | None
    tier: PermissionRuleTier
    source: str = "settings"  # 已有字段, 扩展值
    
    # CC-aligned source priority (lower = loaded first, lower priority)
    SOURCE_PRIORITY: ClassVar[dict[str, int]] = {
        "builtin": 1,
        "user": 2,
        "project": 3,
        "local": 4,
        "flag": 5,       # --settings CLI arg
        "policy": 6,     # enterprise managed
        "cli": 7,        # --allow/--deny CLI args
        "command": 8,    # Skill allowedTools
        "session": 9,    # Always Allow in session
    }
```

**Step 2: 统一规则加载器**

**文件:** `hitl/settings_loader.py`

```python
def load_permission_settings(project_path: str) -> tuple[list[PermissionRule], list]:
    """Load rules from ALL available sources, preserving source metadata.
    
    Loading order (CC-aligned, ascending priority):
    1. Builtin defaults                    (source="builtin", priority=1)
    2. ~/.forge-agent/settings.json        (source="user", priority=2)
    3. .forge-agent/settings.json          (source="project", priority=3)
    4. .forge-agent/settings.local.json    (source="local", priority=4)
    """
    rules: list[PermissionRule] = []
    
    # 1. Builtin
    rules.extend(_builtin_defaults())  # already marks source="builtin"
    
    # 2. User
    _append_from_file(Path.home() / ".forge-agent" / "settings.json", rules, "user")
    
    # 3. Project  
    _append_from_file(Path(project_path) / ".forge-agent" / "settings.json", rules, "project")
    
    # 4. Local
    _append_from_file(Path(project_path) / ".forge-agent" / "settings.local.json", rules, "local")
    
    return rules, []
```

**Step 3: Pipeline 规则注入 (保持 source 标记)**

**文件:** `hitl/pipeline.py`

```python
class PermissionPipeline:
    def __init__(self, *, rules=None, ...):
        self._deny_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        # 已按 source priority 排序的完整列表
        
        for r in (rules or []):
            if r.tier is PermissionRuleTier.DENY:
                self._deny_rules.append(r)
            elif r.tier is PermissionRuleTier.ASK:
                self._ask_rules.append(r)
            else:
                self._allow_rules.append(r)
    
    def add_session_rule(self, rule: PermissionRule):
        """Add an 'Always Allow' session rule (source='session', priority=9)."""
        object.__setattr__(rule, 'source', 'session')  # frozen dataclass
        self._session_rules.append(rule)
```

**Step 4: AgentService 加载逻辑 (对齐 CC 的 4 源 + session)**

**文件:** `server/services/agent_service.py`

当前 `_load_permission_rules()` 已按 user/project/local 加载。需要确保 source 标记正确传递：

```python
def _load_permission_rules(self):
    rules = []
    # 使用 settings_loader 的标准加载
    from hitl.settings_loader import load_permission_settings
    rules, _ = load_permission_settings(self.repo_path)
    
    # 也加载 user-level 设置
    _load_json_file(Path.home() / ".forge-agent" / "settings.json", rules, "user")
    _load_json_file(Path(self.repo_path) / ".forge-agent" / "settings.json", rules, "project") 
    _load_json_file(Path(self.repo_path) / ".forge-agent" / "settings.local.json", rules, "local")
    
    return rules
```

**Step 5: Session 规则持久化 (保持不变)**

`save_rule_to_settings()` 已实现持久化。Always Allow 规则同时添加到 `_session_rules`（内存）和写入 `.forge-agent/settings.local.json`（磁盘）。

### 实施优先级

对于 Web MVP，实际需要的来源：
- `builtin` (1) — 已实现
- `user` (2) — 已实现
- `project` (3) — 已实现
- `local` (4) — 已实现
- `session` (9) — 已实现 (_session_rules)

不需要的来源（CLI/企业专用）:
- `flag` (5) — CLI `--settings` 参数
- `policy` (6) — 企业托管策略
- `cli` (7) — `--allow/--deny` CLI 参数
- `command` (8) — Skill allowedTools (可在 Skills 系统中单独处理)

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/permission_rule.py` | SOURCE_PRIORITY 常量 + source 字段文档 |
| `hitl/settings_loader.py` | 统一加载逻辑，确保 source 标记 |
| `hitl/pipeline.py` | 规则排序按 source_priority + behavior |
| `server/services/agent_service.py` | 加载逻辑对齐 |

---

## 实施顺序

```
Batch G4: deny→ask→allow 优先级重构    (1 文件: hitl/pipeline.py)
Batch G9: 8 源规则层级 source 标记     (3 文件: permission_rule, settings_loader, pipeline)
Batch G6-Step1+2: MCP Protocol + Transport  (2 文件: mcp/protocol.py, mcp/transport.py)
Batch G6-Step3+4: MCP Registry + Tool Wrapper (2 文件: mcp/registry.py, tools/mcp_tool.py)
Batch G6-Step5+6: MCP 注册 + 权限集成  (2 文件: registry_factory, agent_service)
```

每批 ≤3 文件，commit 后全局反思。

---

## 反思

### G4 改造的关键
当前 Ask 规则直接跳 Layer 6 的做法是一个**快捷方式**，在简单场景下工作但不符合 CC 的 Phase 1/Phase 2 分层设计。改造成 `_force_interactive` 标记 + 继续管线流转后，`plan` 和 `dontAsk` 模式才能正确覆盖 Ask 规则。

### G6 改造的关键
MCP 不能以"添加一个 HTTP 端点"的方式打补丁。必须从 JSON-RPC 协议层 → Transport 层 → Registry 层 → Tool Wrapper 层逐层实现。当前仅实现 Stdio + HTTP 传输即可覆盖 90% 的 MCP 服务器。

### G9 改造的关键
CC 的规则系统本质是 **merge-by-behavior, evaluate-by-type**。来源优先级只影响加载顺序（append 顺序），不影响评估逻辑。我们已有的三列表结构 (_deny/_ask/_allow) 已经是正确的评估模型，只需要为每个规则添加 source 标记即可完整对齐。

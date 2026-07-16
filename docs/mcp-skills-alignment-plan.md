# MCP & Skills 对齐计划

> 参考来源：[Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Claude Code Skills](https://code.claude.com/docs/en/skills) · [Skills 前端参考](https://code.claude.com/docs/en/skills#frontmatter-reference)

---

## 一、MCP — 缺失功能（8 项）

### MCP-01: HTTP 传输 (`type: "http"`)

- **CC 参考**：[Option 1: Add a remote HTTP server](https://code.claude.com/docs/en/mcp#option-1-add-a-remote-http-server)
- **CC 行为**：`claude mcp add --transport http <name> <url>`。HTTP server 通过 JSON-RPC over HTTP 通信，支持 OAuth 和 Bearer token。
- **当前**：`runtime/mcp/config.py:_parse_server_config()` 遇到 `type != "stdio"` 返回 None。

**修改文件**：

| 文件 | 改动 |
|------|------|
| `runtime/mcp/types.py` | `MCPServerConfig` 加 `type`、`url`、`headers` 字段 |
| `runtime/mcp/config.py` | `_parse_server_config()` 分派 stdio/http/sse/ws；加 `_parse_http_config()` |
| `runtime/mcp/client.py` | 新增 `HttpMCPBridge` 类，`async def connect()` → `tools/list` |
| `runtime/mcp/sync_bridge.py` | `load_and_discover()` 按 type 创建不同 bridge |

**与 Runtime 集成**：
- `HttpMCPBridge` 需要 event loop → 复用 `SyncMCPToolManager._loop`
- 工具注册路径不变 → `mcp_tool_to_runtime_tool()` → `ToolRegistry`
- HTTP server 的工具名格式同 stdio：`mcp__<server>__<tool>`

**与 Hooks 集成**：
- `PreToolUse` hook 的 `tool_name` 对 HTTP MCP 工具同样匹配
- Hook matcher `mcp__*` 覆盖所有传输类型的 MCP 工具

```python
# 接口
class HttpMCPBridge:
    def __init__(self, config: MCPServerConfig) -> None: ...
    async def connect(self) -> list[MCPToolInfo]:
        """POST initialize, then tools/list. Returns discovered tools."""
    async def call_tool(self, tool_name: str, arguments: dict) -> MCPCallResult:
        """POST tools/call with JSON-RPC body."""
    async def close(self) -> None: ...
```

---

### MCP-02: SSE 传输 (`type: "sse"`)

- **CC 参考**：[Option 2: Add a remote SSE server](https://code.claude.com/docs/en/mcp#option-2-add-a-remote-sse-server)（已标记 deprecated）
- **CC 行为**：SSE 长连接接收 server→client 推送，HTTP POST 发送 client→server。

**修改文件**：同 MCP-01，`client.py` 加 `SseMCPBridge`。

---

### MCP-03: WebSocket 传输 (`type: "ws"`)

- **CC 参考**：[Option 4: Add a remote WebSocket server](https://code.claude.com/docs/en/mcp#option-4-add-a-remote-websocket-server)
- **CC 行为**：持久双向连接。`headersHelper` 可动态生成认证头。

**修改文件**：同 MCP-01，`client.py` 加 `WsMCPBridge`。

---

### MCP-04: 自动重连（exponential backoff）

- **CC 参考**：[Automatic reconnection](https://code.claude.com/docs/en/mcp#automatic-reconnection)
- **CC 行为**：HTTP/SSE 断连后最多 5 次重试，1s→2s→4s→8s→16s。5 次失败后标记 failed。

**修改文件**：`runtime/mcp/sync_bridge.py`

```python
class SyncMCPToolManager:
    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_BASE_DELAY = 1.0

    async def _reconnect(self, name: str, bridge: MCPToolBridge) -> bool:
        for i in range(self.MAX_RECONNECT_ATTEMPTS):
            await asyncio.sleep(self.RECONNECT_BASE_DELAY * (2 ** i))
            try:
                tools = await bridge.connect()
                self._refresh_tool_map(name, bridge, tools)
                return True
            except Exception: continue
        return False
```

**与 Runtime 集成**：重连成功后工具注册表需刷新 → `ToolRegistry.unregister()` 旧工具 + `register()` 新工具。如果主循环正在执行 MCP 工具调用时断连 → `ToolResult.from_error(ToolErrorType.ENVIRONMENT_UNAVAILABLE)`。

**与 Hooks 集成**：可加 `MCP_RECONNECT` 事件让 hook 感知重连状态。

---

### MCP-05: 动态工具更新 (`list_changed` 通知)

- **CC 参考**：[Dynamic tool updates](https://code.claude.com/docs/en/mcp#dynamic-tool-updates)
- **CC 行为**：server 发送 `notifications/tools/list_changed`，CC 自动刷新工具列表。

**修改文件**：`runtime/mcp/client.py`

```python
class MCPToolBridge:
    _on_tools_changed: Callable[[], None] | None = None

    async def _handle_list_changed(self, msg: dict) -> None:
        self._tools = await self.discover_tools()
        if self._on_tools_changed:
            self._on_tools_changed()
```

**与 Runtime 集成**：刷新后 `SyncMCPToolManager` 需要重建该 server 的 `_tool_map`。正在执行中的旧工具调用不受影响（已拿到 bridge ref）。

---

### MCP-06: CLI 管理命令

- **CC 参考**：[Managing your servers](https://code.claude.com/docs/en/mcp#managing-your-servers)
- **CC 行为**：`claude mcp add/list/get/remove`，支持 `--transport`、`--scope`、`--env` 标志。

**修改文件**：`entry/cli.py`

```python
@cli.group()
def mcp(): ...
@mcp.command("add")  # --transport, --scope, --env, -- name
@mcp.command("list")
@mcp.command("get")
@mcp.command("remove")
```

**与 Runtime 集成**：`mcp add` 后触发 `SyncMCPToolManager.load_and_discover([new_server])`。`mcp remove` 后 `unregister()` 该 server 的所有工具。

---

### MCP-07: `CLAUDE_PROJECT_DIR` 环境变量

- **CC 参考**：[Stdio server environment](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server)
- **CC 行为**：启动 stdio server 时自动设置 `CLAUDE_PROJECT_DIR=<project-root>`。

**修改文件**：`runtime/mcp/client.py`

```python
# MCPToolBridge.connect()
env = dict(os.environ)
if project_dir:
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
```

---

### MCP-08: Plugin-bundled MCP servers

- **CC 参考**：[Plugin-provided MCP servers](https://code.claude.com/docs/en/mcp#plugin-provided-mcp-servers)
- **CC 行为**：插件自带 `.mcp.json`，启用插件时 server 自动启动。工具名 `mcp__plugin_<plugin>_<server>__<tool>`。

**修改文件**：取决于 plugin 系统的设计。暂列低优先级。

---

## 二、MCP — 做错了（3 项）

### MCP-E1: 只支持 stdio 架构

- **错在哪**：`_parse_server_config()` 写死了 `if server_type != "stdio": return None`。`MCPServerConfig` 没有 `url`/`headers`/`type` 字段。
- **应改为**：多传输架构——`StdioBridge` / `HttpBridge` / `SseBridge` / `WsBridge` 四个类，`MCPServerConfig` 加字段，`_parse_server_config()` 按 type 分派。
- **接口已在 MCP-01/02/03 中定义**。

### MCP-E2: 配置路径不标准

- **错在哪**：`DEFAULT_GLOBAL_MCP_CONFIG = ~/.forge-agent/mcp.json`，CC 标准是 `~/.claude.json` + `.mcp.json`。
- **应改为**：
```python
DEFAULT_USER_CONFIG = Path.home() / ".claude.json"
DEFAULT_PROJECT_CONFIG = Path(".mcp.json")
_LEGACY_CONFIG = Path.home() / ".forge-agent" / "mcp.json"  # fallback only
```

### MCP-E3: 无 idle timeout

- **错在哪**：`_execute_once()` 只设了总超时。挂起的 MCP 调用（server 不响应也不断连）永久等待。
- **应改为**：
```python
# HTTP: 5min idle, stdio: 30min idle
DEFAULT_IDLE_TIMEOUT_HTTP = 300.0
DEFAULT_IDLE_TIMEOUT_STDIO = 1800.0

def _execute_once(self, ..., idle_timeout: float | None = None) -> MCPCallResult:
    idle = idle_timeout or self._idle_timeout_for(bridge)
    future = asyncio.run_coroutine_threadsafe(bridge.call_tool(...), self._loop)
    try:
        return future.result(timeout=idle)
    except TimeoutError:
        future.cancel()
        raise MCPToolTimeoutError(...)
```

---

## 三、Skills — 缺失功能（20 项）

### SK-01: `/skill-name` 斜杠命令

- **CC 参考**：[Test the skill](https://code.claude.com/docs/en/skills#getting-started)
- **CC 行为**：用户输入 `/summarize-changes` 或 `/deploy staging` → skill 内容直接注入当前上下文。

**修改文件**：

| 文件 | 改动 |
|------|------|
| `entry/chat.py` | `_handle_slash_command()` 解析 `/name args` |
| `skills/registry.py` | `load_and_render()` 接受 `arguments: str` |

**与 Runtime 集成**：skill 内容注入为 `LLMMessage(role="user", content=rendered_skill)`，追加到 `shared_history`。下一轮 LLM 就能看到。不走 tool_use 往返。

**与 Hooks 集成**：可加 `SKILL_LOAD` hook 事件，让 hook 在 skill 加载前/后执行验证或日志。

```python
# entry/chat.py
def _handle_slash_command(self, user_input: str) -> str | None:
    if not user_input.startswith("/"):
        return None
    parts = user_input[1:].split(maxsplit=1)
    name, args = parts[0], (parts[1] if len(parts) > 1 else "")
    meta = self._skill_registry.get(name)
    if meta is None:
        return f"Unknown skill: /{name}"
    if not meta.user_can_invoke:
        return f"Skill /{name} is not user-invocable"
    return self._skill_registry.load_and_render(name, args, session_id=..., project_dir=...)
```

---

### SK-02: 描述自动匹配（LLM 自主决定何时加载 skill）

- **CC 参考**：[Let Claude invoke it automatically](https://code.claude.com/docs/en/skills#getting-started)
- **CC 行为**：系统 prompt 中列出所有 skill 的 `description` + `when_to_use`。LLM 根据当前任务语义自主决定加载哪个 skill。

**修改文件**：`skills/registry.py`

```python
class SkillRegistry:
    def format_for_prompt(self, *, llm_invocable_only: bool = True) -> str:
        """Format available skills for system prompt injection.
        LLM decides which to load based on description matching.
        """
        lines = ["## Available Skills\nUse /skill-name to invoke:"]
        for meta in self._metadata.values():
            if llm_invocable_only and meta.disable_model_invocation:
                continue
            desc = meta.description
            if meta.when_to_use:
                desc += f" ({meta.when_to_use})"
            lines.append(f"- **/{meta.name}**: {desc}")
        return "\n".join(lines)
```

**与 Runtime 集成**：`format_for_prompt()` 输出注入 system prompt → `runtime/prompt.py` 的 prompt builder 调用它。

---

### SK-03: `disable-model-invocation`

- **CC 参考**：[Control who invokes a skill](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill)
- **CC 行为**：`true` → 只有用户 `/name` 能触发，LLM 不能自动加载。

**修改文件**：`skills/registry.py`

```python
@dataclass
class SkillMetadata:
    disable_model_invocation: bool = False
```

**与 Runtime 集成**：`format_for_prompt()` 过滤掉 `disable_model_invocation=True` 的 skill。`/name` 命令不受此限制。

---

### SK-04: `user-invocable`

- **CC 参考**：[Control who invokes a skill](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill)
- **CC 行为**：`false` → skill 从 `/` 菜单隐藏，只有 LLM 能加载。

**修改文件**：`skills/registry.py`

```python
@dataclass
class SkillMetadata:
    user_invocable: bool = True  # False = hidden from / menu
```

**与 Runtime 集成**：`_handle_slash_command()` 检查 `meta.user_can_invoke`，拒绝用户调用。

---

### SK-05: `allowed-tools`

- **CC 参考**：[Frontmatter reference - allowed-tools](https://code.claude.com/docs/en/skills#frontmatter-reference)
- **CC 行为**：skill 激活时限制 LLM 只能使用列出的工具（空格/逗号分隔或 YAML list）。

**修改文件**：`skills/registry.py` + `agent/policy_registry.py`

```python
@dataclass
class SkillMetadata:
    allowed_tools: frozenset[str] = frozenset()

# agent/policy_registry.py
class PolicyAwareToolRegistry:
    def with_skill_active(self, skill: SkillMetadata) -> "PolicyAwareToolRegistry":
        if skill.allowed_tools:
            return self.with_allowed_tools(skill.allowed_tools)
        return self
```

**与 Runtime 集成**：skill 加载时调用 `registry.with_skill_active(skill)`，工具集缩小为 skill 允许的子集。skill 上下文 clear 时恢复原 registry。

**与 Hooks 集成**：`PreToolUse` hook 中 `tool_name not in allowed` → deny。

---

### SK-06: `disallowed-tools`

- **CC 参考**：[Frontmatter reference - disallowed-tools](https://code.claude.com/docs/en/skills#frontmatter-reference)
- **CC 行为**：skill 激活时从可用池中移除列出的工具。限制在下一条用户消息时自动 clear。

**修改文件**：同 SK-05。

```python
@dataclass
class SkillMetadata:
    disallowed_tools: frozenset[str] = frozenset()
```

---

### SK-07: `context: fork`

- **CC 参考**：[Types of skill content](https://code.claude.com/docs/en/skills#types-of-skill-content)
- **CC 行为**：skill 在 forked subagent 上下文中运行。配合 `agent` 字段指定 subagent 类型。

**修改文件**：`skills/registry.py` + `entry/chat.py`

```python
@dataclass
class SkillMetadata:
    context: str = ""   # "" | "fork"
    agent: str = ""     # subagent type when context=fork
```

**与 Runtime 集成**：`context=fork` 时调用 `runtime.spawn_fork(system_prompt=skill_content)`，结果返回给父对话。

---

### SK-08: `paths` glob 激活范围

- **CC 参考**：[Frontmatter reference - paths](https://code.claude.com/docs/en/skills#frontmatter-reference)
- **CC 行为**：`paths: "src/api/**/*.ts"` — 只在处理匹配文件时才自动激活。

**修改文件**：`skills/registry.py`

```python
@dataclass
class SkillMetadata:
    paths: tuple[str, ...] = ()

    def matches_path(self, file_path: str) -> bool:
        if not self.paths: return True
        from fnmatch import fnmatch
        p = file_path.replace("\\", "/")
        return any(fnmatch(p, pat) for pat in self.paths)
```

**与 Runtime 集成**：当 LLM 调用 `Read`/`Edit`/`Write` 等工具时，检查当前操作的文件是否匹配任何 `paths`-restricted skill。如果匹配，自动将该 skill 注入上下文。

---

### SK-09: 动态上下文注入 `` !`command` ``

- **CC 参考**：[Skill example with `!`git diff HEAD``](https://code.claude.com/docs/en/skills#getting-started)
- **CC 行为**：`` !`git diff HEAD` `` → 渲染 skill 前先执行该 shell 命令，输出替换该行。

**修改文件**：`skills/registry.py`

```python
_INLINE_CMD_RE = re.compile(r"!`([^`]+)`")

class SkillRegistry:
    @staticmethod
    def _expand_inline_commands(content: str, cwd: str = ".") -> str:
        def _run(m: re.Match) -> str:
            r = subprocess.run(m.group(1), shell=True, capture_output=True, text=True, timeout=30, cwd=cwd)
            return r.stdout.strip() or "(no output)"
        return _INLINE_CMD_RE.sub(_run, content)
```

**与 Runtime 集成**：命令在项目 CWD 下执行 → 复用 `LocalRuntime.exec()` 进行沙箱隔离。

**与 Hooks 集成**：`` !`command` `` 中的命令触发 `PreToolUse` hook（以 Bash 工具身份）。如果该命令被 deny 规则拦截，`` !`...` `` 块返回 `(blocked)`。

---

### SK-10: `$ARGUMENTS[N]` 索引参数

- **CC 参考**：[Available string substitutions](https://code.claude.com/docs/en/skills#available-string-substitutions)
- **CC 行为**：`$ARGUMENTS[0]` 展开为第一个参数。

```python
subs[f"$ARGUMENTS[{i}]"] = args_list[i] if i < len(args_list) else ""
```

---

### SK-11: `$N` 简写参数

- **CC 参考**：同上
- **CC 行为**：`$0`、`$1` 简写。

```python
subs[f"${i}"] = args_list[i] if i < len(args_list) else ""
```

---

### SK-12: `$name` 命名参数

- **CC 参考**：同上
- **CC 行为**：frontmatter `arguments: [issue, branch]` → `$issue` 展开为第一个参数。

```python
# SkillMetadata
arguments: tuple[str, ...] = ()
# load_and_render() 中
for idx, name in enumerate(meta.arguments):
    subs[f"${name}"] = args_list[idx] if idx < len(args_list) else ""
```

---

### SK-13: `${CLAUDE_SESSION_ID}`

- **CC 参考**：同上
- **CC 行为**：当前会话 ID，用于日志/文件命名。

```python
subs["${CLAUDE_SESSION_ID}"] = session_id
```

---

### SK-14: `${CLAUDE_PROJECT_DIR}`

- **CC 参考**：同上
- **CC 行为**：项目根目录绝对路径。

```python
subs["${CLAUDE_PROJECT_DIR}"] = project_dir
```

---

### SK-15: `${CLAUDE_SKILL_DIR}`

- **CC 参考**：同上
- **CC 行为**：skill 的 `SKILL.md` 所在目录。用于引用 skill 自带脚本。

```python
subs["${CLAUDE_SKILL_DIR}"] = meta.dir_path
```

---

### SK-16: `${CLAUDE_EFFORT}`

- **CC 参考**：同上
- **CC 行为**：当前 effort level（`low`/`medium`/`high`/`xhigh`/`max`）。

```python
subs["${CLAUDE_EFFORT}"] = effort_level
```

---

### SK-17: 支持文件

- **CC 参考**：[Add supporting files](https://code.claude.com/docs/en/skills#add-supporting-files)
- **CC 行为**：skill 目录可有 `reference.md`、`examples.md`、`scripts/`。LLM 按需 `Read` 这些文件。

**修改文件**：`skills/registry.py` — `load_and_render()` 末尾追加文件索引。

```python
@staticmethod
def _list_supporting_files(skill_dir: str) -> list[str]:
    lines = []
    for entry in sorted(Path(skill_dir).iterdir()):
        if entry.name == "SKILL.md": continue
        if entry.is_file(): lines.append(f"- `{entry.name}`")
        elif entry.is_dir(): lines.append(f"- `{entry.name}/`")
    return lines
```

---

### SK-18: 实时变更检测

- **CC 参考**：[Live change detection](https://code.claude.com/docs/en/skills#live-change-detection)
- **CC 行为**：监听 skill 目录，新增/编辑/删除立即生效。

**修改文件**：`skills/registry.py`

```python
def _start_watcher(self):
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    class H(FileSystemEventHandler):
        def on_any_event(self, event): self.registry.refresh()
    ...
```

**与 Runtime 集成**：`refresh()` 后 system prompt 和 tool schemas 需要重建 → 触发 `PromptBuilder.invalidate_cache()`。

---

### SK-19: 嵌套/目录限定 skill

- **CC 参考**：[Where skills live](https://code.claude.com/docs/en/skills#where-skills-live)
- **CC 行为**：monorepo 中 `apps/web/.claude/skills/deploy/` → `/apps/web:deploy`。

**修改文件**：`skills/registry.py` — 扫描子目录 `.claude/skills/`

---

### SK-20: `model` / `effort` 覆盖

- **CC 参考**：[Frontmatter reference - model](https://code.claude.com/docs/en/skills#frontmatter-reference)
- **CC 行为**：skill 激活时临时切换 model / effort level。下一条用户消息时恢复。

```python
@dataclass
class SkillMetadata:
    model: str = ""     # e.g. "opus", "haiku", "inherit"
    effort: str = ""    # e.g. "high", "xhigh", "inherit"
```

---

## 四、Skills — 做错了（3 项）

### SK-E1: 调用模型错误

- **错在哪**：通过 `use_skill` **工具调用** skill。LLM 做出 tool_use → execute → observation → 下轮看到内容。多一轮往返 + 无法被用户 `/name` 直接触发。
- **应改为**：`/skill-name` 斜杠命令直接注入上下文（不走 tool_use）。LLM 可以通过描述匹配自主决定加载（系统 prompt 中列出可用 skill）。`SkillTool` 降级为 fallback（CC 同名工具 `Skill` 仅用于不允许 `/name` 的场景）。

```python
# 新：entry/chat.py
def _handle_user_message(self, user_input: str) -> str | None:
    slash_result = self._handle_slash_command(user_input)
    if slash_result:
        self._shared_history.append(LLMMessage(role="user", content=slash_result))
        return slash_result
    return None  # normal message flow
```

### SK-E2: triggers 子串匹配

- **错在哪**：`match_triggers()` 用 `trigger.lower() in text_lower`。CC 不这么做——LLM 通过 `description` 语义匹配自主判断。
- **应改为**：删除 `triggers` 字段和 `match_triggers()` 方法。只保留 `description` + `when_to_use` 用于 system prompt listing。LLM 自主决定。

### SK-E3: 工具名 `"use_skill"`

- **错在哪**：CC 的 skill 工具名为 `Skill`。

```python
class SkillTool(BaseTool):
    @property
    def name(self) -> str: return "Skill"
    aliases = ("use_skill",)
```

---

## 五、缺失功能总表（21 项）

| 优先级 | 编号 | 模块 | 功能 | 涉及文件数 |
|--------|------|------|------|----------|
| 🔴 P0 | MCP-01 | MCP | HTTP 传输 | 4 |
| 🔴 P0 | MCP-02 | MCP | SSE 传输 | 2 |
| 🔴 P0 | MCP-03 | MCP | WebSocket 传输 | 2 |
| 🔴 P0 | SK-01 | Skills | `/skill-name` 斜杠命令 | 3 |
| 🔴 P0 | SK-02 | Skills | 描述自动匹配 | 1 |
| 🔴 P1 | MCP-04 | MCP | 自动重连 | 1 |
| 🔴 P1 | SK-03 | Skills | `disable-model-invocation` | 2 |
| 🔴 P1 | SK-04 | Skills | `user-invocable` | 2 |
| 🟡 P2 | MCP-05 | MCP | `list_changed` 动态更新 | 1 |
| 🟡 P2 | MCP-06 | MCP | CLI 管理 | 1 |
| 🟡 P2 | SK-05 | Skills | `allowed-tools` | 2 |
| 🟡 P2 | SK-06 | Skills | `disallowed-tools` | 2 |
| 🟡 P2 | SK-07 | Skills | `context: fork` | 2 |
| 🟡 P3 | SK-08 | Skills | `paths` glob | 1 |
| 🟡 P3 | SK-09 | Skills | `` !`cmd` `` 注入 | 1 |
| 🟡 P3 | SK-10~16 | Skills | 7 种字符串替换 | 1 |
| 🟢 P4 | SK-17 | Skills | 支持文件 | 1 |
| 🟢 P4 | SK-18 | Skills | 实时变更检测 | 1 |
| 🟢 P4 | SK-19 | Skills | 嵌套 skill | 1 |
| 🟢 P4 | SK-20 | Skills | model/effort 覆盖 | 1 |
| 🟢 P4 | MCP-07 | MCP | `CLAUDE_PROJECT_DIR` | 1 |
| 🟢 P4 | MCP-08 | MCP | Plugin MCP | — |

## 六、做错了总表（6 项）

| 编号 | 模块 | 问题 | 错在哪 | 涉及文件数 |
|------|------|------|--------|----------|
| MCP-E1 | MCP | 只支持 stdio | `_parse_server_config()` 硬拒绝非 stdio | 3 |
| MCP-E2 | MCP | 配置路径不标准 | `~/.forge-agent/mcp.json` 而非 `.mcp.json` | 1 |
| MCP-E3 | MCP | 无 idle timeout | 只有总超时，无 idle 检测 | 1 |
| SK-E1 | Skills | 调用模型 | `use_skill` 工具而非 `/name` 命令 | 3 |
| SK-E2 | Skills | triggers 子串匹配 | `trigger.lower() in text_lower` | 1 |
| SK-E3 | Skills | 工具名不对 | `"use_skill"` → 应为 `"Skill"` | 1 |

---

## 七、建议执行顺序

1. **先修错误**（E1-E6）——修错不引入新功能，风险最低
2. **再补 P0 缺失**——HTTP 传输 + 斜杠命令，最大架构差距
3. **再补 P1 缺失**——自动重连 + 调用控制，安全关键
4. **最后 P2-P4**——渐进增强

# 剩余差距：完整计划与实施指导

> 基于 `cc-permission-comparison.md` 差距对比表的逐项审计
> 已实现: P0(4/4) ✅ P1(5/5) ✅ P2(2/5)

---

## 审计结果: 已实现 vs 未实现

### 已实现 ✅

| 差距项 | 来源章节 | 实现 Batch |
|--------|----------|------------|
| 硬安全门 (.git/.claude/ 路径保护) | 3.1 | P1-5 |
| requiresUserInteraction | 3.1 | P1-6 |
| decision_reason + tool_use_id | 3.3 | P1-7 |
| \*\* 递归通配 | 3.5 | P1-8 |
| 拒绝成功重置 | 3.4 | P1-9 |
| 模型切换 | 4.1 | P0-1 |
| Skills | 4.4 | P0-3 |
| 上下文压缩 | 4.5 | P0-2 |
| 文件附件 | 4.6 | P0-4 |
| 思考模式 + 权限热切换 | 4.2 + 4.8 | P2-10+11 |

### 未实现 — 按优先级排列

| # | 差距项 | 来源 | 严重度 | 工作量 |
|---|--------|------|--------|--------|
| G1 | acceptEdits 添加 rm/rmdir/sed | 3.2 | 🟡 | 小 |
| G2 | 熔断 headless 直接终止 | 3.4 | 🟡 | 小 |
| G3 | 完成守卫外部条件注入 | 3.7 | 🟡 | 中 |
| G4 | deny→ask→allow 优先级 (CC 对齐) | 3.1 | 🟡 | 中 |
| G5 | Settings hot-reload | 4.8 | 🟡 | 中 |
| G6 | MCP 支持 | 4.3 | 🔴 | 大 |
| G7 | IDE 选区同步 | 4.7 | 🔴 | 大 |
| G8 | auto/YOLO 模式 | 3.2 | 🔴 | 大 |
| G9 | 8 源规则层级 | 3.5 | 🔴 | 大 |
| G10 | bubble 子代理模式 | 3.2 | ⚪ | 暂不需要 |

---

## G1: acceptEdits 添加 rm/rmdir/sed 安全命令

### 差距

CC 的 `acceptEdits` 模式自动批准 `mkdir, touch, rm, rmdir, mv, cp, sed`。
我们当前仅支持 `mkdir, touch, mv, cp`。

### 现状

**文件:** `hitl/pipeline.py:639-646`
```python
_FILESYSTEM_SAFE_COMMANDS: frozenset[str] = frozenset({
    "mkdir", "touch", "mv", "cp",
})
```

### 修复

```python
_FILESYSTEM_SAFE_COMMANDS: frozenset[str] = frozenset({
    "mkdir", "touch", "mv", "cp",
    "rm", "rmdir", "sed",       # CC-aligned additions
})
```

**注意:** `rm` 有风险。但 CC 在 `acceptEdits` 中也包含它，因为这是开发者经常使用的安全命令。Layer 1 的 `_BLOCKED_PATTERNS`（如 `rm -rf /`）已经覆盖了危险用法。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | 添加 `"rm"`, `"rmdir"`, `"sed"` |

### 验证

发送 `"delete test.txt with rm test.txt"` → Bash(rm) 在 acceptEdits 模式下应自动通过。

---

## G2: 熔断 headless 直接终止

### 差距

CC 在熔断跳闸时 headless 模式会**直接终止 agent**。我们当前仅注入消息并继续。

### 现状

**文件:** `hitl/pipeline.py:381-401`

当前逻辑：连续 3 次拒绝 → 升级拒绝原因文本，总计 20 次 → 拒绝。但不强制终止。

### 修复

在 `check()` 的 DENY 分支添加超限终止：

```python
if tier is PermissionRuleTier.DENY:
    consecutive = self._denial_counters.get(tool_name, 0) + 1
    self._denial_counters[tool_name] = consecutive
    self._total_denials += 1
    
    # CC-aligned: headless termination on circuit breaker trip
    if consecutive >= 3 and self._web_confirm_callback is not None:
        # Headless mode: force agent termination
        return PermissionResult(
            decision=PermissionDecision.DENY,
            layer=PermissionLayer.RULE,
            reason=(
                f"Tool '{tool_name}' denied {consecutive} consecutive times. "
                "Session terminated — agent is stuck in a denial loop."
            ),
            feedback="CIRCUIT_BREAKER_TERMINATE",  # signal to caller
        )
    # ... existing total denial logic ...
```

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | DENY 分支添加 headless 终止 |
| `agent/core.py` | 检查 `CIRCUIT_BREAKER_TERMINATE` feedback 并强制 GIVE_UP |

### 验证

连续拒绝同一工具 3 次 → agent 应终止而非继续。

---

## G3: 完成守卫外部条件注入

### 差距

CC 的完成守卫支持外部 `verify_callback` 注入。我们的 `TaskCompletionGuard.check()` 已接受 `verify_callback` 参数但调用方未使用。

### 现状

**文件:** `agent/completion_guard.py:209-212`
```python
if verify_callback is not None:
    callback_result = verify_callback()
    if not callback_result.can_complete:
        return callback_result
```

**调用方:** `agent/core.py:1237` — `completion_fact_check` 已传入但仅做 git diff 检查。

### 修复

1. 将 `CompletionContext` 传递给 `verify_callback`
2. 允许外部（如 hooks 或 API）注入自定义完成条件：

```python
# runtime.py — add a per-session completion verifier
def set_completion_verifier(self, session_id: str, verifier: Callable):
    self._completion_verifiers[session_id] = verifier

# agent/core.py — call it in the completion guard
_external_verifier = getattr(task, 'completion_verifier', None)
guard_result = completion_guard.check(
    ctx=completion_ctx,
    task_intent=task.intent,
    git_state=_git_state,
    verify_callback=_external_verifier,
)
```

### 涉及文件

| 文件 | 改动 |
|------|------|
| `agent/completion_guard.py` | 传递 CompletionContext 给 verify_callback |
| `agent/core.py` | 从 task 获取外部 verifier |
| `agent/session/runtime.py` | 添加 set_completion_verifier |

---

## G4: deny→ask→allow 优先级 (CC 对齐)

### 差距

CC 的管线 Phase 1 是 `deny → ask → allow`（ask 在 allow 之前，且 bypass-immune）。

我们的 Layer 3 是 `deny → session_allow → allow → ask`（allow 在 ask 之前）。

### 设计权衡

**CC 方式 (deny→ask→allow):**
- Ask 规则是 bypass-immune 的
- 用户无法通过添加 allow 规则来覆盖内置 ask
- 更安全，但灵活性较低

**我们当前方式 (deny→allow→ask):**
- Ask 规则在 allow 之后
- 用户可以通过添加 allow 规则覆盖内置 ask
- 更灵活，但 ask 的保护力较弱

### 修复

如果选择 CC 对齐，将 Layer 3 优先级改为 `deny → ask → session_allow → allow`：

```python
def _layer3_rules(self, tool_name, params):
    # 1. Deny (highest, safety invariant)
    for rule in self._deny_rules:
        if rule.matches(tool_name, params):
            return PermissionRuleTier.DENY
    
    # 2. Ask (CC: bypass-immune)
    for rule in self._ask_rules:
        if rule.matches(tool_name, params):
            return PermissionRuleTier.ASK
    
    # 3. Session rules (Always Allow)
    for rule in self._session_rules:
        if rule.matches(tool_name, params):
            return PermissionRuleTier.ALLOW
    
    # 4. Static allow
    for rule in self._allow_rules:
        if rule.matches(tool_name, params):
            return PermissionRuleTier.ALLOW
    
    return None
```

**风险:** 用户可能对内置 ask 规则感到困惑（无法通过 settings.json 覆盖）。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `hitl/pipeline.py` | `_layer3_rules` 重排优先级 |

---

## G5: Settings hot-reload

### 差距

CC 声称 v1.0.90+ 支持 settings 热重载。我们的所有设置都在服务启动时加载。

### 现状

**文件:** `server/services/agent_service.py:133` — `_loaded_rules = self._load_permission_rules()`
加载时机: `__init__` → 之后不变

### 修复

**Step 1: 添加 file watcher**

```python
# server/services/agent_service.py
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class SettingsReloadHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith("settings.json"):
            logger.info("Settings file changed — reloading rules")
            self._agent_service._loaded_rules = self._agent_service._load_permission_rules()

def _start_settings_watcher(self):
    self._watcher = Observer()
    handler = SettingsReloadHandler()
    handler._agent_service = self
    settings_dir = Path(self.repo_path) / ".forge-agent"
    if settings_dir.exists():
        self._watcher.schedule(handler, str(settings_dir), recursive=False)
    self._watcher.start()
```

**Step 2: 在 `_run_and_notify` 中每次读取最新规则**

当前规则在 `_run_and_notify` 中通过 `list(self._loaded_rules)` 传入。如果 watcher 更新了 `_loaded_rules`，下次运行会自动使用新规则。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `server/services/agent_service.py` | File watcher + 自动重载 |
| `requirements.txt` | 可能需要 `watchdog` 依赖 |

---

## G6: MCP 支持

### 差距

CC 支持完整的 MCP (Model Context Protocol)，包括 `stdio`/`http` 传输、`requiresUserInteraction` 标记、`mcp__<server>__<tool>` 权限格式。

### 实施范围

这是**独立里程碑**级别的工作：

1. MCP 客户端实现（连接管理、工具发现）
2. MCP 工具注册到 ToolRegistry
3. 权限格式 `mcp__<server>__<tool>`
4. `requiresUserInteraction` 集成（已有 P1-6 的基础）
5. `/mcp` 管理命令

### 涉及文件 (新建为主)

| 文件 | 内容 |
|------|------|
| `mcp/client.py` | MCP 客户端 (stdio + http 传输) |
| `mcp/registry.py` | MCP 工具注册 + 发现 |
| `server/routers/mcp.py` | `/api/mcp/*` 管理端点 |
| `hitl/permission_rule.py` | `mcp__*` 权限格式 |

### 参考

> **来源:** [Claude Code MCP docs](https://code.claude.com/docs/en/mcp), [GitHub issue #52470](https://github.com/anthropics/claude-code/issues/52470)

---

## G7: IDE 选区同步

### 差距

CC 的 VS Code 扩展自动注入当前文件 + 选区到上下文。

### 实施范围

需要 VS Code 扩展 + 后端 WebSocket 通道。超出当前项目范围。

---

## G8: auto/YOLO 模式

### 差距

CC 的 `auto` 模式使用独立 LLM 调用（两阶段 Sonnet 分类器）自动决策是否批准工具。

### 实施范围

1. 独立 LLM 分类器调用（Stage 1: fast Sonnet, Stage 2: deep Sonnet）
2. 转录排除（助手的文本回复被视为对抗内容）
3. 行为提示每 5 轮注入
4. `TRANSCRIPT_CLASSIFIER` feature flag

**建议:** P3 级别。在当前阶段，`approval_mode="auto"` (无分类器) + `force_interactive` ASK 规则已提供合理的自动化水平。

> **来源:** [Tencent Cloud analysis](https://cloud.tencent.cn/developer/article/2653444), [The Block Beats](https://en.theblockbeats.news/flash/338093)

---

## G9: 8 源规则层级

### 差距

CC 从 8 个来源加载规则（userSettings → session）。我们仅 3 个（builtin + project + local）。

### 实施范围

添加对以下来源的支持：
- `flagSettings`: `--settings` CLI 参数 → 仅 CLI 模式
- `policySettings`: 企业托管配置 → 非当前需求
- `cliArg`: `--allow` / `--deny` 参数 → 仅 CLI 模式
- `command`: Skill tool `allowedTools` → 已有 Skills 基础设施

**建议:** `command` 来源 (Skills allowedTools) 可以在 Skills 系统中实现。其余是 CLI/企业功能，Web MVP 不需要。

---

## 实施计划

### 批次规划

| Batch | 内容 | 文件 | 工作量 |
|-------|------|------|--------|
| **G1** | acceptEdits 添加 rm/rmdir/sed | 1 | 5 分钟 |
| **G2** | 熔断 headless 终止 | 2 | 30 分钟 |
| **G3** | 完成守卫外部条件注入 | 3 | 1 小时 |
| **G4** | deny→ask→allow 优先级 (可选) | 1 | 15 分钟 |
| **G5** | Settings hot-reload | 1-2 | 2 小时 |
| **G6-G9** | MCP/IDE/YOLO/8源 | 多个 | 独立里程碑 |

### 建议实施顺序

```
Batch G1: acceptEdits 命令补全         (1 文件)
Batch G2: 熔断 headless 终止           (2 文件)
Batch G3: 完成守卫外部条件注入          (3 文件)
Batch G4: deny→ask→allow 优先级 (可选) (1 文件)
Batch G5: Settings hot-reload          (1 文件)
--- 以下暂停，待独立规划 ---
G6-G9: MCP / IDE / YOLO / 8源
```

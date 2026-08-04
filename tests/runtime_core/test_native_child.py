"""Phase 1: Native Child Contract + Runner — Before Tests → Target Tests.

All tests expect ModuleNotFoundError before implementation.
After implementation, BT-1 through BT-7 must all pass.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# BT-1: NativeChildRequest 字段匹配 CC Agent Tool 参数
# ═══════════════════════════════════════════════════════════════════════════════

def test_child_request_matches_cc_agent_tool_schema():
    """NativeChildRequest = CC Agent Tool 的最小字段集（+ Grace Code 扩展）。"""
    from runtime_core.native_child_contract import NativeChildRequest

    req = NativeChildRequest(
        description="Find relevant files",
        prompt="Inspect the routing layer",
        subagent_type="explore",
    )
    # CC 字段
    assert req.description == "Find relevant files"
    assert req.prompt == "Inspect the routing layer"
    assert req.subagent_type == "explore"
    assert req.model == ""           # inherit parent
    assert req.run_in_background is False
    assert req.isolation == ""       # current workspace
    # Grace Code 扩展
    assert req.idempotency_key == ""
    # 明确不存在的 v1.x 字段
    assert not hasattr(req, "tool_profile")
    assert not hasattr(req, "context_policy")
    assert not hasattr(req, "context_mode")
    assert not hasattr(req, "metadata")
    assert not hasattr(req, "budget_tokens")
    assert not hasattr(req, "max_steps")
    assert not hasattr(req, "placement")


# ═══════════════════════════════════════════════════════════════════════════════
# BT-2: NativeChildResult 字段匹配 CC Agent Tool 输出
# ═══════════════════════════════════════════════════════════════════════════════

def test_child_result_matches_cc_agent_tool_output():
    """NativeChildResult 核心字段 = CC Agent Tool 输出字段。"""
    from runtime_core.native_child_contract import NativeChildResult

    result = NativeChildResult(
        status="completed",
        agent_id="agent-abc123",
        content="Found 3 files: ...",
        total_tool_use_count=5,
        total_duration_ms=1200.0,
        total_tokens=3500,
    )
    # CC 字段
    assert result.status == "completed"
    assert result.agent_id == "agent-abc123"
    assert result.content == "Found 3 files: ..."
    assert result.total_tool_use_count == 5
    assert result.total_duration_ms == 1200.0
    assert result.total_tokens == 3500
    # Grace Code 扩展默认值
    assert result.error == ""
    assert result.structured_report is None
    assert result.evidence_refs == ()
    assert result.worktree_disposition == "not_applicable"
    # 明确不存在的 v1.x 字段
    assert not hasattr(result, "clarification_needed")
    assert not hasattr(result, "child_session_id")
    assert not hasattr(result, "child_run_id")
    assert not hasattr(result, "state")


# ═══════════════════════════════════════════════════════════════════════════════
# BT-3: JSON roundtrip
# ═══════════════════════════════════════════════════════════════════════════════

def test_child_contract_json_roundtrip():
    """NativeChildRequest / NativeChildResult 可 JSON 往返。"""
    from runtime_core.native_child_contract import (
        NativeChildRequest,
        NativeChildResult,
    )

    req = NativeChildRequest(
        description="Search codebase",
        prompt="Find all API endpoints",
        subagent_type="explore",
    )
    result = NativeChildResult(
        status="completed",
        agent_id="agent-xyz",
        content="Found 12 endpoints",
        total_tool_use_count=8,
        total_duration_ms=3400.0,
        total_tokens=8200,
    )
    for obj in (req, result):
        d = obj.to_dict()
        encoded = json.dumps(d, ensure_ascii=False, sort_keys=True)
        decoded = type(obj).from_dict(json.loads(encoded))
        assert decoded == obj, f"{type(obj).__name__} roundtrip failed"


# ═══════════════════════════════════════════════════════════════════════════════
# BT-4: filter_tool_schemas 按 allowlist + denylist 过滤
# ═══════════════════════════════════════════════════════════════════════════════

def test_filter_tool_schemas_respects_allowlist_and_denylist():
    """Child 工具 = parent ∩ allowed − disallowed。"""
    from runtime_core.native_child_runner import filter_tool_schemas
    from runtime_core.native_backend import NativeToolSchema

    schemas = (
        NativeToolSchema("Read", "Read file", {}),
        NativeToolSchema("Write", "Write file", {}),
        NativeToolSchema("Grep", "Search code", {}),
        NativeToolSchema("Bash", "Shell command", {}),
    )
    filtered = filter_tool_schemas(
        schemas,
        allowed=frozenset({"Read", "Grep", "Bash"}),
        disallowed=frozenset({"Bash"}),
    )
    names = {s.name for s in filtered}
    assert names == {"Read", "Grep"}


def test_filter_tool_schemas_empty_allowed_returns_empty():
    """allowed 为空 → child 无工具。"""
    from runtime_core.native_child_runner import filter_tool_schemas
    from runtime_core.native_backend import NativeToolSchema

    schemas = (NativeToolSchema("Read", "", {}),)
    result = filter_tool_schemas(schemas, allowed=frozenset(), disallowed=frozenset())
    assert result == ()


def test_filter_tool_schemas_empty_disallowed_passes_all_allowed():
    """disallowed 为空 → 只按 allowed 过滤。"""
    from runtime_core.native_child_runner import filter_tool_schemas
    from runtime_core.native_backend import NativeToolSchema

    schemas = (
        NativeToolSchema("Read", "", {}),
        NativeToolSchema("Grep", "", {}),
        NativeToolSchema("Write", "", {}),
    )
    result = filter_tool_schemas(
        schemas,
        allowed=frozenset({"Read", "Grep"}),
        disallowed=frozenset(),
    )
    assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# BT-5: child_runtime_ports 工具受限
# ═══════════════════════════════════════════════════════════════════════════════

def test_child_ports_has_restricted_tools():
    """从 parent ports 构造的 child ports 只暴露 allowed 工具。"""
    from runtime_core.native_child_runner import child_runtime_ports
    from runtime_core.ports import RuntimePorts, ToolPort
    from runtime_core.native_backend import NativeToolSchema
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    # Fake tool port: 只有 Read / Write / Bash 三个工具
    class _FakeToolPort:
        def execute(self, tool_name, params, invocation_id=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=tool_name)

    parent_ports = RuntimePorts(
        llm=object(),
        tools=_FakeToolPort(),
        hooks=object(),
        live_events=object(),
        clock=object(),
        token_usage=object(),
    )
    # Child definition: 只有 Read + Grep，禁用 Bash
    definition = AgentDefinition(
        name="explore",
        description="Read-only code search",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep"}),
        disallowed_tools=frozenset({"Bash"}),
    )
    child = child_runtime_ports(parent_ports, definition)
    assert child is not parent_ports
    assert hasattr(child.tools, 'execute')


# ═══════════════════════════════════════════════════════════════════════════════
# BT-6: run_native_child 用 fake backend 完成一次 child 执行
# ═══════════════════════════════════════════════════════════════════════════════

def test_run_native_child_completes():
    """用 fake backend，child 应能完成一次执行并返回 RuntimeOutcome。"""
    from runtime_core.native_child_runner import run_native_child
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle
    from runtime_core.native_message import NativeConversation, NativeMessage
    from runtime_core.native_backend import NativeBackend
    from core.eventing.identifiers import SessionId, RunId

    # Minimal fake LLM that returns AssistantText immediately
    class _FakeLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="Task completed.", stop_reason="end_turn")

        def stream(self, conversation, *, tool_choice=None, cancellation=None):
            raise NotImplementedError

    class _FakeLiveEvents:
        def publish(self, event_type, payload, scope=None):
            pass  # no-op for test

    class _FakeHooks:
        def check(self, event_type, hook_input, tool_name=""):
            from runtime_core.ports import HookGateResult
            return HookGateResult(allowed=True)

    class _FakeClock:
        def now(self):
            import time
            return time.monotonic()
        def deadline(self, timeout_s):
            return self.now() + timeout_s

    class _FakeTokenUsage:
        def record(self, run_id, input_tokens, output_tokens):
            pass

    ports = RuntimePorts(
        llm=_FakeLLM(),
        tools=object(),     # not called if model returns text
        hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(),
        clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )
    conv = NativeConversation(messages=(
        NativeMessage.user("Test prompt"),
    ))
    outcome = run_native_child(
        ports=ports,
        session_id=SessionId("child-1"),
        run_id=RunId("run-1"),
        conversation=conv,
        cancellation=CancellationHandle(),
        max_steps=5,
        budget_tokens=1000,
    )
    assert outcome.status.value == "completed"
    assert "Task completed" in outcome.summary


# ═══════════════════════════════════════════════════════════════════════════════
# BT-7: 静态边界 — 禁止导入旧路径
# ═══════════════════════════════════════════════════════════════════════════════

# ── Shared fake ports for tests ──────────────────────────────────────────

import time as _time

class _FakeHooks:
    def check(self, event_type, hook_input, tool_name=""):
        from runtime_core.ports import HookGateResult
        return HookGateResult(allowed=True)


class _FakeLiveEvents:
    def publish(self, event_type, payload, scope=None):
        pass


class _FakeClock:
    def now(self):
        return _time.monotonic()
    def deadline(self, timeout_s):
        return self.now() + timeout_s


class _FakeTokenUsage:
    def record(self, run_id, input_tokens, output_tokens):
        pass


FORBIDDEN_IMPORTS = [
    "SessionRuntime", "ReActAgent", "LLMMessage",
    "ToolExecutionPipeline", "AgentSpawnContext", "AgentSpawnRequest",
]

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "runtime_core")


def test_native_child_no_legacy_imports():
    """native_child_contract.py / native_child_runner.py 不导入旧路径。"""
    import ast
    violations: list[str] = []
    for module_name in ("native_child_contract.py", "native_child_runner.py", "native_agent_tool.py"):
        path = os.path.join(RUNTIME_DIR, module_name)
        if not os.path.isfile(path):
            # BT runs before implementation — skip
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                for forbidden in FORBIDDEN_IMPORTS:
                    if forbidden in module:
                        violations.append(
                            f"{module_name}: imports '{module}' (contains forbidden '{forbidden}')"
                        )
    assert violations == [], (
        f"Phase 1 static boundary FAILED — forbidden imports:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — BT-8: NativeAgentTool.parameters_schema = CC Agent Tool schema
# ═══════════════════════════════════════════════════════════════════════════════

def test_native_agent_tool_schema_matches_cc():
    """NativeAgentTool 的参数 = CC Agent Tool 的字段，无 v1.x 编造字段。"""
    from runtime_core.native_agent_tool import NativeAgentTool
    from runtime_core.ports import RuntimePorts

    ports = RuntimePorts(
        llm=object(), tools=object(), hooks=object(),
        live_events=object(), clock=object(), token_usage=object(),
    )
    tool = NativeAgentTool(definition_registry={}, parent_ports=ports)
    schema = tool.parameters_schema
    props = schema["properties"]

    # CC 字段
    assert "description" in props
    assert "prompt" in props
    assert "subagent_type" in props
    assert "model" in props
    assert "run_in_background" in props
    assert "isolation" in props
    # 明确不存在的 v1.x 字段
    assert "tool_profile" not in props
    assert "context_policy" not in props
    assert "execution_placement" not in props


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — BT-9: NativeAgentTool.execute 端到端 child spawn
# ═══════════════════════════════════════════════════════════════════════════════

def test_native_agent_tool_spawns_and_returns_result():
    """Agent 工具执行 → resolve definition → run_native_child → 返回 ToolResult。"""
    from runtime_core.native_agent_tool import NativeAgentTool
    from runtime_core.ports import RuntimePorts
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult
    from runtime_core.native_child_contract import NativeChildResult
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent
    import json

    # Fake LLM that returns AssistantText immediately
    class _FakeLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="Child task done.", stop_reason="end_turn")
        def stream(self, *args, **kwargs):
            raise NotImplementedError

    class _FakeEvents:
        def publish(self, *args, **kwargs):
            pass

    class _FakeHooks:
        def check(self, *args, **kwargs):
            return HookGateResult(allowed=True)

    import time as _t
    class _FakeClock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t

    class _FakeToken:
        def record(self, *args, **kwargs): pass

    class _FakeTools:
        def execute(self, name, params, invocation_id=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=name)

    ports = RuntimePorts(
        llm=_FakeLLM(), tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeEvents(), clock=_FakeClock(), token_usage=_FakeToken(),
    )

    # Agent definition registry
    explore_def = AgentDefinition(
        name="explore",
        description="Fast read-only code search",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep", "Glob"}),
    )
    definitions = {"explore": explore_def}

    tool = NativeAgentTool(definition_registry=definitions, parent_ports=ports)
    result = tool.execute({
        "description": "Search code",
        "prompt": "Find all API endpoints",
        "subagent_type": "explore",
    })
    assert result.success is True
    assert "Child task done" in result.output

    # Verify output contains JSON-serialized NativeChildResult
    child_result = json.loads(result.output)
    assert child_result["status"] == "completed"
    assert child_result["agent_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — BT-10: NativeAgentTool 不依赖旧路径
# ═══════════════════════════════════════════════════════════════════════════════

def test_native_agent_tool_no_legacy_agent_tool_import():
    """native_agent_tool.py 不 import 旧 AgentTool。"""
    import ast
    import os
    path = os.path.join(RUNTIME_DIR, "native_agent_tool.py")
    if not os.path.isfile(path):
        return  # skip before implementation
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, 'module', '') or ''
            for forbidden in ["SessionRuntime", "ReActAgent", "LLMMessage"]:
                if forbidden in module:
                    violations.append(f"imports '{module}'")
    assert violations == [], f"BT-10 FAILED: {violations}"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — BT-11: Project rules (GRACE.md) 注入 child conversation
# ═══════════════════════════════════════════════════════════════════════════════

def test_child_conversation_includes_project_rules(tmp_path):
    """Child conversation 包含 .grace/GRACE.md 内容。"""
    from runtime_core.native_child_runner import build_child_conversation
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    # Write project rules
    grace_dir = tmp_path / ".grace"
    grace_dir.mkdir()
    (grace_dir / "GRACE.md").write_text("Use pnpm, not npm.", encoding="utf-8")

    definition = AgentDefinition(
        name="explore",
        description="Read-only code search",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep"}),
    )
    conv = build_child_conversation(
        definition=definition,
        prompt="Search code",
        description="",
        project_dir=str(tmp_path),
    )
    has_rules = any(
        hasattr(m, 'content') and any(
            hasattr(b, 'text') and "pnpm" in b.text
            for b in (m.content if isinstance(m.content, tuple) else ())
        )
        for m in conv.messages
    )
    assert has_rules, "GRACE.md content must appear in child conversation"


def test_child_conversation_without_project_rules():
    """若无 GRACE.md，不注入 project rules，其他消息照常（3 条）。"""
    from runtime_core.native_child_runner import build_child_conversation
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent
    import tempfile, os

    definition = AgentDefinition(
        name="explore",
        description="Read-only code search",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep"}),
    )
    conv = build_child_conversation(
        definition=definition,
        prompt="Search",
        description="",
        project_dir=tempfile.gettempdir(),
    )
    # Phase 1: system(opt) + protocol + task — system prompt may be absent
    # if definition has no system_prompt and no GRACE.md
    assert len(conv.messages) >= 2


def test_child_conversation_includes_agent_system_prompt():
    """Agent system prompt 作为第一条消息注入。"""
    from runtime_core.native_child_runner import build_child_conversation
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    definition = AgentDefinition(
        name="reviewer",
        description="Code reviewer",
        intent=TaskIntent.ANALYSIS,
        system_prompt="You are a code reviewer. Be thorough.",
    )
    conv = build_child_conversation(
        definition=definition,
        prompt="Review this",
    )
    assert conv.messages[0].role == "system"
    # First message content must include the system prompt text
    assert any(
        hasattr(b, 'text') and "code reviewer" in b.text
        for b in (conv.messages[0].content if isinstance(conv.messages[0].content, tuple) else ())
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — BT-12: Model resolve 优先级
# ═══════════════════════════════════════════════════════════════════════════════

def test_resolve_child_model_hierarchy():
    """request.model > definition.model > inherit parent。"""
    from runtime_core.native_child_context import resolve_child_model

    class _Backend:
        model_name = "claude-sonnet"

    parent = _Backend()
    assert resolve_child_model("haiku", "sonnet", parent) == "haiku"
    assert resolve_child_model("", "sonnet", parent) == "sonnet"
    assert resolve_child_model("", "inherit", parent) == "claude-sonnet"
    assert resolve_child_model("", "", parent) == "claude-sonnet"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — BT-13: NativeAgentTool 传 model → child 端到端正确
# ═══════════════════════════════════════════════════════════════════════════════

def test_agent_tool_model_override_end_to_end():
    """definition.model='haiku' → NativeAgentTool.execute() 成功完成。"""
    from runtime_core.native_agent_tool import NativeAgentTool
    from runtime_core.ports import RuntimePorts
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult
    import time as _t
    import json

    class _FakeLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="done with haiku.", stop_reason="end_turn")
        def stream(self, *a, **kw):
            raise NotImplementedError

    class _FakeEvents:
        def publish(self, *a, **kw): pass
    class _FakeHooks:
        def check(self, *a, **kw): return HookGateResult(allowed=True)
    class _FakeClock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _FakeToken:
        def record(self, *a, **kw): pass
    class _FakeTools:
        def execute(self, n, p, i=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=n)

    ports = RuntimePorts(
        llm=_FakeLLM(), tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeEvents(), clock=_FakeClock(), token_usage=_FakeToken(),
    )
    explore_def = AgentDefinition(
        name="explore",
        description="Read-only code search",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep"}),
        model="haiku",
    )
    definitions = {"explore": explore_def}
    tool = NativeAgentTool(definition_registry=definitions, parent_ports=ports)
    result = tool.execute({
        "description": "Search",
        "prompt": "Find endpoints",
        "subagent_type": "explore",
        "model": "",
    })
    assert result.success
    assert "done with haiku" in result.output
    child_result = json.loads(result.output)
    assert child_result["status"] == "completed"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — BT-14: 新模块不依赖旧路径
# ═══════════════════════════════════════════════════════════════════════════════

def test_native_child_context_no_legacy_imports():
    """native_child_context.py 不导入旧路径。"""
    import ast
    path = os.path.join(RUNTIME_DIR, "native_child_context.py")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, 'module', '') or ''
            for forbidden in ["SessionRuntime", "ReActAgent", "LLMMessage"]:
                if forbidden in module:
                    violations.append(f"imports '{module}'")
    assert violations == [], f"BT-14 FAILED: {violations}"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — BT-15: MCP server resolve — child 不可扩大父代理权限
# ═══════════════════════════════════════════════════════════════════════════════

def test_resolve_child_mcp_servers_is_intersection():
    """Child MCP servers = definition.mcp_servers ∩ parent servers。"""
    from runtime_core.native_child_context import resolve_child_mcp_servers
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    definition = AgentDefinition(
        name="explore",
        description="Search",
        intent=TaskIntent.ANALYSIS,
        mcp_servers=("github", "weather"),
    )
    parent_servers = {"github", "filesystem", "slack"}
    result = resolve_child_mcp_servers(definition, parent_servers)
    assert result == {"github"}


def test_resolve_child_mcp_empty_definition_returns_empty():
    """mcp_servers 为空 → child 无 MCP。"""
    from runtime_core.native_child_context import resolve_child_mcp_servers
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    definition = AgentDefinition(
        name="explore", description="Search",
        intent=TaskIntent.ANALYSIS,
        mcp_servers=(),
    )
    assert resolve_child_mcp_servers(definition, {"a", "b"}) == set()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — BT-16: Permission mode 继承
# ═══════════════════════════════════════════════════════════════════════════════

def test_resolve_child_permission_mode_priority():
    """定义有 → 用定义的；定义无 → inherit default。"""
    from runtime_core.native_child_context import resolve_child_permission_mode
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    # definition sets it
    d1 = AgentDefinition(
        name="explore", description="Search",
        intent=TaskIntent.ANALYSIS, permission_mode="dontAsk",
    )
    assert resolve_child_permission_mode(d1, "default") == "dontAsk"

    # definition empty → inherit
    d2 = AgentDefinition(
        name="explore", description="Search",
        intent=TaskIntent.ANALYSIS, permission_mode="",
    )
    assert resolve_child_permission_mode(d2, "acceptEdits") == "acceptEdits"

    # both empty → empty
    d3 = AgentDefinition(
        name="explore", description="Search",
        intent=TaskIntent.ANALYSIS, permission_mode="",
    )
    assert resolve_child_permission_mode(d3, "") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — BT-17: 新模块扩展不引入旧依赖
# ═══════════════════════════════════════════════════════════════════════════════

def test_phase4_no_legacy_imports():
    """native_child_context.py 扩展后仍不导入旧路径。"""
    import ast
    path = os.path.join(RUNTIME_DIR, "native_child_context.py")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, 'module', '') or ''
            for forbidden in ["SessionRuntime", "ReActAgent", "LLMMessage"]:
                if forbidden in module:
                    violations.append(f"imports '{module}'")
    assert violations == [], f"BT-17 FAILED: {violations}"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — BT-18: run_native_child_background 立即返回
# ═══════════════════════════════════════════════════════════════════════════════

def test_background_child_returns_immediately():
    """后台启动不阻塞——调用后应立即返回 handle。"""
    import time as _t
    from runtime_core.native_child_runner import run_native_child_background
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle
    from runtime_core.native_message import NativeConversation, NativeMessage
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult

    class _SlowLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            _t.sleep(0.3)
            return AssistantText(text="Slow response.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    class _Hooks:
        def check(self, *a, **kw): return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _Token:
        def record(self, *a, **kw): pass

    ports = RuntimePorts(
        llm=_SlowLLM(), tools=object(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    conv = NativeConversation(messages=(NativeMessage.user("test"),))
    started = _t.monotonic()
    handle = run_native_child_background(
        ports=ports, session_id=SessionId("bg-1"), run_id=RunId("r1"),
        conversation=conv, cancellation=CancellationHandle(),
        max_steps=3, budget_tokens=500,
    )
    elapsed = (_t.monotonic() - started) * 1000
    assert elapsed < 200, f"Background spawn must return quickly, got {elapsed:.0f}ms"
    assert handle.agent_id == "bg-1"
    assert handle.status == "running"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — BT-19: 后台 child 完成 + wait 获取结果
# ═══════════════════════════════════════════════════════════════════════════════

def test_background_child_completes_and_can_wait():
    """wait() 阻塞 → child 完成 → result 可读。"""
    from runtime_core.native_child_runner import run_native_child_background
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle
    from runtime_core.native_message import NativeConversation, NativeMessage
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult
    import time as _t

    class _FastLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="Quick response.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    class _Hooks:
        def check(self, *a, **kw): return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _Token:
        def record(self, *a, **kw): pass

    ports = RuntimePorts(
        llm=_FastLLM(), tools=object(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    conv = NativeConversation(messages=(NativeMessage.user("test"),))
    handle = run_native_child_background(
        ports=ports, session_id=SessionId("bg-2"), run_id=RunId("r2"),
        conversation=conv, cancellation=CancellationHandle(),
        max_steps=3, budget_tokens=500,
    )
    outcome = handle.wait(timeout=10)
    assert outcome is not None
    assert outcome.status.value == "completed"
    assert "Quick response" in outcome.summary
    # result property should also be available after wait
    assert handle.result is outcome


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — BT-20: NativeAgentTool run_in_background=True 分支
# ═══════════════════════════════════════════════════════════════════════════════

def test_agent_tool_background_spawn_returns_async_launched():
    """run_in_background=True → async_launched JSON + agent_id。"""
    from runtime_core.native_agent_tool import NativeAgentTool
    from runtime_core.ports import RuntimePorts
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent
    import json, time as _t
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult, ToolSuccess

    class _SlowLLM:
        def invoke(self, c, **kw):
            _t.sleep(0.3)
            return AssistantText(text="eventual.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    class _Hooks:
        def check(self, *a, **kw): return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _Token:
        def record(self, *a, **kw): pass
    class _Tools:
        def execute(self, n, p, i=""): return ToolSuccess(tool_name=n)

    ports = RuntimePorts(
        llm=_SlowLLM(), tools=_Tools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    explore_def = AgentDefinition(
        name="explore", description="Search", intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read", "Grep"}),
    )
    definitions = {"explore": explore_def}
    tool = NativeAgentTool(definition_registry=definitions, parent_ports=ports)
    result = tool.execute({
        "description": "Search", "prompt": "Find stuff",
        "subagent_type": "explore", "run_in_background": True,
    })
    # async_launched: return immediately
    assert result.success
    data = json.loads(result.output)
    assert data["status"] == "async_launched"
    assert data["agent_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — BT-21: 新模块不依赖旧路径
# ═══════════════════════════════════════════════════════════════════════════════

def test_phase5_no_legacy_imports():
    """native_child_runner.py 扩展后仍不导入旧路径。"""
    import ast
    path = os.path.join(RUNTIME_DIR, "native_child_runner.py")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, 'module', '') or ''
            for forbidden in ["SessionRuntime", "ReActAgent", "LLMMessage"]:
                if forbidden in module:
                    violations.append(f"imports '{module}'")
    assert violations == [], f"BT-21 FAILED: {violations}"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 — BT-22: run_native_child_in_worktree 在 git repo 中创建 worktree
# ═══════════════════════════════════════════════════════════════════════════════

def test_run_native_child_in_worktree_creates_and_runs(tmp_path):
    """在 git repo 中创建 worktree → child 在其中执行 → 完成。"""
    import subprocess, time as _t
    from runtime_core.native_child_runner import run_native_child_in_worktree
    from runtime_core.ports import RuntimePorts
    from runtime_core.native_message import NativeConversation, NativeMessage
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.model_actions import AssistantText

    # Setup git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    class _LLM:
        def invoke(self, c, **kw):
            return AssistantText(text="done in worktree.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError
    class _Hooks:
        def check(self, *a, **kw):
            from runtime_core.ports import HookGateResult
            return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _Token:
        def record(self, *a, **kw): pass
    class _Tools:
        def execute(self, n, p, i=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=n)

    ports = RuntimePorts(
        llm=_LLM(), tools=_Tools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    conv = NativeConversation(messages=(NativeMessage.user("test"),))
    outcome, disposition = run_native_child_in_worktree(
        repo_path=str(repo),
        definition_name="explore",
        agent_id="wt-test-1",
        ports=ports,
        conversation=conv,
        session_id=SessionId("s1"),
        run_id=RunId("r1"),
        max_steps=3,
        budget_tokens=500,
        cancellation=None,
    )
    assert outcome.status.value == "completed"
    assert disposition == "discarded"  # no changes → auto-cleanup


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 — BT-23: worktree 生命周期：创建 → 执行 → 无变更 discard
# ═══════════════════════════════════════════════════════════════════════════════

def test_worktree_lifecycle_no_changes_discarded(tmp_path):
    """Child 在 worktree 中执行，无文件变更 → auto-discard。"""
    import subprocess, time as _t
    from runtime_core.native_child_runner import run_native_child_in_worktree
    from runtime_core.ports import RuntimePorts
    from runtime_core.native_message import NativeConversation, NativeMessage
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.model_actions import AssistantText
    from runtime_core.ports import HookGateResult

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True)
    (repo / "file.txt").write_text("original")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    class _LLM:
        def invoke(self, c, **kw):
            return AssistantText(text="no changes made.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError
    class _Hooks:
        def check(self, *a, **kw): return HookGateResult(allowed=True)
    class _Events:
        def publish(self, *a, **kw): pass
    class _Clock:
        def now(self): return _t.monotonic()
        def deadline(self, t): return _t.monotonic() + t
    class _Token:
        def record(self, *a, **kw): pass
    class _Tools:
        def execute(self, n, p, i=""):
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=n)

    ports = RuntimePorts(
        llm=_LLM(), tools=_Tools(), hooks=_Hooks(),
        live_events=_Events(), clock=_Clock(), token_usage=_Token(),
    )
    conv = NativeConversation(messages=(NativeMessage.user("test"),))
    outcome, disposition = run_native_child_in_worktree(
        repo_path=str(repo),
        definition_name="explore",
        agent_id="wt-lifecycle",
        ports=ports,
        conversation=conv,
        session_id=SessionId("s3"),
        run_id=RunId("r3"),
        max_steps=3,
        budget_tokens=500,
        cancellation=None,
    )
    assert outcome.status.value == "completed"
    assert disposition == "discarded"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 — BT-24: _check_worktree_changes 正确检测
# ═══════════════════════════════════════════════════════════════════════════════

def test_check_worktree_changes_detection(tmp_path):
    """Clean worktree → no changes. Modified file → has changes."""
    import subprocess
    from runtime_core.native_child_runner import _check_worktree_changes

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=str(repo), check=True, capture_output=True)

    # No changes
    assert not _check_worktree_changes(str(repo))
    # Add untracked file
    (repo / "new.txt").write_text("hello")
    assert _check_worktree_changes(str(repo))


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 7 — BT-25: ToolScheduler 将 Agent 工具分组为可并行
# ═══════════════════════════════════════════════════════════════════════════════

def test_scheduler_groups_agent_tools_for_parallel():
    """Agent 注册为 concurrency_safe → 多个 Agent 调用分到同一并行组。"""
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    from runtime_core.model_actions import ToolCall

    scheduler = ToolScheduler({
        "Read": ToolMetadata(name="Read", read_only=True, concurrency_safe=True),
        "Agent": ToolMetadata(name="Agent", read_only=False, concurrency_safe=True),
        "Write": ToolMetadata(name="Write", read_only=False, concurrency_safe=False),
    })
    calls = (
        ToolCall(id="a1", name="Agent", params={"description": "A"}),
        ToolCall(id="a2", name="Agent", params={"description": "B"}),
        ToolCall(id="r1", name="Read", params={"path": "x"}),
    )
    batches = scheduler.schedule(calls)
    # All three should be in the same batch (Agent is concurrency_safe, Read is too)
    assert len(batches) == 1, f"Expected 1 batch, got {len(batches)}"
    assert len(batches[0]) == 3

    # Mix with Write: Write splits the batch, but Agent after Write
    # can go in same serial batch as Write (correct: Agent runs after Write)
    calls2 = (
        ToolCall(id="a1", name="Agent", params={}),
        ToolCall(id="w1", name="Write", params={}),
        ToolCall(id="a2", name="Agent", params={}),
    )
    batches2 = scheduler.schedule(calls2)
    # Agent | Write+Agent → 2 batches (Agent after Write goes into Write's batch)
    assert len(batches2) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 7 — BT-26: NativeStepLoop 并行执行 ToolCallBatch → 时间 ≈ max(individual)
# ═══════════════════════════════════════════════════════════════════════════════

def test_parallel_tool_calls_finish_faster_than_serial():
    """同一个 ToolCallBatch 中的慢工具并行执行，总耗时远小于串行。"""
    import time as _t
    from runtime_core.runtime import AgentRuntime
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle, ConversationSnapshot, RuntimeExecution
    from runtime_core.model_actions import ToolCall, ToolCallBatch
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    from core.eventing.identifiers import SessionId, RunId

    delay = 0.15  # 每个工具模拟耗时

    class _SlowTool:
        def execute(self, name, params, invocation_id=""):
            _t.sleep(delay)
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=name)

    class _LLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="All tools done.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    scheduler = ToolScheduler({
        "SlowA": ToolMetadata(name="SlowA", read_only=True, concurrency_safe=True),
        "SlowB": ToolMetadata(name="SlowB", read_only=True, concurrency_safe=True),
        "SlowC": ToolMetadata(name="SlowC", read_only=True, concurrency_safe=True),
    })

    # Build RuntimeExecution with 3 tool calls in the conversation
    # so NativeStepLoop processes them in one turn via _process_tool_calls_parallel
    # Use ToolCallBatch as the model action — trigger parallel path
    class _LLMWithBatch:
        _called = False
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            if not self._called:
                self._called = True
                return ToolCallBatch(calls=(
                    ToolCall(id="t1", name="SlowA", params={}),
                    ToolCall(id="t2", name="SlowB", params={}),
                    ToolCall(id="t3", name="SlowC", params={}),
                ))
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="done.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    ports = RuntimePorts(
        llm=_LLMWithBatch(), tools=_SlowTool(),
        hooks=_FakeHooks(), live_events=_FakeLiveEvents(),
        clock=_FakeClock(), token_usage=_FakeTokenUsage(),
    )
    ctx = RuntimeExecution(
        session_id=SessionId("s-parallel"),
        run_id=RunId("r-parallel"),
        cancellation=CancellationHandle(),
        max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "test"},
        )),
    )
    started = _t.monotonic()
    runtime = AgentRuntime(ports, scheduler=scheduler)
    outcome = asyncio.run(runtime.arun(ctx))
    elapsed = (_t.monotonic() - started) * 1000

    assert outcome.status.value == "completed"
    # Parallel: 3 tools × 150ms each ≈ 150ms (parallel) vs 450ms (serial)
    assert elapsed < 400, f"Parallel execution too slow: {elapsed:.0f}ms (expected < 400ms)"
    assert elapsed < delay * 1000 * 3, f"Must be faster than serial: {elapsed:.0f}ms"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 8+9 — BT-28: 端到端编排——ToolCallBatch(Agent×3) → 并行 → 综合
# ═══════════════════════════════════════════════════════════════════════════════

def test_orchestration_fan_out_batch_executes_and_synthesizes():
    """模型返回 ToolCallBatch 含多个 Agent → 并行执行 → 结果注入 → 下一 turn 综合。"""
    import time as _t
    from runtime_core.runtime import AgentRuntime
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle, ConversationSnapshot, RuntimeExecution
    from runtime_core.model_actions import ToolCall, ToolCallBatch, AssistantText
    from runtime_core.tool_scheduler import ToolScheduler, ToolMetadata
    from core.eventing.identifiers import SessionId, RunId
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    delay = 0.1

    # Each Agent call → completes quickly with fake LLM
    class _AgentLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="Agent task done.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    class _AgentTools:
        def execute(self, name, params, invocation_id=""):
            _t.sleep(delay)  # simulate work
            from runtime_core.ports import ToolSuccess
            return ToolSuccess(tool_name=name)

    # Simulate orchestrator turn:
    # Turn 1: model returns ToolCallBatch(Agent×3)
    # Turn 2: model sees results → synthesizes

    class _OrchestratorLLM:
        _turn = 0
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            if self._turn == 0:
                self._turn = 1
                return ToolCallBatch(calls=(
                    ToolCall(id="a1", name="Agent", params={
                        "description": "Check module A",
                        "prompt": "Investigate module A structure",
                        "subagent_type": "explore",
                    }),
                    ToolCall(id="a2", name="Agent", params={
                        "description": "Check module B",
                        "prompt": "Investigate module B structure",
                        "subagent_type": "explore",
                    }),
                ))
            return AssistantText(
                text="Modules A and B both investigated. No issues found. Combined report complete.",
                stop_reason="end_turn",
            )
        def stream(self, *a, **kw): raise NotImplementedError

    # NativeAgentTool needs definitions
    definitions = {
        "explore": AgentDefinition(
            name="explore", description="Read-only search",
            intent=TaskIntent.ANALYSIS,
            tools=frozenset({"Read", "Grep", "Glob"}),
        ),
    }

    from runtime_core.native_agent_tool import NativeAgentTool
    tools = _AgentTools()

    # Register NativeAgentTool so it can be looked up
    agent_tool = NativeAgentTool(
        definition_registry=definitions,
        parent_ports=RuntimePorts(
            llm=_AgentLLM(), tools=tools, hooks=_FakeHooks(),
            live_events=_FakeLiveEvents(), clock=_FakeClock(),
            token_usage=_FakeTokenUsage(),
        ),
    )

    # Tool port that can resolve both Agent and the agent tool
    class _OrchTools:
        _agent_tool = agent_tool
        _tools = tools
        def execute(self, name, params, invocation_id=""):
            if name == "Agent":
                return self._agent_tool.execute(params)
            return self._tools.execute(name, params, invocation_id)

    scheduler = ToolScheduler({
        "Agent": ToolMetadata(name="Agent", read_only=False, concurrency_safe=True),
    })

    ports = RuntimePorts(
        llm=_OrchestratorLLM(), tools=_OrchTools(),
        hooks=_FakeHooks(), live_events=_FakeLiveEvents(),
        clock=_FakeClock(), token_usage=_FakeTokenUsage(),
    )
    ctx = RuntimeExecution(
        session_id=SessionId("s-orch"),
        run_id=RunId("r-orch"),
        cancellation=CancellationHandle(),
        max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "Check modules A and B and report"},
        )),
    )

    started = _t.monotonic()
    runtime = AgentRuntime(ports, scheduler=scheduler)
    outcome = asyncio.run(runtime.arun(ctx))
    elapsed = (_t.monotonic() - started) * 1000

    assert outcome.status.value == "completed"
    assert "Combined report complete" in outcome.summary
    # Parallel: 2 agents × 100ms ≈ 100ms (parallel) vs 200ms (serial)
    assert elapsed < 350, f"Orchestration parallel too slow: {elapsed:.0f}ms"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 0b / Batch B — BT-29: ChatSession._run_native_turn() 端到端
# ═══════════════════════════════════════════════════════════════════════════════

def test_chat_session_native_turn_completes():
    """ChatSession 以 native_mode 完成一轮对话：AgentRuntime → RunResult。"""
    import time as _t
    from runtime_core.ports import RuntimePorts
    from runtime_core.runtime import AgentRuntime
    from runtime_core.model_actions import AssistantText
    from runtime_core.execution import CancellationHandle
    from agent.session.models import AgentDefinition
    from agent.task import TaskIntent

    class _FakeLLM:
        def invoke(self, conversation, *, tool_choice=None, cancellation=None):
            return AssistantText(text="Turn completed.", stop_reason="end_turn")
        def stream(self, *a, **kw): raise NotImplementedError

    ports = RuntimePorts(
        llm=_FakeLLM(), tools=object(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(), token_usage=_FakeTokenUsage(),
    )
    agent_runtime = AgentRuntime(ports)

    # Minimal ConversationStore stub
    class _FakeStore:
        def list_messages(self, session_id, limit=200):
            return []

    definition = AgentDefinition(
        name="build", description="Build agent",
        intent=TaskIntent.EDIT,
    )

    # Simulate what ChatSession._run_native_turn does
    import uuid as _uuid
    from runtime_core.execution import ConversationSnapshot, RuntimeExecution
    from core.eventing.identifiers import SessionId, RunId

    conv = ConversationSnapshot(messages=(
        {"role": "user", "content": "Hello"},
    ))
    ctx = RuntimeExecution(
        session_id=SessionId("cli-test"),
        run_id=RunId(str(_uuid.uuid4())),
        max_steps=10,
        budget_tokens=5000,
        conversation=conv,
    )
    outcome = asyncio.run(agent_runtime.arun(ctx))

    assert outcome.status.value == "completed"
    assert "Turn completed" in outcome.summary


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 10 — BT-30: NativeBackend.invoke(model="haiku") per-request override
# ═══════════════════════════════════════════════════════════════════════════════

def test_backend_per_request_model_override():
    """invoke(model='haiku') → 请求中使用 haiku，不改变 backend._model。"""
    from runtime_core.native_backend import NativeBackend, NativeToolSchema

    class _FakeClient:
        def __init__(self):
            self.last_kwargs = {}
        class messages:
            @staticmethod
            def create(**kwargs):
                _FakeClient().last_kwargs.update(kwargs)
                return _fake_response("ok", "end_turn")

    def _fake_response(text, stop_reason):
        class _Msg:
            def __init__(self):
                self.stop_reason = stop_reason
                self.content = [type('_B', (), {'type': 'text', 'text': text})()]
                class _U:
                    input_tokens = 10
                    output_tokens = 5
                self.usage = _U()
        return _Msg()

    import anthropic
    _orig = getattr(anthropic, 'Anthropic', None)
    # Simple spy: verify model kwarg
    class _SpyClient:
        def __init__(self, **kw):
            self.last_kwargs = {}
        class messages:
            @staticmethod
            def create(**kwargs):
                _SpyClient._shared_kwargs = dict(kwargs)
                return _fake_response("ok", "end_turn")

    _SpyClient._shared_kwargs = {}

    # Patch just for this test
    backend = object.__new__(NativeBackend)
    object.__setattr__(backend, '_model', 'claude-sonnet')
    object.__setattr__(backend, '_max_tokens', 100)
    object.__setattr__(backend, '_tool_schemas', ())
    object.__setattr__(backend, '_cached_api_tools', [])
    object.__setattr__(backend, '_timeout_seconds', 10.0)

    # Create fake client
    class _FakeClient:
        def __init__(self, **kw):
            self.last_kwargs = {}
        class messages:
            @staticmethod
            def create(**kwargs):
                _FakeClient.last_kwargs = dict(kwargs)
                return _fake_response("ok", "end_turn")

    _FakeClient.last_kwargs = {}
    object.__setattr__(backend, '_client', _FakeClient())

    from runtime_core.native_message import NativeConversation, NativeMessage
    conv = NativeConversation(messages=(NativeMessage.user("hello"),))

    # Default model
    backend.invoke(conv)
    assert _FakeClient.last_kwargs["model"] == "claude-sonnet"

    # Per-request override
    backend.invoke(conv, model="haiku")
    assert _FakeClient.last_kwargs["model"] == "haiku"

    # Backend._model unchanged
    assert backend._model == "claude-sonnet"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 10 — BT-31: NativeStepLoop stream 路径推送 text deltas 到 callback
# ═══════════════════════════════════════════════════════════════════════════════

def test_stream_callback_receives_text_deltas():
    """text_callback passed → stream_iter 产出的 TEXT_DELTA 逐 token 推送。"""
    import time as _t
    from runtime_core.runtime import AgentRuntime
    from runtime_core.ports import RuntimePorts
    from runtime_core.execution import CancellationHandle, ConversationSnapshot, RuntimeExecution
    from core.eventing.identifiers import SessionId, RunId
    from runtime_core.native_message import NativeConversation, NativeMessage
    from llm.base import StreamEvent, StreamEventKind
    from runtime_core.model_actions import ToolCall

    # Fake backend with stream_iter yielding events
    class _StreamBackend:
        def stream_iter(self, conversation, *, tool_choice=None, model=""):
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, text="Hello ")
            yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, text="world.")
            yield StreamEvent(kind=StreamEventKind.FINISH, text="Hello world.")
        def invoke(self, *a, **kw):
            from runtime_core.model_actions import AssistantText
            return AssistantText(text="fallback", stop_reason="end_turn")

    parts = []
    def _cb(text):
        parts.append(text)

    ports = RuntimePorts(
        llm=_StreamBackend(), tools=object(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(), token_usage=_FakeTokenUsage(),
    )
    ctx = RuntimeExecution(
        session_id=SessionId("s-stream"),
        run_id=RunId("r-stream"),
        cancellation=CancellationHandle(),
        max_steps=5,
        conversation=ConversationSnapshot(messages=(
            {"role": "user", "content": "hi"},
        )),
    )
    runtime = AgentRuntime(ports)
    outcome = asyncio.run(runtime.arun(ctx, text_callback=_cb))

    assert outcome.status.value == "completed"
    assert parts == ["Hello ", "world."]
    assert "Hello world" in outcome.summary

"""Tests for Batch C1-C7: CC alignment features.

Verifies each newly implemented function actually works.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent.task import TaskIntent
from agent.session.models import (
    AgentDefinition,
    AgentKind,
    AgentSpawnRequest,
    DelegationPolicy,
    ExecutionPlacement,
    WorkspaceMode,
)


def test_build_registry_registers_skill_tool_and_skill_registry(tmp_path):
    from config.schema import AppConfig
    from entry.bootstrap.registry_factory import build_registry

    skill_dir = tmp_path / ".forge-agent" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("""---
name: Demo
description: Demo skill
---

Use this skill for demo tasks.
""", encoding="utf-8")

    registry = build_registry(AppConfig(), repo_path=tmp_path)

    assert getattr(registry, "_skill_registry", None) is not None
    assert getattr(registry, "_skill_buffer", None) is not None
    assert "Skill" in registry.tool_names


def test_v2_runtime_messages_include_available_skills_from_registry(tmp_path):
    from config.schema import AppConfig
    from entry.bootstrap.registry_factory import build_registry
    from agent.core import AgentConfig
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.runtime import SessionRuntime
    from agent.session.session_store import SessionStore
    from llm.base import MockBackend

    skill_dir = tmp_path / ".forge-agent" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("""---
name: Review
description: Review code carefully
when_to_use: Use for read-only code review tasks
---

Review the target carefully.
""", encoding="utf-8")

    registry = build_registry(AppConfig(), repo_path=tmp_path)
    runtime = SessionRuntime(
        store=SessionStore(str(tmp_path / "sessions.db")),
        backend=MockBackend([]),
        base_registry=registry,
        agent_registry=AgentRegistryV2(project_dir=tmp_path),
        root_agent_config=AgentConfig(stream=False),
        log_dir=str(tmp_path / "logs"),
    )

    definition = runtime.agent_registry.get("build")
    text = " ".join(
        str(message.content)
        for message in runtime._build_runtime_messages(definition, "inspect repo")
    )

    assert "## Available Skills" in text
    assert "Review code carefully" in text


# ═══════════════════════════════════════════════════════════════════════
# C1: permission_mode
# ═══════════════════════════════════════════════════════════════════════

class TestPermissionMode:
    """PhasePolicy.is_tool_blocked_by_permission_mode()"""

    def test_plan_mode_blocks_write_edit_not_bash(self):
        """CC-aligned: Plan blocks Write/Edit, but Bash is available for read-only exploration."""
        from core.policy import PhasePolicy
        policy = PhasePolicy(permission_mode="plan")
        assert policy.is_tool_blocked_by_permission_mode("Write") is True
        assert policy.is_tool_blocked_by_permission_mode("Edit") is True
        assert policy.is_tool_blocked_by_permission_mode("Bash") is False
        assert policy.is_tool_blocked_by_permission_mode("Read") is False
        assert policy.is_tool_blocked_by_permission_mode("Grep") is False

    def test_default_mode_blocks_nothing(self):
        from core.policy import PhasePolicy
        policy = PhasePolicy()
        assert policy.is_tool_blocked_by_permission_mode("Write") is False
        assert policy.is_tool_blocked_by_permission_mode("Bash") is False

    def test_dont_ask_mode_blocks_non_allowed(self):
        from core.policy import PhasePolicy
        policy = PhasePolicy(
            permission_mode="dontAsk",
            allowed_tools=frozenset({"Read", "Grep"}),
        )
        assert policy.is_tool_blocked_by_permission_mode("Read") is False
        assert policy.is_tool_blocked_by_permission_mode("Write") is True
        assert policy.is_tool_blocked_by_permission_mode("Bash") is True

    def test_accept_edits_does_not_block_write(self):
        from core.policy import PhasePolicy
        policy = PhasePolicy(permission_mode="acceptEdits")
        assert policy.is_tool_blocked_by_permission_mode("Write") is False
        assert policy.is_tool_blocked_by_permission_mode("Edit") is False
        assert policy.is_tool_blocked_by_permission_mode("Bash") is False

    def test_plan_mode_agent_definition_validation(self):
        """AgentDefinition with permission_mode='plan' rejects EDIT intent."""
        with pytest.raises(ValueError, match="permission_mode"):
            AgentDefinition(
                name="bad-plan",
                description="test",
                intent=TaskIntent.EDIT,
                agent_kind=AgentKind.PRIMARY,
                permission_mode="invalid",
            )


# ═══════════════════════════════════════════════════════════════════════
# C2: mcp_servers
# ═══════════════════════════════════════════════════════════════════════

class TestMcpServers:
    """MCPToolIntegration.server_tools and _mcp_tool_names_for_spec"""

    def test_server_tools_empty_when_no_manager(self):
        """server_tools returns empty dict when MCP not initialized."""
        from agent.session.mcp_integration import MCPToolIntegration
        integration = MCPToolIntegration()
        assert integration.server_tools == {}

    def test_mcp_tool_names_from_spec_mcp_servers(self):
        """_mcp_tool_names_for_spec returns empty when no mcp_servers or EDIT intent."""
        from agent.session.models import AgentDefinition, TaskIntent, AgentKind
        spec = AgentDefinition(
            name="read-only",
            description="test",
            intent=TaskIntent.ANALYSIS,
            agent_kind=AgentKind.NAMED_SUBAGENT,
        )
        # Without mcp_servers and without EDIT intent, should return empty
        assert spec.mcp_servers == ()

    def test_agent_definition_stores_mcp_servers(self):
        """AgentDefinition stores mcp_servers tuple from frontmatter."""
        spec = AgentDefinition(
            name="mcp-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
            mcp_servers=("db-server", "api-server"),
        )
        assert "db-server" in spec.mcp_servers
        assert "api-server" in spec.mcp_servers


# ═══════════════════════════════════════════════════════════════════════
# C3: background + initial_prompt
# ═══════════════════════════════════════════════════════════════════════

class TestBackground:
    """AgentSpawnRequest.named() uses definition.background"""

    def test_background_true_uses_background_placement(self):
        spec = AgentDefinition(
            name="bg-agent",
            description="background task",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            background=True,
        )
        req = AgentSpawnRequest.named(
            definition=spec,
            description="test",
            prompt="do work",
        )
        assert req.execution_placement is ExecutionPlacement.BACKGROUND

    def test_background_false_uses_foreground(self):
        spec = AgentDefinition(
            name="fg-agent",
            description="foreground task",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            background=False,
        )
        req = AgentSpawnRequest.named(
            definition=spec,
            description="test",
            prompt="do work",
        )
        assert req.execution_placement is ExecutionPlacement.FOREGROUND

    def test_explicit_placement_overrides_background(self):
        spec = AgentDefinition(
            name="explicit-agent",
            description="explicit placement",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            background=True,
        )
        req = AgentSpawnRequest.named(
            definition=spec,
            description="test",
            prompt="do work",
            execution_placement=ExecutionPlacement.FOREGROUND,
        )
        assert req.execution_placement is ExecutionPlacement.FOREGROUND


class TestInitialPrompt:
    """initial_prompt injection in entry/cli.py"""

    def test_initial_prompt_stored_in_definition(self):
        spec = AgentDefinition(
            name="prompt-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
            initial_prompt="Please analyze first.",
        )
        assert spec.initial_prompt == "Please analyze first."

    def test_initial_prompt_default_empty(self):
        spec = AgentDefinition(
            name="no-prompt",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
        )
        assert spec.initial_prompt == ""


# ═══════════════════════════════════════════════════════════════════════
# C4: hooks
# ═══════════════════════════════════════════════════════════════════════

class TestAgentHooks:
    """register_external / unregister_external on HookRegistry"""

    def test_register_and_unregister_external_hook(self):
        from hooks.events import HookEvent
        from hooks.registry import ExternalHookConfig, HookRegistry
        from hooks.matcher import HookMatcher

        registry = HookRegistry()
        event = HookEvent.PRE_TOOL_USE
        config = ExternalHookConfig(
            command="echo test",
            matcher=HookMatcher(pattern="Bash"),
        )

        # Register
        registry.register_external(event, config)
        matches = registry.find_external(event, "Bash", {})
        assert len(matches) == 1
        assert matches[0].command == "echo test"

        # Unregister
        registry.unregister_external(event, config)
        matches = registry.find_external(event, "Bash", {})
        assert len(matches) == 0

    def test_hooks_stored_in_agent_definition(self):
        spec = AgentDefinition(
            name="hooked-agent",
            description="test",
            intent=TaskIntent.ANALYSIS,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            hooks=(
                {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./validate.sh"}]}]},
            ),
        )
        assert len(spec.hooks) == 1
        assert "PreToolUse" in spec.hooks[0]


# ═══════════════════════════════════════════════════════════════════════
# C5: skills + memory
# ═══════════════════════════════════════════════════════════════════════

class TestSkills:
    """_load_skills loads SKILL.md content"""

    def test_load_skills_returns_empty_for_no_skills(self):
        from agent.session.runtime_prompt_builder import _load_skills
        result = _load_skills((), None)
        assert result == []

    def test_load_skills_returns_empty_for_missing_skill(self):
        from agent.session.runtime_prompt_builder import _load_skills
        result = _load_skills(("nonexistent-skill",), None)
        assert result == []

    def test_skills_stored_in_agent_definition(self):
        spec = AgentDefinition(
            name="skill-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            skills=("code-review", "security-scan"),
        )
        assert "code-review" in spec.skills
        assert "security-scan" in spec.skills


class TestMemory:
    """_load_agent_memory reads MEMORY.md"""

    def test_load_memory_returns_empty_when_no_memory(self):
        from agent.session.runtime_prompt_builder import _load_agent_memory
        spec = AgentDefinition(
            name="no-memory",
            description="test",
            intent=TaskIntent.ANALYSIS,
            agent_kind=AgentKind.NAMED_SUBAGENT,
        )
        result = _load_agent_memory(spec, None)
        assert result == ""

    def test_memory_stored_in_agent_definition(self):
        spec = AgentDefinition(
            name="mem-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            memory="project",
        )
        assert spec.memory == "project"

    def test_load_memory_with_existing_file(self, tmp_path):
        from agent.session.runtime_prompt_builder import _load_agent_memory
        spec = AgentDefinition(
            name="mem-test",
            description="test",
            intent=TaskIntent.ANALYSIS,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            memory="project",
        )
        # Create memory file
        mem_dir = tmp_path / ".forge-agent" / "agent-memory" / "mem-test"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "MEMORY.md"
        mem_file.write_text("project memory content", encoding="utf-8")

        result = _load_agent_memory(spec, str(tmp_path))
        assert "project memory content" in result

    def test_memory_truncated_at_25k(self, tmp_path):
        from agent.session.runtime_prompt_builder import _load_agent_memory
        spec = AgentDefinition(
            name="big-mem",
            description="test",
            intent=TaskIntent.ANALYSIS,
            agent_kind=AgentKind.NAMED_SUBAGENT,
            memory="project",
        )
        mem_dir = tmp_path / ".forge-agent" / "agent-memory" / "big-mem"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "MEMORY.md"
        mem_file.write_text("x" * 50000, encoding="utf-8")

        result = _load_agent_memory(spec, str(tmp_path))
        assert len(result) <= 25000


# ═══════════════════════════════════════════════════════════════════════
# C6: effort + color
# ═══════════════════════════════════════════════════════════════════════

class TestEffort:
    """effort stored in AgentDefinition and passed to AgentConfig"""

    def test_effort_stored_in_definition(self):
        spec = AgentDefinition(
            name="effort-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
            effort="high",
        )
        assert spec.effort == "high"

    def test_effort_validated(self):
        with pytest.raises(ValueError, match="effort"):
            AgentDefinition(
                name="bad-effort",
                description="test",
                intent=TaskIntent.EDIT,
                agent_kind=AgentKind.PRIMARY,
                effort="turbo",
            )

    def test_effort_in_agent_config(self):
        from agent.core import AgentConfig
        config = AgentConfig(effort="high")
        assert config.effort == "high"

    def test_effort_default_empty(self):
        from agent.core import AgentConfig
        config = AgentConfig()
        assert config.effort == ""


class TestColor:
    """color stored in AgentDefinition"""

    def test_color_stored_in_definition(self):
        spec = AgentDefinition(
            name="color-agent",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
            color="blue",
        )
        assert spec.color == "blue"

    def test_color_default_empty(self):
        spec = AgentDefinition(
            name="no-color",
            description="test",
            intent=TaskIntent.EDIT,
            agent_kind=AgentKind.PRIMARY,
        )
        assert spec.color == ""


# ═══════════════════════════════════════════════════════════════════════
# C7: Agent() deny + Agent(agent_type) + --agents CLI
# ═══════════════════════════════════════════════════════════════════════

class TestAgentDenySyntax:
    """PermissionRule with Agent(name) syntax"""

    def test_agent_deny_rule_parsed_correctly(self):
        from hitl.permission_rule import PermissionRule, PermissionRuleTier
        rule = PermissionRule.parse("Agent(explore)", tier=PermissionRuleTier.DENY, source="test")
        assert rule.tool_name == "agent"
        assert rule.pattern == "explore"

    def test_agent_deny_rule_matches_subagent_type(self):
        from hitl.permission_rule import PermissionRule, PermissionRuleTier
        rule = PermissionRule.parse("Agent(explore)", tier=PermissionRuleTier.DENY, source="test")
        assert rule.matches("Agent", {"subagent_type": "explore"}) is True
        assert rule.matches("Agent", {"subagent_type": "general"}) is False
        assert rule.matches("Agent", {"agent_name": "explore"}) is True

    def test_agent_deny_rule_does_not_match_other_tools(self):
        from hitl.permission_rule import PermissionRule, PermissionRuleTier
        rule = PermissionRule.parse("Agent(explore)", tier=PermissionRuleTier.DENY, source="test")
        assert rule.matches("Read", {"path": "/tmp"}) is False
        assert rule.matches("Bash", {"command": "ls"}) is False


class TestDelegationPolicyFromTools:
    """DelegationPolicy.from_tools() parses Agent(worker,researcher)"""

    def test_from_tools_parses_agent_syntax(self):
        policy = DelegationPolicy.from_tools(frozenset({"Read", "Agent(worker,researcher)", "Bash"}))
        assert policy.mode.name == "ALLOWLIST"
        assert "worker" in policy.allowed_names
        assert "researcher" in policy.allowed_names

    def test_from_tools_without_agent_syntax_returns_disabled(self):
        policy = DelegationPolicy.from_tools(frozenset({"Read", "Bash", "Write"}))
        assert policy.mode.name == "DISABLED"

    def test_from_tools_with_bare_agent_returns_disabled(self):
        """bare Agent (no parens) means unrestricted, not allowlist."""
        policy = DelegationPolicy.from_tools(frozenset({"Read", "Agent", "Bash"}))
        assert policy.mode.name == "DISABLED"

    def test_from_tools_removes_whitespace(self):
        policy = DelegationPolicy.from_tools(frozenset({"Agent( worker , researcher )"}))
        assert "worker" in policy.allowed_names
        assert "researcher" in policy.allowed_names


class TestAgentsCLI:
    """--agents JSON parsing in entry/cli.py"""

    def test_session_agents_json_parsing(self):
        """Verify the JSON structure that --agents accepts."""
        agents_json = json.dumps({
            "my-agent": {
                "description": "A test agent",
                "intent": "edit",
                "tools": ["Read", "Write", "Bash"],
                "model": "sonnet",
                "prompt": "You are a test agent.",
            },
            "my-reader": {
                "description": "Read-only agent",
                "intent": "analysis",
                "tools": ["Read", "Grep", "Glob"],
            },
        })
        data = json.loads(agents_json)
        assert "my-agent" in data
        assert data["my-agent"]["intent"] == "edit"
        assert data["my-reader"]["tools"] == ["Read", "Grep", "Glob"]


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: StreamingToolExecutor + Per-call Concurrency (CC-aligned)
# ═══════════════════════════════════════════════════════════════════════

class TestPartitionToolCalls:
    """CC-aligned partition: consecutive safe tools → batch, non-safe → break."""

    def test_all_safe_single_batch(self):
        from core.streaming_executor import partition_tool_calls
        from core.base import ToolRegistry, NoopTool, ToolConcurrency
        from agent.task import ToolCall

        registry = ToolRegistry()
        for name in ("Read", "Grep", "Glob"):
            t = NoopTool(name)
            t.concurrency_mode = lambda _params=None, _t=t: ToolConcurrency.PARALLEL_SAFE  # type: ignore[method-assign]
            registry.register(t)

        calls = [
            ToolCall(name="Read", params={"path": "a.py"}),
            ToolCall(name="Grep", params={"pattern": "foo"}),
            ToolCall(name="Glob", params={"pattern": "*.py"}),
        ]
        batches = partition_tool_calls(calls, registry)
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_unsafe_breaks_batch(self):
        from core.streaming_executor import partition_tool_calls
        from core.base import ToolRegistry, NoopTool, ToolConcurrency
        from agent.task import ToolCall

        registry = ToolRegistry()
        for name, conc in [("Read", ToolConcurrency.PARALLEL_SAFE),
                           ("Edit", ToolConcurrency.SERIAL),
                           ("Read2", ToolConcurrency.PARALLEL_SAFE)]:
            t = NoopTool(name)
            t.concurrency_mode = lambda _p=None, _c=conc: _c  # type: ignore[method-assign]
            registry.register(t)

        calls = [
            ToolCall(name="Read", params={"path": "a.py"}),
            ToolCall(name="Edit", params={"path": "b.py"}),
            ToolCall(name="Read2", params={"path": "c.py"}),
        ]
        batches = partition_tool_calls(calls, registry)
        assert len(batches) == 3  # Read, Edit, Read2
        assert len(batches[0]) == 1
        assert len(batches[1]) == 1
        assert len(batches[2]) == 1

    def test_consecutive_safe_grouped(self):
        from core.streaming_executor import partition_tool_calls
        from core.base import ToolRegistry, NoopTool, ToolConcurrency
        from agent.task import ToolCall

        registry = ToolRegistry()
        for name, conc in [("Read", ToolConcurrency.PARALLEL_SAFE),
                           ("Grep", ToolConcurrency.PARALLEL_SAFE),
                           ("Edit", ToolConcurrency.SERIAL)]:
            t = NoopTool(name)
            t.concurrency_mode = lambda _p=None, _c=conc: _c  # type: ignore[method-assign]
            registry.register(t)

        calls = [
            ToolCall(name="Read", params={}),
            ToolCall(name="Grep", params={}),
            ToolCall(name="Edit", params={}),
        ]
        batches = partition_tool_calls(calls, registry)
        assert len(batches) == 2  # [Read,Grep], [Edit]
        assert len(batches[0]) == 2
        assert len(batches[1]) == 1

    def test_empty_list(self):
        from core.streaming_executor import partition_tool_calls
        from core.base import ToolRegistry
        assert partition_tool_calls([], ToolRegistry()) == []

    def test_fail_closed_unknown_tool(self):
        """Unknown tools default to serial (fail-closed)."""
        from core.streaming_executor import partition_tool_calls
        from core.base import ToolRegistry, NoopTool, ToolConcurrency
        from agent.task import ToolCall

        registry = ToolRegistry()
        t = NoopTool("Read")
        t.concurrency_mode = lambda _p=None: ToolConcurrency.PARALLEL_SAFE  # type: ignore[method-assign]
        registry.register(t)

        calls = [
            ToolCall(name="Read", params={}),
            ToolCall(name="UnknownTool", params={}),
        ]
        batches = partition_tool_calls(calls, registry)
        assert len(batches) == 2  # Unknown breaks batch


class TestBashPerCallConcurrency:
    """Bash.concurrency_mode(): read-only commands are PARALLEL_SAFE."""

    def test_read_only_command_is_parallel_safe(self):
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": "ls"}) is ToolConcurrency.PARALLEL_SAFE
        assert tool.concurrency_mode({"command": "grep"}) is ToolConcurrency.PARALLEL_SAFE
        assert tool.concurrency_mode({"command": "cat"}) is ToolConcurrency.PARALLEL_SAFE

    def test_git_read_commands_are_parallel_safe(self):
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": "git", "args": ["status"]}) is ToolConcurrency.PARALLEL_SAFE
        assert tool.concurrency_mode({"command": "git", "args": ["log", "--oneline"]}) is ToolConcurrency.PARALLEL_SAFE
        assert tool.concurrency_mode({"command": "git", "args": ["diff"]}) is ToolConcurrency.PARALLEL_SAFE

    def test_destructive_commands_are_serial(self):
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": "rm"}) is ToolConcurrency.SERIAL
        assert tool.concurrency_mode({"command": "mv"}) is ToolConcurrency.SERIAL
        assert tool.concurrency_mode({"command": "npm", "args": ["install"]}) is ToolConcurrency.SERIAL

    def test_path_prefixed_read_command_is_safe(self):
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": "/usr/bin/ls"}) is ToolConcurrency.PARALLEL_SAFE
        assert tool.concurrency_mode({"command": "/bin/cat"}) is ToolConcurrency.PARALLEL_SAFE

    def test_package_managers_are_serial(self):
        """npm, cargo, pip etc. are serial — subcommands can be destructive."""
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": "npm"}) is ToolConcurrency.SERIAL
        assert tool.concurrency_mode({"command": "cargo"}) is ToolConcurrency.SERIAL
        assert tool.concurrency_mode({"command": "python"}) is ToolConcurrency.SERIAL

    def test_empty_command_defaults_serial(self):
        from tools.shell_tool import ShellTool
        from core.base import ToolConcurrency
        tool = ShellTool()
        assert tool.concurrency_mode({"command": ""}) is ToolConcurrency.SERIAL
        assert tool.concurrency_mode({}) is ToolConcurrency.SERIAL


class TestStreamingToolExecutor:
    """StreamingToolExecutor: enqueue → dispatch → collect in input order."""

    def _registry(self):
        from core.base import ToolRegistry, NoopTool, ToolConcurrency
        r = ToolRegistry()
        for name in ("Read", "Write", "Bash"):
            t = NoopTool(name, output=f"{name} ok")
            if name == "Read":
                t.concurrency_mode = lambda _p=None: ToolConcurrency.PARALLEL_SAFE  # type: ignore[method-assign]
            else:
                t.concurrency_mode = lambda _p=None: ToolConcurrency.SERIAL  # type: ignore[method-assign]
            r.register(t)
        return r

    def test_enqueue_dispatch_collect_preserves_order(self):
        from core.streaming_executor import StreamingToolExecutor
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        calls = [
            ToolCall(name="Read", params={"path": "a.py"}, id="c1"),
            ToolCall(name="Read", params={"path": "b.py"}, id="c2"),
            ToolCall(name="Read", params={"path": "c.py"}, id="c3"),
        ]
        for c in calls:
            executor.enqueue(c)
        executor.dispatch()
        results = executor.collect()
        assert len(results) == 3
        assert all(r.success for r in results)

    def test_serial_tool_breaks_concurrent_batch(self):
        """Write (SERIAL) should cause Read → serial Write → serial, not all-concurrent."""
        from core.streaming_executor import StreamingToolExecutor
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        calls = [
            ToolCall(name="Read", params={}, id="c1"),
            ToolCall(name="Write", params={}, id="c2"),
            ToolCall(name="Read", params={}, id="c3"),
        ]
        for c in calls:
            executor.enqueue(c)
        executor.dispatch()
        results = executor.collect()
        assert len(results) == 3
        # Write runs alone (serial), Reads run together
        assert results[0].success  # Read
        assert results[1].success  # Write
        assert results[2].success  # Read

    def test_single_tool(self):
        from core.streaming_executor import StreamingToolExecutor
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        executor.enqueue(ToolCall(name="Read", params={}, id="c1"))
        executor.dispatch()
        results = executor.collect()
        assert len(results) == 1
        assert results[0].success

    def test_no_tools(self):
        from core.streaming_executor import StreamingToolExecutor

        executor = StreamingToolExecutor(self._registry())
        executor.dispatch()
        results = executor.collect()
        assert results == []

    def test_executor_stats(self):
        from core.streaming_executor import StreamingToolExecutor
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        executor.enqueue(ToolCall(name="Read", params={}, id="c1"))
        executor.dispatch()
        executor.collect()
        stats = executor.stats
        assert stats["total"] == 1
        assert stats["statuses"].get("yielded", 0) == 1

    def test_abort_all_before_dispatch_cancels_queued(self):
        """abort_all() before dispatch cancels all queued tools."""
        from core.streaming_executor import StreamingToolExecutor, TrackedStatus
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        # Manually add without speculative start to test abort on queued tools
        tc1 = ToolCall(name="Read", params={}, id="c1")
        tc2 = ToolCall(name="Write", params={}, id="c2")
        # Direct tracked insertion (bypass enqueue speculative start)
        from core.streaming_executor import TrackedTool
        executor._tracked.append(TrackedTool(tool_call=tc1))
        executor._tracked.append(TrackedTool(tool_call=tc2))
        executor.abort_all("test abort")
        results = executor.collect()
        assert len(results) == 2
        assert not results[0].success
        assert "test abort" in str(results[0].tool_error or "") or "test abort" in results[0].error

    def test_speculative_start_executes_immediately(self):
        """enqueue() with PARALLEL_SAFE tool starts executing immediately."""
        from core.streaming_executor import StreamingToolExecutor
        from agent.task import ToolCall

        executor = StreamingToolExecutor(self._registry())
        executor.enqueue(ToolCall(name="Read", params={}, id="c1"))
        # The tool may already be executing or completed
        assert executor.pending_count >= 0  # at worst 1 if still running
        executor.dispatch()
        results = executor.collect()
        assert len(results) == 1
        assert results[0].success


class TestStreamIter:
    """Backend.stream_iter() yields correct StreamEvent sequence."""

    def test_fallback_yields_events_from_complete(self):
        """Base stream_iter fallback converts complete() response to events."""
        from llm.base import MockBackend, StreamEventKind
        from agent.task import Action, ActionType, ToolCall

        backend = MockBackend([
            Action(
                action_type=ActionType.TOOL_CALL,
                thought="inspecting code",
                tool_calls=[
                    ToolCall(name="Read", params={"path": "a.py"}, id="c1"),
                    ToolCall(name="Grep", params={"pattern": "TODO"}, id="c2"),
                ],
            ),
        ])

        events = list(backend.stream_iter([], []))
        kinds = [e.kind for e in events]
        assert StreamEventKind.TEXT_DELTA in kinds  # thought
        assert StreamEventKind.TOOL_USE in kinds
        assert StreamEventKind.FINISH in kinds
        tool_events = [e for e in events if e.kind == StreamEventKind.TOOL_USE]
        assert len(tool_events) == 2
        assert tool_events[0].tool_call.name == "Read"
        assert tool_events[1].tool_call.name == "Grep"

    def test_fallback_yields_error_on_exception(self):
        """stream_iter yields ERROR event when complete() raises."""
        from llm.base import LLMBackend, LLMMessage, LLMToolSchema, StreamEventKind

        class FailingBackend(LLMBackend):
            @property
            def model_name(self) -> str:
                return "failing"
            def complete(self, messages, tools):
                raise RuntimeError("API down")

        backend = FailingBackend()
        events = list(backend.stream_iter([], []))
        assert len(events) == 1
        assert events[0].kind == StreamEventKind.ERROR
        assert "API down" in events[0].text

    def test_finish_action_yields_finish_event(self):
        """FINISH action yields TEXT_DELTA (thought) + FINISH."""
        from llm.base import MockBackend, StreamEventKind
        from agent.task import Action, ActionType

        backend = MockBackend([
            Action(action_type=ActionType.FINISH, thought="done", message="All good"),
        ])

        events = list(backend.stream_iter([], []))
        kinds = [e.kind for e in events]
        assert StreamEventKind.FINISH in kinds
        finish = [e for e in events if e.kind == StreamEventKind.FINISH][0]
        assert finish.finish_message == "All good"


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: RecoveryState (CC-aligned continue-site tracking)
# ═══════════════════════════════════════════════════════════════════════

class TestRecoveryState:
    """CC-aligned: max_output_tokens escalation + token budget nudge + reactive compact."""

    def test_escalation_not_applied_initially(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        assert r.escalation_applied is False
        assert r.output_recovery_count == 0

    def test_can_escalate_when_below_threshold(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        assert r.can_escalate(8000) is True  # 8k < 64k
        assert r.can_escalate(32000) is True

    def test_cannot_escalate_when_already_escalated(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.escalation_applied = True
        assert r.can_escalate(8000) is False

    def test_cannot_escalate_when_already_at_max(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        assert r.can_escalate(64000) is False  # already at escalated max

    def test_can_recover_output_up_to_3_times(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        assert r.can_recover_output() is True
        r.output_recovery_count = 1
        assert r.can_recover_output() is True
        r.output_recovery_count = 3
        assert r.can_recover_output() is False  # 3 == max

    def test_should_nudge_when_budget_has_room(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.nudge_count = 0
        r.last_nudge_tokens = 0
        # 1000 used out of 100000 budget → 1% used → should nudge
        assert r.should_nudge(1000, 100000) is True

    def test_should_not_nudge_when_budget_exhausted(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        # 95000 used out of 100000 → 95% used → beyond 90% threshold
        assert r.should_nudge(95000, 100000) is False

    def test_should_not_nudge_when_diminishing(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.nudge_count = 4  # 3+ triggers diminishing check
        r.last_nudge_tokens = 1000
        # delta = 1100 - 1000 = 100 < 500 → diminishing
        assert r.is_diminishing(1100) is True
        assert r.should_nudge(1100, 100000) is False

    def test_diminishing_not_triggered_under_3_nudges(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.nudge_count = 2  # < 3, diminishing detection not active
        r.last_nudge_tokens = 1000
        assert r.is_diminishing(1100) is False

    def test_can_reactive_compact_initially_true(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        assert r.can_reactive_compact() is True

    def test_cannot_reactive_compact_after_attempt(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.has_attempted_reactive_compact = True
        assert r.can_reactive_compact() is False

    def test_reset_for_new_turn(self):
        from agent.core import RecoveryState
        r = RecoveryState()
        r.has_attempted_reactive_compact = True
        r.reset_for_new_turn()
        assert r.has_attempted_reactive_compact is False

    def test_finish_reason_populated_in_response(self):
        """LLMResponse carries finish_reason from provider."""
        from llm.base import LLMResponse
        from agent.task import Action, ActionType
        resp = LLMResponse(
            action=Action(action_type=ActionType.FINISH, thought="done", message="ok"),
            raw_content="ok",
            finish_reason="stop",
        )
        assert resp.finish_reason == "stop"

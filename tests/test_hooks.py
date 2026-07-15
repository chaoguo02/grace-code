"""
tests/test_hooks.py

Tests for the hooks/ package: events, matcher, executor, registry, dispatcher.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hooks.events import (
    BLOCKABLE_EVENTS, HookContext, HookEvent, SessionStartSource,
)
from hooks.matcher import HookMatcher
from hooks.protocol import (
    DispatchResult,
    ExitCode,
    HookControl,
    HookDecision,
    HookOutput,
    HookResult,
)
from hooks.registry import ExternalHookConfig, HookRegistry, InternalHook
from hooks.dispatcher import HookDispatcher


# ─── HookEvent tests ──────────────────────────────────────────────────────────

class TestHookEvent:
    def test_event_values(self):
        assert HookEvent.PRE_TOOL_USE == "PreToolUse"
        assert HookEvent.POST_TOOL_USE == "PostToolUse"
        assert HookEvent.STOP == "Stop"
        assert HookEvent.SESSION_START == "SessionStart"
        assert HookEvent.SUBAGENT_START == "SubagentStart"
        assert HookEvent.SUBAGENT_STOP == "SubagentStop"

    def test_blockable_events(self):
        assert HookEvent.PRE_TOOL_USE in BLOCKABLE_EVENTS
        assert HookEvent.USER_PROMPT_SUBMIT in BLOCKABLE_EVENTS
        assert HookEvent.STOP in BLOCKABLE_EVENTS
        assert HookEvent.SUBAGENT_STOP in BLOCKABLE_EVENTS
        assert HookEvent.POST_TOOL_USE not in BLOCKABLE_EVENTS


# ─── HookContext tests ────────────────────────────────────────────────────────

class TestHookContext:
    def test_to_dict(self):
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            session_id="sess1",
            tool_name="shell",
            tool_input={"cmd": "ls"},
        )
        d = ctx.to_dict()
        assert d["event"] == "PreToolUse"
        assert d["hook_event_name"] == "PreToolUse"
        assert d["session_id"] == "sess1"
        assert d["tool_name"] == "shell"
        assert d["tool_input"] == {"cmd": "ls"}
        assert "timestamp" in d

    def test_defaults(self):
        ctx = HookContext(event=HookEvent.STOP)
        assert ctx.tool_name == ""
        assert ctx.tool_input == {}
        assert ctx.tool_output is None
        assert ctx.user_input == ""

    def test_messages_are_serialized(self):
        messages = [{"role": "user", "content": "hi"}]
        ctx = HookContext(event=HookEvent.STOP, messages=messages)
        assert ctx.to_dict()["messages"] == messages

    def test_subagent_fields_and_matcher_subject_are_typed(self):
        ctx = HookContext(
            event=HookEvent.SUBAGENT_STOP,
            session_id="parent",
            agent_id="child",
            agent_type="explore",
            last_assistant_message="done",
        )

        assert ctx.matcher_subject == "explore"
        assert ctx.to_dict() == {
            "event": "SubagentStop",
            "hook_event_name": "SubagentStop",
            "session_id": "parent",
            "timestamp": ctx.timestamp,
            "agent_id": "child",
            "agent_type": "explore",
            "last_assistant_message": "done",
            "stop_hook_active": False,
        }

    def test_session_start_matches_typed_source(self):
        ctx = HookContext(
            event=HookEvent.SESSION_START,
            session_start_source=SessionStartSource.RESUME,
        )

        assert ctx.matcher_subject == "resume"


def test_dispatcher_matches_subagent_hooks_by_agent_type(tmp_path):
    calls = []
    registry = HookRegistry()
    registry.register_internal(
        HookEvent.SUBAGENT_START,
        InternalHook(
            callback=calls.append,
            matcher=HookMatcher(pattern="explore"),
        ),
    )
    dispatcher = HookDispatcher(registry, cwd=str(tmp_path))

    dispatcher.dispatch(
        HookEvent.SUBAGENT_START,
        HookContext(
            event=HookEvent.SUBAGENT_START,
            session_id="parent",
            agent_id="child",
            agent_type="explore",
        ),
    )

    assert len(calls) == 1
    assert calls[0].agent_id == "child"


# ─── HookMatcher tests ───────────────────────────────────────────────────────

class TestHookMatcher:
    def test_wildcard_matches_all(self):
        m = HookMatcher(pattern="*")
        assert m.matches("shell", {})
        assert m.matches("file_write", {"path": "/x"})

    def test_exact_match(self):
        m = HookMatcher(pattern="shell")
        assert m.matches("shell", {})
        assert not m.matches("file_write", {})

    def test_pipe_alternation(self):
        m = HookMatcher(pattern="file_write|file_edit")
        assert m.matches("file_write", {})
        assert m.matches("file_edit", {})
        assert not m.matches("shell", {})

    def test_if_condition_matches(self):
        m = HookMatcher(pattern="shell", if_condition="tool_input.cmd matches 'git *'")
        assert m.matches("shell", {"cmd": "git push"})
        assert m.matches("shell", {"cmd": "git status"})
        assert not m.matches("shell", {"cmd": "rm -rf /"})

    def test_if_condition_no_match_wrong_tool(self):
        m = HookMatcher(pattern="shell", if_condition="tool_input.cmd matches 'git *'")
        assert not m.matches("file_write", {"cmd": "git push"})

    def test_if_condition_missing_field(self):
        m = HookMatcher(pattern="shell", if_condition="tool_input.cmd matches 'git *'")
        assert not m.matches("shell", {"path": "/x"})


# ─── HookResult / HookOutput tests ──────────────────────────────────────────

class TestHookResult:
    def test_blocks_exit_2(self):
        r = HookResult(exit_code=2, stderr="denied")
        assert r.control is HookControl.BLOCK

    def test_no_block_exit_0(self):
        r = HookResult(exit_code=0, stdout="ok")
        assert r.control is HookControl.CONTINUE

    def test_approves_explicitly(self):
        output = HookOutput(decision=HookDecision.ALLOW)
        r = HookResult(exit_code=0, parsed=output)
        assert r.control is HookControl.APPROVE

    def test_no_approve_without_decision(self):
        r = HookResult(exit_code=0, stdout="some text")
        assert r.control is HookControl.CONTINUE

    def test_structured_block_is_typed_at_protocol_boundary(self):
        output = HookOutput.from_dict({"decision": "block", "reason": "policy"})
        r = HookResult(exit_code=0, parsed=output)

        assert output.decision is HookDecision.BLOCK
        assert r.control is HookControl.BLOCK

    def test_unknown_decision_does_not_create_a_control_state(self):
        output = HookOutput.from_dict({"decision": "maybe"})

        assert output.decision is None

    def test_has_context(self):
        output = HookOutput(additional_context="extra info")
        r = HookResult(exit_code=0, parsed=output)
        assert r.context == "extra info"


# ─── HookRegistry tests ─────────────────────────────────────────────────────

class TestHookRegistry:
    def test_register_internal(self):
        registry = HookRegistry()
        callback = MagicMock()
        hook = InternalHook(callback=callback)
        registry.register_internal(HookEvent.POST_TOOL_USE, hook)

        found = registry.find_internal(HookEvent.POST_TOOL_USE, "shell", {})
        assert len(found) == 1
        assert found[0].callback is callback

    def test_find_internal_respects_matcher(self):
        registry = HookRegistry()
        callback = MagicMock()
        hook = InternalHook(callback=callback, matcher=HookMatcher(pattern="shell"))
        registry.register_internal(HookEvent.POST_TOOL_USE, hook)

        assert len(registry.find_internal(HookEvent.POST_TOOL_USE, "shell", {})) == 1
        assert len(registry.find_internal(HookEvent.POST_TOOL_USE, "file_write", {})) == 0

    def test_load_from_settings(self, tmp_path):
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell",
                        "hooks": [
                            {"type": "command", "command": "python check.py", "timeout": 5}
                        ],
                    }
                ]
            }
        }
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        registry = HookRegistry()
        registry.load_from_settings(settings_path)

        found = registry.find_external(HookEvent.PRE_TOOL_USE, "shell", {})
        assert len(found) == 1
        assert found[0].command == "python check.py"
        assert found[0].timeout == 5

    def test_load_from_nonexistent_file(self, tmp_path):
        registry = HookRegistry()
        registry.load_from_settings(tmp_path / "nope.json")
        assert registry.find_external(HookEvent.PRE_TOOL_USE, "shell", {}) == []


# ─── HookDispatcher tests ────────────────────────────────────────────────────

class TestHookDispatcher:
    def test_internal_hook_fires(self):
        callback = MagicMock()
        registry = HookRegistry()
        registry.register_internal(
            HookEvent.POST_TOOL_USE,
            InternalHook(callback=callback),
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(
            event=HookEvent.POST_TOOL_USE,
            tool_name="shell",
            tool_input={"cmd": "ls"},
            tool_output={"success": True, "output": "file.txt"},
        )
        result = dispatcher.dispatch(HookEvent.POST_TOOL_USE, ctx)

        callback.assert_called_once_with(ctx)
        assert result.control is HookControl.CONTINUE

    def test_internal_hook_exception_does_not_crash(self):
        def bad_hook(ctx):
            raise RuntimeError("boom")

        registry = HookRegistry()
        registry.register_internal(
            HookEvent.POST_TOOL_USE,
            InternalHook(callback=bad_hook),
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.POST_TOOL_USE, tool_name="shell")
        result = dispatcher.dispatch(HookEvent.POST_TOOL_USE, ctx)
        assert result.control is HookControl.CONTINUE

    @patch("hooks.executor.LocalRuntime.exec")
    def test_external_hook_blocks_on_exit_2(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="Blocked: dangerous"
        )
        registry = HookRegistry()
        registry._external[HookEvent.PRE_TOOL_USE].append(
            ExternalHookConfig(command="python block.py", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="shell", tool_input={"cmd": "rm -rf /"})
        result = dispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)

        assert result.control is HookControl.BLOCK
        assert "Blocked" in result.reason
        call = mock_run.call_args
        assert call.kwargs["cwd"] == str(Path.cwd().resolve())
        stdin_payload = json.loads(call.kwargs["stdin_data"])
        assert stdin_payload["event"] == HookEvent.PRE_TOOL_USE.value
        assert stdin_payload["tool_input"] == {"cmd": "rm -rf /"}

    @patch("hooks.executor.LocalRuntime.exec")
    def test_external_hook_approves_explicitly(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"decision": "allow"}),
            stderr="",
        )
        registry = HookRegistry()
        registry._external[HookEvent.PRE_TOOL_USE].append(
            ExternalHookConfig(command="python allow.py", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="shell", tool_input={"cmd": "ls"})
        result = dispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)

        assert result.control is HookControl.APPROVE

    @patch("hooks.executor.LocalRuntime.exec")
    def test_dispatch_stop_blocks_on_exit_2(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="tests failed"
        )
        registry = HookRegistry()
        registry._external[HookEvent.STOP].append(
            ExternalHookConfig(command="python -m pytest", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.STOP, messages=[{"role": "assistant", "content": "done"}])
        result = dispatcher.dispatch_stop(ctx)

        assert result.control is HookControl.BLOCK
        assert "tests failed" in result.reason

    @patch("hooks.executor.LocalRuntime.exec")
    def test_regular_stop_dispatch_is_blockable(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="tests failed"
        )
        registry = HookRegistry()
        registry._external[HookEvent.STOP].append(
            ExternalHookConfig(command="python -m pytest", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.STOP)
        result = dispatcher.dispatch(HookEvent.STOP, ctx)

        assert result.control is HookControl.BLOCK

    @patch("hooks.executor.LocalRuntime.exec")
    def test_non_blockable_event_ignores_exit_2(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="Blocked"
        )
        registry = HookRegistry()
        registry._external[HookEvent.POST_TOOL_USE].append(
            ExternalHookConfig(command="python x.py", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.POST_TOOL_USE, tool_name="shell")
        result = dispatcher.dispatch(HookEvent.POST_TOOL_USE, ctx)

        # POST_TOOL_USE is not blockable, so exit 2 is logged but doesn't block
        assert result.control is HookControl.CONTINUE

    def test_no_hooks_returns_empty_result(self):
        registry = HookRegistry()
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="shell")
        result = dispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)

        assert result.control is HookControl.CONTINUE
        assert result.additional_context == ""


# ─── Integration: ToolRegistry + HookDispatcher ──────────────────────────────

class TestToolRegistryHookIntegration:
    def test_post_tool_use_fires_after_execution(self):
        from tools.base import NoopTool, ToolRegistry

        callback = MagicMock()
        registry_hooks = HookRegistry()
        registry_hooks.register_internal(
            HookEvent.POST_TOOL_USE,
            InternalHook(callback=callback, matcher=HookMatcher(pattern="noop")),
        )
        dispatcher = HookDispatcher(registry_hooks)

        tool_registry = ToolRegistry(hook_dispatcher=dispatcher)
        tool_registry.register(NoopTool())
        tool_registry.execute_tool("noop", {"input": "hi"})

        callback.assert_called_once()
        ctx = callback.call_args[0][0]
        assert ctx.event == HookEvent.POST_TOOL_USE
        assert ctx.tool_name == "noop"
        assert ctx.tool_output["success"] is True

    def test_post_tool_use_failure_fires_on_error(self):
        from tools.base import FailingTool, ToolRegistry

        callback = MagicMock()
        registry_hooks = HookRegistry()
        registry_hooks.register_internal(
            HookEvent.POST_TOOL_USE_FAILURE,
            InternalHook(callback=callback, matcher=HookMatcher(pattern="test")),
        )
        dispatcher = HookDispatcher(registry_hooks)

        tool_registry = ToolRegistry(hook_dispatcher=dispatcher)
        tool_registry.register(FailingTool())
        tool_registry.execute_tool("test", {})

        callback.assert_called_once()
        ctx = callback.call_args[0][0]
        assert ctx.event == HookEvent.POST_TOOL_USE_FAILURE
        assert ctx.tool_output["success"] is False


# ─── Integration: PermissionPipeline + HookDispatcher (Layer 2) ──────────────

class TestPipelineHookIntegration:
    @patch("hooks.executor.LocalRuntime.exec")
    def test_layer2_blocks_via_dispatcher(self, mock_run):
        from hitl.pipeline import PermissionDecision, PermissionLayer, PermissionPipeline

        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="Hook denied"
        )
        registry_hooks = HookRegistry()
        registry_hooks._external[HookEvent.PRE_TOOL_USE].append(
            ExternalHookConfig(command="python deny.py", matcher=HookMatcher(pattern="shell"))
        )
        dispatcher = HookDispatcher(registry_hooks)

        pipeline = PermissionPipeline(hook_dispatcher=dispatcher)

        class FakeShell:
            name = "shell"
            risk_level = "high"
            def classify_risk(self, params):
                return self.risk_level
            def permission_denial_reason(self, params):
                return None

        result = pipeline.check(FakeShell(), {"cmd": "git push origin main"})
        assert result.decision is PermissionDecision.DENY
        assert result.layer is PermissionLayer.PRE_TOOL_HOOK

    @patch("hooks.executor.LocalRuntime.exec")
    def test_layer2_approves_via_dispatcher(self, mock_run):
        from hitl.pipeline import PermissionDecision, PermissionLayer, PermissionPipeline

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"decision": "allow"}),
            stderr="",
        )
        registry_hooks = HookRegistry()
        registry_hooks._external[HookEvent.PRE_TOOL_USE].append(
            ExternalHookConfig(command="python allow.py", matcher=HookMatcher(pattern="*"))
        )
        dispatcher = HookDispatcher(registry_hooks)

        pipeline = PermissionPipeline(hook_dispatcher=dispatcher)

        class FakeShell:
            name = "shell"
            risk_level = "high"
            def classify_risk(self, params):
                return self.risk_level
            def permission_denial_reason(self, params):
                return None

        result = pipeline.check(FakeShell(), {"cmd": "ls"})
        assert result.decision is PermissionDecision.ALLOW
        assert result.layer is PermissionLayer.PRE_TOOL_HOOK


class TestTypedPermissionPipeline:
    def test_string_boundaries_coerce_to_enums(self):
        from hitl.permission_rule import PermissionRule, PermissionRuleTier
        from hitl.pipeline import PromptAction, PromptDecision

        rule = PermissionRule.parse("file_read", tier="allow")
        prompt = PromptDecision(action="allow_once")

        assert rule.tier is PermissionRuleTier.ALLOW
        assert prompt.action is PromptAction.ALLOW_ONCE

    def test_background_policy_surfaces_prompt_with_agent_identity(self):
        from hitl.pipeline import (
            PermissionDecision,
            PermissionLayer,
            PermissionPipeline,
            PromptAction,
            PromptDecision,
        )
        from tools.base import NoopTool

        prompt_calls = []

        def confirm(request):
            prompt_calls.append(request)
            return PromptDecision(action=PromptAction.ALLOW_ONCE)

        pipeline = PermissionPipeline(confirm_callback=confirm)
        background = pipeline.for_agent("general")
        result = background.check(NoopTool("writer"), {})

        assert result.decision is PermissionDecision.ALLOW
        assert result.layer is PermissionLayer.INTERACTIVE
        assert len(prompt_calls) == 1
        assert prompt_calls[0].agent_name == "general"

    def test_background_policy_preserves_explicit_auto_approval(self):
        from hitl.pipeline import (
            PermissionDecision,
            PermissionPipeline,
            ToolApprovalMode,
        )
        from tools.base import NoopTool

        background = PermissionPipeline(
            approval_mode=ToolApprovalMode.AUTO,
        ).for_agent("general")

        assert background.check(
            NoopTool("writer"), {}
        ).decision is PermissionDecision.ALLOW

    def test_prompt_mode_fails_closed_without_callback(self):
        from hitl.pipeline import PermissionDecision, PermissionPipeline
        from tools.base import NoopTool

        result = PermissionPipeline().check(NoopTool("writer"), {})

        assert result.decision is PermissionDecision.DENY

    def test_tool_owned_validator_blocks_parameterized_shell(self):
        from hitl.pipeline import PermissionDecision, PermissionLayer, PermissionPipeline
        from tools.shell_tool import ShellTool

        from hitl.pipeline import ToolApprovalMode
        result = PermissionPipeline(approval_mode=ToolApprovalMode.AUTO).check(
            ShellTool(),
            {"command": "rm", "args": ["-rf", "/"]},
        )

        assert result.decision is PermissionDecision.DENY
        assert result.layer is PermissionLayer.INPUT_VALIDATION

    def test_path_check_uses_metadata_not_tool_name(self, tmp_path):
        from hitl.pipeline import (
            PermissionDecision,
            PermissionLayer,
            PermissionPipeline,
            ToolApprovalMode,
        )
        from tools.base import NoopTool, PathAccess, ToolEffect, ToolMetadata

        tool = NoopTool("arbitrary_writer")
        tool.metadata = ToolMetadata(
            effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
            path_access=PathAccess.WRITE,
            path_parameter="target",
        )
        outside = tmp_path.parent / "outside.txt"

        result = PermissionPipeline(
            approval_mode=ToolApprovalMode.AUTO,
            project_root=str(tmp_path),
        ).check(tool, {"target": str(outside)})

        assert result.decision is PermissionDecision.DENY
        assert result.layer is PermissionLayer.TOOL_CHECK

    def test_path_check_resolves_relative_path_from_declared_project_root(
        self, tmp_path, monkeypatch,
    ):
        from hitl.pipeline import PermissionDecision, PermissionPipeline, ToolApprovalMode
        from tools.base import NoopTool, PathAccess, ToolEffect, ToolMetadata

        project = tmp_path / "project"
        elsewhere = tmp_path / "elsewhere"
        project.mkdir()
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        tool = NoopTool("writer")
        tool.metadata = ToolMetadata(
            effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
            path_access=PathAccess.WRITE,
            path_parameter="path",
        )

        result = PermissionPipeline(
            approval_mode=ToolApprovalMode.AUTO,
            project_root=str(project),
        ).check(tool, {"path": "child.txt"})

        assert result.decision is PermissionDecision.ALLOW

    def test_registry_scope_rebinds_permission_project_root(self, tmp_path):
        from hitl.pipeline import PermissionDecision, PermissionPipeline, ToolApprovalMode
        from tools.base import (
            ExecutionContext,
            NoopTool,
            PathAccess,
            ToolEffect,
            ToolMetadata,
            ToolRegistry,
        )

        parent = tmp_path / "parent"
        child = tmp_path / "child"
        parent.mkdir()
        child.mkdir()
        pipeline = PermissionPipeline(
            approval_mode=ToolApprovalMode.AUTO,
            project_root=str(parent),
        )
        tool = NoopTool("writer")
        tool.metadata = ToolMetadata(
            effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
            path_access=PathAccess.WRITE,
            path_parameter="path",
        )
        registry = ToolRegistry(permission_pipeline=pipeline)
        registry.register(tool)

        original = pipeline.check(tool, {"path": str(child / "child.txt")})
        scoped = registry.scoped(ExecutionContext(
            workspace_root=str(child), repo_path=str(child),
        )).execute_tool("writer", {"path": str(child / "child.txt")})

        assert original.decision is PermissionDecision.DENY
        assert scoped.success is True


# ─── P1-2: ProactiveMemory.check_plan_feedback ───────────────────────────────

class TestProactiveMemoryPlanFeedback:
    def test_captures_actionable_feedback(self, tmp_path):
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        pm.check_plan_feedback("Don't use raw SQL queries, use the ORM instead")

        # Should have saved a memory
        memories = store.list_memories()
        assert len(memories) >= 1
        assert any("plan feedback" in m.description.lower() for m in memories)

    def test_ignores_generic_feedback(self, tmp_path):
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        pm.check_plan_feedback("Plan rejected by user")
        pm.check_plan_feedback("")
        pm.check_plan_feedback("short")

        memories = store.list_memories()
        assert len(memories) == 0

    def test_deduplicates_same_feedback(self, tmp_path):
        from memory.store import MemoryStore
        from memory.proactive import ProactiveMemory

        store = MemoryStore(repo_path=str(tmp_path), memory_dir=str(tmp_path / "mem"))
        pm = ProactiveMemory(store)

        pm.check_plan_feedback("Always use TypeScript interfaces, never raw objects")
        pm.check_plan_feedback("Always use TypeScript interfaces, never raw objects")

        memories = store.list_memories()
        assert len(memories) == 1

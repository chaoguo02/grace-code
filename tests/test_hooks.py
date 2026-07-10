"""
tests/test_hooks.py

Tests for the hooks/ package: events, matcher, executor, registry, dispatcher.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hooks.events import BLOCKABLE_EVENTS, HookContext, HookEvent
from hooks.matcher import HookMatcher
from hooks.protocol import DispatchResult, ExitCode, HookOutput, HookResult
from hooks.registry import ExternalHookConfig, HookRegistry, InternalHook
from hooks.dispatcher import HookDispatcher


# ─── HookEvent tests ──────────────────────────────────────────────────────────

class TestHookEvent:
    def test_event_values(self):
        assert HookEvent.PRE_TOOL_USE == "PreToolUse"
        assert HookEvent.POST_TOOL_USE == "PostToolUse"
        assert HookEvent.STOP == "Stop"
        assert HookEvent.SESSION_START == "SessionStart"

    def test_blockable_events(self):
        assert HookEvent.PRE_TOOL_USE in BLOCKABLE_EVENTS
        assert HookEvent.USER_PROMPT_SUBMIT in BLOCKABLE_EVENTS
        assert HookEvent.POST_TOOL_USE not in BLOCKABLE_EVENTS
        assert HookEvent.STOP not in BLOCKABLE_EVENTS


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
        assert r.blocks is True

    def test_no_block_exit_0(self):
        r = HookResult(exit_code=0, stdout="ok")
        assert r.blocks is False

    def test_approves_explicitly(self):
        output = HookOutput(decision="allow")
        r = HookResult(exit_code=0, parsed=output)
        assert r.approves_explicitly is True

    def test_no_approve_without_decision(self):
        r = HookResult(exit_code=0, stdout="some text")
        assert r.approves_explicitly is False

    def test_has_context(self):
        output = HookOutput(additional_context="extra info")
        r = HookResult(exit_code=0, parsed=output)
        assert r.has_context is True


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
        assert result.blocked is False

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
        assert result.blocked is False

    @patch("hooks.executor.subprocess.run")
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

        assert result.blocked is True
        assert "Blocked" in result.reason

    @patch("hooks.executor.subprocess.run")
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

        assert result.approved_explicitly is True
        assert result.blocked is False

    @patch("hooks.executor.subprocess.run")
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

        assert result.blocked is True
        assert "tests failed" in result.reason

    @patch("hooks.executor.subprocess.run")
    def test_regular_stop_dispatch_remains_non_blockable(self, mock_run):
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

        assert result.blocked is False

    @patch("hooks.executor.subprocess.run")
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
        assert result.blocked is False

    def test_no_hooks_returns_empty_result(self):
        registry = HookRegistry()
        dispatcher = HookDispatcher(registry)

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="shell")
        result = dispatcher.dispatch(HookEvent.PRE_TOOL_USE, ctx)

        assert result.blocked is False
        assert result.approved_explicitly is False
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
    @patch("hooks.executor.subprocess.run")
    def test_layer2_blocks_via_dispatcher(self, mock_run):
        from hitl.pipeline import PermissionPipeline

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

        result = pipeline.check(FakeShell(), {"cmd": "git push origin main"})
        assert result.approved is False
        assert result.layer == 2

    @patch("hooks.executor.subprocess.run")
    def test_layer2_approves_via_dispatcher(self, mock_run):
        from hitl.pipeline import PermissionPipeline

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

        result = pipeline.check(FakeShell(), {"cmd": "ls"})
        assert result.approved is True
        assert result.layer == 2


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

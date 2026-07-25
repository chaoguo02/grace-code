"""Test isReadOnly dynamic judgment — CC-aligned plan mode permission checks."""

from __future__ import annotations

import pytest


class TestIsReadOnlyOnBaseTool:
    """Verify BaseTool.isReadOnly() default and fallback behavior."""

    def test_default_is_false(self):
        """Fail-closed: unnamed tools without metadata return False."""
        from core.base import BaseTool, ToolMetadata, ToolResult

        class MinimalTool(BaseTool):
            metadata = ToolMetadata()
            @property
            def name(self) -> str:
                return "minimal"
            @property
            def description(self) -> str:
                return "test"
            @property
            def parameters_schema(self) -> dict:
                return {"type": "object", "properties": {}}
            def execute(self, params):
                return ToolResult(success=True, output="")

        tool = MinimalTool()
        assert tool.isReadOnly() is False

    def test_read_effect_fallback_returns_true(self):
        """Tools with only READ_WORKSPACE effect are treated as read-only."""
        from core.base import BaseTool, ToolMetadata, ToolResult
        from core.types import ToolEffect

        class ReadOnlyTool(BaseTool):
            metadata = ToolMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}))
            @property
            def name(self) -> str:
                return "read"
            @property
            def description(self) -> str:
                return "test"
            @property
            def parameters_schema(self) -> dict:
                return {"type": "object", "properties": {}}
            def execute(self, params):
                return ToolResult(success=True, output="")

        tool = ReadOnlyTool()
        assert tool.isReadOnly() is True

    def test_write_effect_returns_false(self):
        """Tools with WRITE_WORKSPACE effect are NOT read-only."""
        from core.base import BaseTool, ToolMetadata, ToolResult
        from core.types import ToolEffect

        class WriteTool(BaseTool):
            metadata = ToolMetadata(effects=frozenset({ToolEffect.WRITE_WORKSPACE}))
            @property
            def name(self) -> str:
                return "write"
            @property
            def description(self) -> str:
                return "test"
            @property
            def parameters_schema(self) -> dict:
                return {"type": "object", "properties": {}}
            def execute(self, params):
                return ToolResult(success=True, output="")

        tool = WriteTool()
        assert tool.isReadOnly() is False


class TestIsReadOnlyOnRealTools:
    """Verify concrete tool classes return correct isReadOnly values."""

    def test_read_tool_is_readonly(self):
        from tools.file_tool import FileReadTool
        tool = FileReadTool()
        assert tool.isReadOnly() is True

    def test_write_tool_is_not_readonly(self):
        try:
            from tools.file_tool import FileWriteTool
            tool = FileWriteTool()
            assert tool.isReadOnly() is False
        except ImportError:
            pytest.skip("FileWriteTool not importable")

    def test_grep_is_readonly(self):
        from tools.search_tool import SearchTextTool
        tool = SearchTextTool()
        assert tool.isReadOnly() is True

    def test_plan_mode_tools_are_readonly(self):
        from tools.plan_mode_tool import EnterPlanModeTool, ExitPlanModeTool
        assert EnterPlanModeTool().isReadOnly() is True
        assert ExitPlanModeTool().isReadOnly() is True

    def test_shell_readonly_commands(self):
        from tools.shell_tool import ShellTool
        tool = ShellTool()
        assert tool.isReadOnly({"command": "ls -la"}) is True
        assert tool.isReadOnly({"command": "cat file.txt"}) is True
        assert tool.isReadOnly({"command": "git status"}) is True
        assert tool.isReadOnly({"command": "git log --oneline"}) is True

    def test_shell_write_commands(self):
        from tools.shell_tool import ShellTool
        tool = ShellTool()
        assert tool.isReadOnly({"command": "rm file.txt"}) is False
        assert tool.isReadOnly({"command": "git push origin main"}) is False
        assert tool.isReadOnly({"command": "npm install"}) is False


class TestPermissionPipelinePlanMode:
    """Verify plan mode denies non-readonly tools dynamically."""

    def test_plan_mode_denies_write(self):
        from hitl.pipeline import PermissionPipeline
        from tools.file_tool import FileWriteTool

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("plan")
        tool = FileWriteTool()
        result = pipeline.check(tool, {"file_path": "test.txt"})
        assert result.decision.value == "deny"

    def test_plan_mode_allows_read(self):
        from hitl.pipeline import PermissionPipeline
        from tools.file_tool import FileReadTool

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("plan")
        tool = FileReadTool()
        result = pipeline.check(tool, {"file_path": "test.txt"})
        # Read is read-only → Layer 4 dynamic isReadOnly() check passes through.
        # The denial (if any) comes from Layer 6 (no confirm callback in test
        # mode), NOT from Layer 4. Verify the reason does NOT mention "plan mode".
        assert "plan mode" not in result.reason.lower()


class TestPlanModeAttachmentManager:
    """Verify throttling schedule matches CC-aligned constants."""

    def test_turn_1_skipped(self):
        """Turn 1 is skipped — build_runtime_messages already injects."""
        from agent.plan_attachment_manager import PlanModeAttachmentManager
        mgr = PlanModeAttachmentManager()
        mgr.set_turn(1)
        assert mgr.should_inject() is False  # build_runtime_messages handles turn 1

    def test_turns_2_to_4_get_nothing(self):
        from agent.plan_attachment_manager import PlanModeAttachmentManager
        mgr = PlanModeAttachmentManager()
        for turn in (2, 3, 4):
            mgr.set_turn(turn)
            assert mgr.should_inject() is False, f"Turn {turn} should skip"

    def test_turn_5_gets_injection(self):
        from agent.plan_attachment_manager import PlanModeAttachmentManager
        mgr = PlanModeAttachmentManager()
        mgr.set_turn(5)
        assert mgr.should_inject() is True

    def test_turn_25_gets_full_refresh(self):
        from agent.plan_attachment_manager import PlanModeAttachmentManager
        mgr = PlanModeAttachmentManager()
        mgr.set_turn(25)
        assert mgr.should_inject() is True

    def test_compaction_reset(self):
        from agent.plan_attachment_manager import PlanModeAttachmentManager
        mgr = PlanModeAttachmentManager()
        mgr.set_turn(10)
        mgr.reset_on_compaction()
        assert mgr.current_turn() == 1  # Next injection is full


class TestPlanNaming:
    """Verify word-slug generation."""

    def test_generates_unique_slugs(self):
        from utils.plan_naming import generate_plan_slug
        slugs = {generate_plan_slug() for _ in range(20)}
        # Should get mostly unique slugs (collisions are possible but rare)
        assert len(slugs) >= 15  # Allow a few collisions

    def test_respects_existing_slugs(self):
        from utils.plan_naming import generate_plan_slug
        existing = {"bold-eagle", "calm-river", "wise-owl"}
        for _ in range(5):
            slug = generate_plan_slug(existing)
            assert slug not in existing
            existing.add(slug)


class TestModeSwitching:
    """Verify unified handle_plan_mode_transition."""

    def test_entry_saves_pre_plan_mode(self):
        from hitl.pipeline import PermissionPipeline
        from agent.mode_switching import handle_plan_mode_transition

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("acceptEdits")

        # Mock registry
        class MockRegistry:
            _permission_pipeline = pipeline

        result = handle_plan_mode_transition(MockRegistry(), "plan")
        assert result == "acceptEdits"  # prePlanMode saved correctly
        assert pipeline.permission_mode == "plan"

    def test_enter_plan_mode_does_not_double_save(self):
        """EnterPlanMode.execute() should NOT call handle_plan_mode_transition
        directly — only set the signal. The main loop handles the transition
        via check_pending_mode_switch. Otherwise save_pre_plan_mode is called
        twice and the second call overwrites the true prePlanMode with 'plan'."""
        from tools.plan_mode_tool import EnterPlanModeTool
        from hitl.pipeline import PermissionPipeline

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("acceptEdits")
        pipeline.save_pre_plan_mode()  # Simulate: session setup saved this

        class MockRegistry:
            _permission_pipeline = pipeline
            _pending_mode_switch = None
            _session_id = ""
            _repo_path = ""

        tool = EnterPlanModeTool()
        mock = MockRegistry()
        tool._registry = mock
        result = tool.execute({})

        # Tool should succeed and set _pending_mode_switch on the registry instance
        assert result.success is True
        assert mock._pending_mode_switch is not None
        assert mock._pending_mode_switch["mode"] == "plan"

        # CRITICAL: prePlanMode should STILL be "acceptEdits", NOT "plan"
        # (EnterPlanMode only sets the signal, the main loop does the actual
        # mode switch via check_pending_mode_switch → _apply_mode_to_pipeline)
        assert pipeline._pre_plan_mode == "acceptEdits"

    def test_exit_restores_mode(self):
        from hitl.pipeline import PermissionPipeline
        from agent.mode_switching import handle_plan_mode_transition

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("default")
        pipeline.save_pre_plan_mode()
        pipeline.set_permission_mode("plan")

        class MockRegistry:
            _permission_pipeline = pipeline

        result = handle_plan_mode_transition(MockRegistry(), "build")
        assert result == "default"  # Restored
        assert pipeline.permission_mode == "default"

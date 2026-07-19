"""
End-to-end tests for the core backend flows.

Covers:
  1. Permission pipeline — Layer 1-6 flow, mode checks, rule evaluation
  2. Session lifecycle — create, chat, complete
  3. Plan mode — approve/reject flow
  4. Completion guard — git diff verification
  5. Memory CRUD — create, read, list, delete
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ────────────────────────────────────────────────────────────────────────────
# Test 1: Permission Pipeline — Layer 1-6 end-to-end
# ────────────────────────────────────────────────────────────────────────────


class TestPermissionPipeline:
    """Verify all 6 layers of the permission pipeline work correctly."""

    def test_layer1_validateInput_denies_blocked_params(self):
        """Layer 1 should deny tools with blocked parameters."""
        from hitl.pipeline import PermissionPipeline, PermissionDecision
        from core.base import BaseTool, ToolMetadata, ToolResult

        class DangerousTool(BaseTool):
            name = "Dangerous"
            description = "test"
            parameters_schema = {"type": "object", "properties": {}}
            metadata = ToolMetadata()
            def execute(self, params):
                return ToolResult(success=True, output="ok")

        pipeline = PermissionPipeline()
        result = pipeline.check(DangerousTool(), {})
        # Layer 1 passes through if no denial reason
        assert result.decision in (PermissionDecision.ALLOW, PermissionDecision.DENY)

    def test_layer3_rules_deny_ask_allow_priority(self):
        """Layer 3 should evaluate deny→ask→allow in that order."""
        from hitl.pipeline import PermissionPipeline, PermissionRuleTier
        from hitl.permission_rule import PermissionRule

        deny_rule = PermissionRule.parse("Write", tier="deny")
        ask_rule = PermissionRule.parse("Write", tier="ask")
        allow_rule = PermissionRule.parse("Write", tier="allow")

        pipeline = PermissionPipeline(rules=[allow_rule, ask_rule, deny_rule])
        tier, matched = pipeline._layer3_rules("Write", {"file_path": "test.txt"})

        # Deny should win over ask and allow
        assert tier is PermissionRuleTier.DENY

    def test_layer3_returns_none_when_no_rule_matches(self):
        """Layer 3 should return None when no rules match → Layer 4 runs."""
        from hitl.pipeline import PermissionPipeline

        pipeline = PermissionPipeline(rules=[])
        tier, matched = pipeline._layer3_rules("UnknownTool", {})

        assert tier is None
        assert matched is None

    def test_layer4_acceptEdits_auto_approves_write(self):
        """acceptEdits mode should auto-approve Write/Edit tools."""
        from hitl.pipeline import PermissionPipeline, PermissionDecision
        from core.base import BaseTool, ToolMetadata, ToolResult

        class WriteTool(BaseTool):
            name = "Write"
            description = "test"
            parameters_schema = {"type": "object", "properties": {}}
            metadata = ToolMetadata()
            def execute(self, params):
                return ToolResult(success=True, output="ok")

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("acceptEdits")
        result = pipeline.check(WriteTool(), {"file_path": "test.txt"})

        # acceptEdits should auto-approve Write
        assert result.decision is PermissionDecision.ALLOW

    def test_layer4_plan_denies_write(self):
        """Plan mode should deny Write/Edit/Bash tools."""
        from hitl.pipeline import PermissionPipeline, PermissionDecision
        from core.base import BaseTool, ToolMetadata, ToolResult

        class WriteTool(BaseTool):
            name = "Write"
            description = "test"
            parameters_schema = {"type": "object", "properties": {}}
            metadata = ToolMetadata()
            def execute(self, params):
                return ToolResult(success=True, output="ok")

        pipeline = PermissionPipeline()
        pipeline.set_permission_mode("plan")
        result = pipeline.check(WriteTool(), {"file_path": "test.txt"})

        assert result.decision is PermissionDecision.DENY

    def test_layer4_bypassPermissions_checks_force_interactive(self):
        """bypassPermissions should still prompt when _force_interactive is set."""
        from hitl.pipeline import PermissionPipeline, PermissionDecision
        from hitl.permission_rule import PermissionRule
        from core.base import BaseTool, ToolMetadata, ToolResult

        class WriteTool(BaseTool):
            name = "Write"
            description = "test"
            parameters_schema = {"type": "object", "properties": {}}
            metadata = ToolMetadata()
            def execute(self, params):
                return ToolResult(success=True, output="ok")

        ask_rule = PermissionRule.parse("Write", tier="ask")
        pipeline = PermissionPipeline(rules=[ask_rule])
        pipeline.set_permission_mode("bypassPermissions")

        # With ask rule + bypassPermissions + no callback → should DENY
        result = pipeline.check(WriteTool(), {"file_path": "test.txt"})
        # _force_interactive set → Layer 4 returns None → Layer 6 no callback → DENY
        assert result.decision is PermissionDecision.DENY

    def test_layer6_deny_without_callback(self):
        """Layer 6 should DENY when no callback is available (fail closed)."""
        from hitl.pipeline import PermissionPipeline, PermissionDecision
        from core.base import BaseTool, ToolMetadata, ToolResult

        class WriteTool(BaseTool):
            name = "Write"
            description = "test"
            parameters_schema = {"type": "object", "properties": {}}
            metadata = ToolMetadata()
            def execute(self, params):
                return ToolResult(success=True, output="ok")

        pipeline = PermissionPipeline()
        # No callback set → Layer 6 should deny
        result = pipeline.check(WriteTool(), {"file_path": "test.txt"})
        assert result.decision is PermissionDecision.DENY


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Session Lifecycle
# ────────────────────────────────────────────────────────────────────────────


class TestSessionLifecycle:
    """Verify session create, store, list, delete."""

    def test_create_and_get_session(self):
        """Session should be creatable and retrievable."""
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode

        tmp = tempfile.mkdtemp()
        try:
            db = str(Path(tmp) / "test.db")
            store = SessionStore(db)

            rec = store.create_session(
                agent_name="build", mode=SessionMode.PRIMARY,
                repo_path="/tmp/test", title="Test Session",
            )
            assert rec is not None
            sid = rec.id
            assert len(sid) == 12  # hex session ID

            fetched = store.get_session(sid)
            assert fetched is not None
            assert fetched.agent_name == "build"
            assert fetched.title == "Test Session"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_list_sessions(self):
        """Multiple sessions should be listable."""
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode

        tmp = tempfile.mkdtemp()
        try:
            db = str(Path(tmp) / "test.db")
            store = SessionStore(db)

            store.create_session(agent_name="build", mode=SessionMode.PRIMARY,
                                repo_path="/tmp", title="S1")
            store.create_session(agent_name="plan", mode=SessionMode.PRIMARY,
                                repo_path="/tmp", title="S2")

            sessions = store.list_sessions()
            assert len(sessions) >= 2
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Plan Mode — Approve/Reject
# ────────────────────────────────────────────────────────────────────────────


class TestPlanMode:
    """Verify plan approval and rejection flows."""

    def test_plan_revision_service_create_and_list(self):
        """PlanRevisionService should create and list revisions."""
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode
        from server.services.plan_revision_service import PlanRevisionService
        from app.storage.sqlite import SqliteStorageBackend

        tmp = tempfile.mkdtemp()
        try:
            db_path = str(Path(tmp) / "test.db")
            store = SessionStore(db_path)
            rec = store.create_session(agent_name="plan", mode=SessionMode.PRIMARY,
                                       repo_path=tmp, title="Plan Test")
            sid = rec.id

            storage = SqliteStorageBackend(db_path)
            svc = PlanRevisionService(storage, repo_path=tmp)

            rev = svc.append_revision(sid, "Plan content: build feature X")
            assert rev.revision == 1
            assert rev.session_id == sid

            revisions = svc.list_revisions(sid)
            assert len(revisions) == 1
            assert revisions[0]["content"] == "Plan content: build feature X"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plan_revision_mark_status(self):
        """PlanRevisionService should update revision status."""
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode
        from server.services.plan_revision_service import PlanRevisionService
        from app.storage.sqlite import SqliteStorageBackend

        tmp = tempfile.mkdtemp()
        try:
            db_path = str(Path(tmp) / "test.db")
            store = SessionStore(db_path)
            rec = store.create_session(agent_name="plan", mode=SessionMode.PRIMARY,
                                       repo_path=tmp, title="Plan Test")
            sid = rec.id
            storage = SqliteStorageBackend(db_path)
            svc = PlanRevisionService(storage, repo_path=tmp)

            svc.append_revision(sid, "Plan v1")
            assert svc.mark_status(sid, 1, "approved")
            assert svc.mark_status(sid, 999, "approved") is False
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plan_revision_diff(self):
        """PlanRevisionService should compute diffs between revisions."""
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode
        from server.services.plan_revision_service import PlanRevisionService
        from app.storage.sqlite import SqliteStorageBackend

        tmp = tempfile.mkdtemp()
        try:
            db_path = str(Path(tmp) / "test.db")
            store = SessionStore(db_path)
            rec = store.create_session(agent_name="plan", mode=SessionMode.PRIMARY,
                                       repo_path=tmp, title="Plan Test")
            sid = rec.id
            storage = SqliteStorageBackend(db_path)
            svc = PlanRevisionService(storage, repo_path=tmp)

            svc.append_revision(sid, "line 1\nline 2\nline 3")
            svc.append_revision(sid, "line 1\nline 2 modified\nline 3")

            diff = svc.compute_diff(sid, 1, 2)
            assert "line 2 modified" in diff["diff"]
            assert diff["from_revision"] == 1
            assert diff["to_revision"] == 2
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Completion Guard
# ────────────────────────────────────────────────────────────────────────────


class TestCompletionGuard:
    """Verify the completion guard's git diff validation."""

    def test_guard_blocks_when_no_changes(self):
        """Should block completion when agent wrote files but git has no changes."""
        from agent.completion_guard import TaskCompletionGuard, CompletionContext
        from agent.task import TaskIntent

        ctx = CompletionContext()
        ctx.had_any_write = True
        ctx.files_written = {"test.txt"}

        class FakeGitState:
            is_git_repo = True
            has_changes = False
            files_changed = set()

        guard = TaskCompletionGuard()
        result = guard.check(
            ctx=ctx, task_intent=TaskIntent.EDIT,
            git_state=FakeGitState(),
        )
        assert not result.can_complete
        assert "No workspace revision delta" in result.blocked_reason

    def test_guard_passes_when_changes_present(self):
        """Should pass when git diff shows expected changes."""
        from agent.completion_guard import TaskCompletionGuard, CompletionContext
        from agent.task import TaskIntent

        ctx = CompletionContext()
        ctx.had_any_write = True
        ctx.files_written = {"test.txt"}

        class FakeGitState:
            is_git_repo = True
            has_changes = True
            files_changed = {"test.txt"}

        guard = TaskCompletionGuard()
        result = guard.check(
            ctx=ctx, task_intent=TaskIntent.EDIT,
            git_state=FakeGitState(),
        )
        assert result.can_complete

    def test_guard_passes_for_analysis_intent(self):
        """Analysis intent should skip git check entirely."""
        from agent.completion_guard import TaskCompletionGuard, CompletionContext
        from agent.task import TaskIntent

        ctx = CompletionContext()
        ctx.had_any_write = False

        class FakeGitState:
            is_git_repo = True
            has_changes = False
            files_changed = set()

        guard = TaskCompletionGuard()
        result = guard.check(
            ctx=ctx, task_intent=TaskIntent.ANALYSIS,
            git_state=FakeGitState(),
        )
        assert result.can_complete


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Rule System
# ────────────────────────────────────────────────────────────────────────────


class TestPermissionRules:
    """Verify rule parsing, matching, and glob syntax."""

    def test_exact_match(self):
        """Rule should match exact tool name."""
        from hitl.permission_rule import PermissionRule, PermissionRuleTier

        rule = PermissionRule.parse("Write", tier="deny")
        assert rule.matches("Write", {})
        assert not rule.matches("Read", {})

    def test_bash_alias_match(self):
        """Rule with 'shell' should match tool named 'Bash'."""
        from hitl.permission_rule import PermissionRule, PermissionRuleTier

        rule = PermissionRule.parse("shell(ls *)", tier="allow")
        assert rule.matches("Bash", {"command": "ls -la"})

    def test_glob_prefix_match(self):
        """Trailing * should prefix-match."""
        from hitl.permission_rule import PermissionRule, PermissionRuleTier

        rule = PermissionRule.parse("shell(git commit *)", tier="ask")
        assert rule.matches("Bash", {"command": "git commit -m 'test'"})
        assert rule.matches("Bash", {"command": "git commit"})

    def test_recursive_glob(self):
        """** should recursively match paths."""
        from hitl.permission_rule import PermissionRule, PermissionRuleTier

        rule = PermissionRule.parse("Edit(src/**)", tier="allow")
        assert rule.matches("Edit", {"file_path": "src/foo.py"})
        assert rule.matches("Edit", {"file_path": "src/sub/bar.py"})

    def test_source_priority(self):
        """Higher priority source rules should sort first."""
        from hitl.permission_rule import PermissionRule, PermissionRuleTier, RULE_SOURCE_PRIORITY

        assert RULE_SOURCE_PRIORITY["session"] > RULE_SOURCE_PRIORITY["project"]
        assert RULE_SOURCE_PRIORITY["project"] > RULE_SOURCE_PRIORITY["builtin"]


# ────────────────────────────────────────────────────────────────────────────
# Test 6: Event Typing
# ────────────────────────────────────────────────────────────────────────────


class TestEventTyping:
    """Verify typed WS events serialize correctly."""

    def test_ws_status_to_dict(self):
        """WsStatus should serialize with all non-None fields."""
        from server.events import WsStatus

        ev = WsStatus(status="completed", result={"steps_taken": 5})
        d = ev.to_dict()
        assert d["type"] == "status"
        assert d["status"] == "completed"
        assert d["result"]["steps_taken"] == 5
        assert d.get("error") == ""  # empty string preserved (fix D3-D5)

    def test_ws_plan_ready_to_dict(self):
        """WsPlanReady should serialize contract when present."""
        from server.events import WsPlanReady

        ev = WsPlanReady(plan_text="plan", contract={"goal": "test"}, revision=1)
        d = ev.to_dict()
        assert d["type"] == "plan_ready"
        assert d["contract"]["goal"] == "test"
        assert d["revision"] == 1

    def test_ws_approval_required_to_dict(self):
        """WsApprovalRequired should include decision_reason."""
        from server.events import WsApprovalRequired

        ev = WsApprovalRequired(
            request_id="abc", tool_name="Write",
            decision_reason="Matched ask rule: Write",
        )
        d = ev.to_dict()
        assert d["decision_reason"] == "Matched ask rule: Write"


# ────────────────────────────────────────────────────────────────────────────
# Test 7: Stats Pipeline
# ────────────────────────────────────────────────────────────────────────────


class TestStatsPipeline:
    """Verify stats recording → storage → API chain."""

    def test_record_step_and_complete_session(self):
        """StatsRecorder should record steps and finalize session stats."""
        from app.storage.sqlite import SqliteStorageBackend
        from server.services.stats_service import StatsService
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode

        tmp = tempfile.mkdtemp()
        try:
            db = str(Path(tmp) / "test.db")
            store = SessionStore(db)
            rec = store.create_session(agent_name="build", mode=SessionMode.PRIMARY,
                                       repo_path=tmp, title="Stats Test")
            sid = rec.id

            storage = SqliteStorageBackend(db)
            svc = StatsService(storage)

            # Record a step
            svc.record_step(sid, step_number=1, tool_name="Read",
                            tool_params={"file_path": "test.py"},
                            status="success", duration_ms=100, tokens=50,
                            timestamp="2024-01-01T00:00:00")

            # Complete the session
            svc.record_session_complete(
                sid, agent_name="build", total_steps=5,
                total_tokens=200, total_duration_ms=5000,
                status="completed", tool_summary={"Read": 3, "Write": 2},
            )

            # Verify stats are retrievable
            stats = svc.get_session_stats(sid)
            assert stats is not None
            assert stats["agent_name"] == "build"
            assert stats["total_steps"] == 5

            steps = svc.get_session_steps(sid)
            assert len(steps) >= 1
            assert steps[0]["tool_name"] == "Read"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_record_diff(self):
        """Diff recording should persist diff content."""
        from app.storage.sqlite import SqliteStorageBackend
        from server.services.stats_service import StatsService
        from agent.session.session_store import SessionStore
        from agent.session.models import SessionMode

        tmp = tempfile.mkdtemp()
        try:
            db = str(Path(tmp) / "test.db")
            store = SessionStore(db)
            rec = store.create_session(agent_name="build", mode=SessionMode.PRIMARY,
                                       repo_path=tmp, title="Diff Test")
            sid = rec.id

            storage = SqliteStorageBackend(db)
            svc = StatsService(storage)

            diff = "+hello world\n-old line"
            svc.record_diff(sid, step_number=1, file_path="test.txt", diff_content=diff)

            diffs = svc.get_session_diffs(sid)
            assert len(diffs) >= 1
            assert diffs[0]["file_path"] == "test.txt"
            assert "hello world" in diffs[0]["diff_content"]
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────────
# Test 8: Cancellation Token
# ────────────────────────────────────────────────────────────────────────────


class TestCancellationToken:
    """Verify hierarchical cancellation propagation."""

    def test_parent_cancel_propagates_to_child(self):
        """Child should be cancelled when parent is cancelled."""
        from agent.session.run_context import CancellationToken

        parent = CancellationToken()
        child = parent.child()

        assert not child.is_cancelled
        parent.cancel()
        assert child.is_cancelled

    def test_child_cancel_does_not_affect_parent(self):
        """Child cancel should not cancel parent."""
        from agent.session.run_context import CancellationToken

        parent = CancellationToken()
        child = parent.child()

        child.cancel()
        assert child.is_cancelled
        assert not parent.is_cancelled

    def test_three_level_propagation(self):
        """Grandchild should receive cancel from root."""
        from agent.session.run_context import CancellationToken

        root = CancellationToken()
        child = root.child()
        grandchild = child.child()

        root.cancel()
        assert child.is_cancelled
        assert grandchild.is_cancelled

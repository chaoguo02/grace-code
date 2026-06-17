"""
tests/test_hitl.py

HITL 框架单元测试：RiskLevel / PolicyEngine / HitlManager / 集成。
"""

import pytest
import tempfile
import os
from pathlib import Path

from tools.base import BaseTool, RiskLevel, ToolRegistry, ToolResult
from hitl.request import HitlDecision, HitlRequest, HitlResult, HitlStats
from hitl.policy import PolicyEngine, PolicyRule
from hitl.manager import HitlManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeReadTool(BaseTool):
    @property
    def name(self): return "file_read"
    @property
    def description(self): return "Read"
    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}
    def execute(self, params): return ToolResult(success=True, output="content")


class FakeWriteTool(BaseTool):
    @property
    def name(self): return "file_write"
    @property
    def description(self): return "Write"
    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}
    @property
    def risk_level(self): return RiskLevel.MEDIUM
    def execute(self, params): return ToolResult(success=True, output="written")


class FakeShellTool(BaseTool):
    @property
    def name(self): return "shell"
    @property
    def description(self): return "Shell"
    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}
    @property
    def risk_level(self): return RiskLevel.HIGH

    def classify_risk(self, params):
        cmd = params.get("cmd", "")
        if cmd.startswith("ls") or cmd.startswith("cat"):
            return RiskLevel.NONE
        if "rm" in cmd or "git push" in cmd:
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def execute(self, params): return ToolResult(success=True, output="ok")


class FakeCommitTool(BaseTool):
    @property
    def name(self): return "git_commit"
    @property
    def description(self): return "Commit"
    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}
    @property
    def risk_level(self): return RiskLevel.HIGH
    def execute(self, params): return ToolResult(success=True, output="committed")


# ---------------------------------------------------------------------------
# RiskLevel Tests
# ---------------------------------------------------------------------------

class TestRiskLevel:
    def test_enum_values(self):
        assert RiskLevel.NONE == "none"
        assert RiskLevel.LOW == "low"
        assert RiskLevel.MEDIUM == "medium"
        assert RiskLevel.HIGH == "high"

    def test_default_risk_level(self):
        tool = FakeReadTool()
        assert tool.risk_level == RiskLevel.NONE

    def test_override_risk_level(self):
        tool = FakeWriteTool()
        assert tool.risk_level == RiskLevel.MEDIUM

    def test_classify_risk_default(self):
        tool = FakeWriteTool()
        assert tool.classify_risk({"path": "test.py"}) == RiskLevel.MEDIUM

    def test_classify_risk_dynamic(self):
        tool = FakeShellTool()
        assert tool.classify_risk({"cmd": "ls -la"}) == RiskLevel.NONE
        assert tool.classify_risk({"cmd": "rm -rf /"}) == RiskLevel.HIGH
        assert tool.classify_risk({"cmd": "echo hi"}) == RiskLevel.LOW


# ---------------------------------------------------------------------------
# HitlRequest / HitlResult / HitlStats Tests
# ---------------------------------------------------------------------------

class TestHitlDataModels:
    def test_request_creation(self):
        req = HitlRequest(tool_name="shell", params={"cmd": "rm x"}, risk_level="high")
        assert req.tool_name == "shell"
        assert req.request_id  # auto-generated

    def test_request_summary(self):
        req = HitlRequest(tool_name="file_write", params={"path": "x.py"}, risk_level="medium")
        s = req.summary()
        assert "file_write" in s
        assert "medium" in s

    def test_result_is_approved(self):
        assert HitlResult(decision=HitlDecision.APPROVED).is_approved
        assert HitlResult(decision=HitlDecision.POLICY_APPROVED).is_approved
        assert HitlResult(decision=HitlDecision.SKIPPED).is_approved
        assert not HitlResult(decision=HitlDecision.DENIED).is_approved
        assert not HitlResult(decision=HitlDecision.POLICY_DENIED).is_approved

    def test_result_is_denied(self):
        assert HitlResult(decision=HitlDecision.DENIED).is_denied
        assert HitlResult(decision=HitlDecision.POLICY_DENIED).is_denied
        assert not HitlResult(decision=HitlDecision.APPROVED).is_denied

    def test_stats_record(self):
        stats = HitlStats()
        stats.record(HitlResult(decision=HitlDecision.APPROVED, wait_ms=100))
        stats.record(HitlResult(decision=HitlDecision.DENIED, wait_ms=200))
        stats.record(HitlResult(decision=HitlDecision.POLICY_APPROVED))
        assert stats.total_requests == 3
        assert stats.approvals == 2
        assert stats.denials == 1
        assert stats.policy_matches == 1
        assert stats.total_wait_ms == 300

    def test_stats_rates(self):
        stats = HitlStats()
        stats.record(HitlResult(decision=HitlDecision.APPROVED, wait_ms=100))
        stats.record(HitlResult(decision=HitlDecision.DENIED, wait_ms=300))
        assert stats.approval_rate == 0.5
        assert stats.avg_wait_ms == 200.0


# ---------------------------------------------------------------------------
# PolicyEngine Tests
# ---------------------------------------------------------------------------

class TestPolicyEngine:
    def test_empty_engine(self):
        engine = PolicyEngine()
        assert engine.match("shell", {"cmd": "rm x"}) is None

    def test_param_contains_match(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="r1", tool_name="shell", action="approve",
            condition={"param_contains": {"cmd": "pytest"}},
        ))
        assert engine.match("shell", {"cmd": "pytest tests/"}) is not None
        assert engine.match("shell", {"cmd": "rm -rf /"}) is None

    def test_param_regex_match(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="r2", tool_name="file_write", action="deny",
            condition={"param_regex": {"path": r"tests/.*"}},
        ))
        assert engine.match("file_write", {"path": "tests/foo.py"}) is not None
        assert engine.match("file_write", {"path": "src/main.py"}) is None

    def test_wildcard_tool(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="r3", tool_name="*", action="approve",
            condition={"always": True},
        ))
        assert engine.match("anything", {}) is not None

    def test_param_equals_match(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="r4", tool_name="file_write", action="deny",
            condition={"param_equals": {"path": "secrets.env"}},
        ))
        assert engine.match("file_write", {"path": "secrets.env"}) is not None
        assert engine.match("file_write", {"path": "config.yaml"}) is None

    def test_first_match_wins(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="deny", tool_name="shell", action="deny",
            condition={"param_contains": {"cmd": "rm"}},
        ))
        engine.add_rule(PolicyRule(
            id="allow_all", tool_name="*", action="approve",
            condition={"always": True},
        ))
        result = engine.match("shell", {"cmd": "rm -rf /"})
        assert result.action == "deny"

    def test_remove_rule(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(id="r1", tool_name="shell", action="deny", condition={}))
        assert len(engine.rules) == 1
        engine.remove_rule("r1")
        assert len(engine.rules) == 0

    def test_yaml_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "policies.yaml")
            engine1 = PolicyEngine(policies_path=path)
            engine1.create_rule("shell", "approve", {"param_contains": {"cmd": "pytest"}})
            engine1.create_rule("file_write", "deny", {"param_regex": {"path": "tests/.*"}})

            # Reload from disk
            engine2 = PolicyEngine(policies_path=path)
            assert len(engine2.rules) == 2
            assert engine2.rules[0].tool_name == "shell"
            assert engine2.rules[1].action == "deny"


# ---------------------------------------------------------------------------
# HitlManager Tests
# ---------------------------------------------------------------------------

class TestHitlManager:
    def test_skip_low_risk(self):
        manager = HitlManager(min_risk_for_confirm="medium")
        tool = FakeReadTool()  # risk=NONE
        result = manager.check(tool, {})
        assert result.decision == HitlDecision.SKIPPED

    def test_skip_low_risk_shell_readonly(self):
        manager = HitlManager(min_risk_for_confirm="medium")
        tool = FakeShellTool()
        result = manager.check(tool, {"cmd": "ls -la"})  # classify → NONE
        assert result.decision == HitlDecision.SKIPPED

    def test_policy_auto_approve(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="p1", tool_name="shell", action="approve",
            condition={"param_contains": {"cmd": "pytest"}},
        ))
        manager = HitlManager(policy_engine=engine, min_risk_for_confirm="medium")
        tool = FakeShellTool()
        result = manager.check(tool, {"cmd": "pytest tests/"})
        # classify_risk("pytest tests/") returns LOW < medium → SKIPPED
        # Actually LOW < MEDIUM → skipped before policy check
        assert result.decision == HitlDecision.SKIPPED

    def test_policy_auto_approve_high_risk(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="p1", tool_name="shell", action="approve",
            condition={"param_contains": {"cmd": "git push"}},
        ))
        manager = HitlManager(policy_engine=engine, min_risk_for_confirm="medium")
        tool = FakeShellTool()
        result = manager.check(tool, {"cmd": "git push origin"})
        assert result.decision == HitlDecision.POLICY_APPROVED

    def test_policy_auto_deny(self):
        engine = PolicyEngine()
        engine.add_rule(PolicyRule(
            id="p2", tool_name="file_write", action="deny",
            condition={"param_regex": {"path": r"tests/.*"}},
        ))
        manager = HitlManager(policy_engine=engine, min_risk_for_confirm="medium")
        tool = FakeWriteTool()
        result = manager.check(tool, {"path": "tests/foo.py"})
        assert result.decision == HitlDecision.POLICY_DENIED

    def test_user_approve(self):
        def confirm(req):
            return (True, "")
        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        tool = FakeWriteTool()
        result = manager.check(tool, {"path": "src/main.py"})
        assert result.decision == HitlDecision.APPROVED

    def test_user_deny_with_note(self):
        def confirm(req):
            return (False, "don't modify this file")
        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        tool = FakeWriteTool()
        result = manager.check(tool, {"path": "src/main.py"})
        assert result.decision == HitlDecision.DENIED
        assert result.feedback_note == "don't modify this file"

    def test_feedback_injector_called(self):
        injected = []
        def confirm(req):
            return (False, "stop doing this")
        def injector(note):
            injected.append(note)

        manager = HitlManager(
            confirm_callback=confirm,
            min_risk_for_confirm="medium",
            feedback_injector=injector,
        )
        tool = FakeWriteTool()
        manager.check(tool, {"path": "x.py"})
        assert injected == ["stop doing this"]

    def test_no_callback_skips(self):
        manager = HitlManager(confirm_callback=None, min_risk_for_confirm="medium")
        tool = FakeWriteTool()
        result = manager.check(tool, {"path": "x.py"})
        assert result.decision == HitlDecision.SKIPPED

    def test_stats_accumulate(self):
        call_count = [0]
        def confirm(req):
            call_count[0] += 1
            return (call_count[0] % 2 == 1, "")  # alternating approve/deny

        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        tool = FakeWriteTool()
        manager.check(tool, {"path": "a.py"})  # approved
        manager.check(tool, {"path": "b.py"})  # denied
        manager.check(tool, {"path": "c.py"})  # approved

        stats = manager.stats
        assert stats.total_requests == 3
        assert stats.approvals == 2
        assert stats.denials == 1

    def test_thought_passed_to_request(self):
        received = []
        def confirm(req):
            received.append(req.thought)
            return (True, "")

        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        tool = FakeCommitTool()
        manager.check(tool, {"message": "fix bug"}, thought="I need to commit the fix")
        assert received == ["I need to commit the fix"]


# ---------------------------------------------------------------------------
# Integration: ToolRegistry + HitlManager
# ---------------------------------------------------------------------------

class TestToolRegistryHitl:
    def test_registry_with_hitl_approve(self):
        def confirm(req):
            return (True, "")
        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        registry = ToolRegistry(hitl_manager=manager)
        registry.register(FakeWriteTool())

        result = registry.execute_tool("file_write", {"path": "x.py"}, thought="writing file")
        assert result.success
        assert result.output == "written"

    def test_registry_with_hitl_deny(self):
        def confirm(req):
            return (False, "not allowed")
        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        registry = ToolRegistry(hitl_manager=manager)
        registry.register(FakeWriteTool())

        result = registry.execute_tool("file_write", {"path": "x.py"})
        assert not result.success
        assert "denied" in result.error.lower()
        assert "not allowed" in result.error

    def test_registry_no_hitl_passthrough(self):
        registry = ToolRegistry(hitl_manager=None)
        registry.register(FakeWriteTool())
        result = registry.execute_tool("file_write", {"path": "x.py"})
        assert result.success

    def test_registry_read_tool_skips_hitl(self):
        call_count = [0]
        def confirm(req):
            call_count[0] += 1
            return (True, "")
        manager = HitlManager(confirm_callback=confirm, min_risk_for_confirm="medium")
        registry = ToolRegistry(hitl_manager=manager)
        registry.register(FakeReadTool())

        result = registry.execute_tool("file_read", {})
        assert result.success
        assert call_count[0] == 0  # confirm was never called


# ---------------------------------------------------------------------------
# Integration: ShellTool classify_risk
# ---------------------------------------------------------------------------

class TestShellToolRiskClassification:
    def test_real_shell_tool_classify(self):
        from tools.shell_tool import ShellTool
        tool = ShellTool()
        assert tool.classify_risk({"cmd": "ls -la"}) == RiskLevel.NONE
        assert tool.classify_risk({"cmd": "cat file.py"}) == RiskLevel.NONE
        assert tool.classify_risk({"cmd": "pytest tests/"}) == RiskLevel.NONE
        assert tool.classify_risk({"cmd": "git status"}) == RiskLevel.NONE

    def test_real_shell_tool_dangerous(self):
        from tools.shell_tool import ShellTool
        tool = ShellTool()
        assert tool.classify_risk({"cmd": "rm -rf build/"}) == RiskLevel.HIGH
        assert tool.classify_risk({"cmd": "git commit -m fix"}) == RiskLevel.HIGH
        assert tool.classify_risk({"cmd": "pip install requests"}) == RiskLevel.HIGH

    def test_real_shell_tool_low(self):
        from tools.shell_tool import ShellTool
        tool = ShellTool()
        # Commands that are not readonly but also not in _CONFIRM_KEYWORDS
        assert tool.classify_risk({"cmd": "python main.py"}) == RiskLevel.LOW

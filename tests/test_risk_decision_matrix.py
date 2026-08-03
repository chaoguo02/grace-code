"""Phase 2: RiskLevel + TrustAccumulator 联合决策 — Target 测试。

对齐 Claude Code "静态规则 + 动态信任累积"：LOW/NONE 风险工具在
被用户确认足够多次后自动放行（减少打扰）；HIGH/MEDIUM 风险无论
信任多高都强制 ASK；确认增信任、拒绝降信任、信任随时间衰减。
"""

from __future__ import annotations

import pytest

from core.base import BaseTool, ToolMetadata, ToolResult
from hitl.pipeline import (
    PermissionPipeline, PermissionDecision, PromptAction, PromptDecision,
)
from hitl.permission_rule import PermissionRule
from hitl.trust_accumulator import SessionTrustAccumulator, compute_trust_key


# ── 假工具 ────────────────────────────────────────────────────────────────

class _LowRiskTool(BaseTool):
    name = "LowTool"
    description = "test"
    parameters_schema = {"type": "object", "properties": {}}
    metadata = ToolMetadata()

    @property
    def risk_level(self) -> str:
        return "low"

    def classify_risk(self, params):
        return "low"

    def execute(self, params):
        return ToolResult(success=True, output="ok")


class _MediumRiskTool(BaseTool):
    name = "MedTool"
    description = "test"
    parameters_schema = {"type": "object", "properties": {}}
    metadata = ToolMetadata()

    @property
    def risk_level(self) -> str:
        return "medium"

    def classify_risk(self, params):
        return "medium"

    def execute(self, params):
        return ToolResult(success=True, output="ok")


class _HighRiskTool(BaseTool):
    name = "HighTool"
    description = "test"
    parameters_schema = {"type": "object", "properties": {}}
    metadata = ToolMetadata()

    @property
    def risk_level(self) -> str:
        return "high"

    def classify_risk(self, params):
        return "high"

    def execute(self, params):
        return ToolResult(success=True, output="ok")


def _ask_rule(tool_name: str) -> PermissionRule:
    return PermissionRule.parse(tool_name, tier="ask")


# ── 2A: 联合决策 ──────────────────────────────────────────────────────────

def test_trusted_low_risk_ask_is_auto_allowed():
    acc = SessionTrustAccumulator(threshold=2)
    key = compute_trust_key("LowTool", {})
    acc.record_approval(key)
    acc.record_approval(key)
    assert acc.is_trusted(key)

    pipeline = PermissionPipeline(
        rules=[_ask_rule("LowTool")], trust_accumulator=acc,
    )
    result = pipeline.check(_LowRiskTool(), {})

    assert result.decision is PermissionDecision.ALLOW
    assert result.feedback == "TRUST_AUTO_ALLOW"


def test_untracked_low_risk_still_asks():
    acc = SessionTrustAccumulator(threshold=2)
    pipeline = PermissionPipeline(
        rules=[_ask_rule("LowTool")], trust_accumulator=acc,
    )
    # 无 confirm callback → fail closed（DENY），绝不 auto-allow
    result = pipeline.check(_LowRiskTool(), {})
    assert result.decision is PermissionDecision.DENY
    assert result.feedback != "TRUST_AUTO_ALLOW"


def test_high_risk_never_auto_allowed_even_when_trusted():
    """R-C 硬规则：HIGH 风险无论 trust 多高都强制 ASK。"""
    acc = SessionTrustAccumulator(threshold=1)
    key = compute_trust_key("HighTool", {})
    acc.record_approval(key)
    assert acc.is_trusted(key)

    pipeline = PermissionPipeline(
        rules=[_ask_rule("HighTool")], trust_accumulator=acc,
    )
    result = pipeline.check(_HighRiskTool(), {})
    # 走 Layer 6 → 无 callback → DENY（而非 TRUST_AUTO_ALLOW）
    assert result.decision is PermissionDecision.DENY
    assert result.feedback != "TRUST_AUTO_ALLOW"


def test_medium_risk_not_auto_allowed_even_when_trusted():
    """MEDIUM 风险保守处理：不自动放行。"""
    acc = SessionTrustAccumulator(threshold=1)
    key = compute_trust_key("MedTool", {})
    acc.record_approval(key)
    pipeline = PermissionPipeline(
        rules=[_ask_rule("MedTool")], trust_accumulator=acc,
    )
    result = pipeline.check(_MediumRiskTool(), {})
    assert result.feedback != "TRUST_AUTO_ALLOW"


def test_no_accumulator_keeps_ask_behavior():
    """无 accumulator 时行为不变（不引入自动放行）。"""
    pipeline = PermissionPipeline(rules=[_ask_rule("LowTool")])
    result = pipeline.check(_LowRiskTool(), {})
    assert result.decision is PermissionDecision.DENY  # fail closed


# ── 2B: 反馈回路 ──────────────────────────────────────────────────────────

def test_confirmation_increases_trust_and_eventually_auto_allows():
    acc = SessionTrustAccumulator(threshold=2)
    confirmations = []

    def _confirm(req):
        confirmations.append(req.tool_name)
        return PromptDecision(action=PromptAction.ALLOW_ONCE)

    pipeline = PermissionPipeline(
        rules=[_ask_rule("LowTool")],
        trust_accumulator=acc,
        confirm_callback=_confirm,
    )
    tool = _LowRiskTool()
    key = compute_trust_key(tool.name, {})

    # 前两次：用户确认 → trust 累积
    for _ in range(2):
        r = pipeline.check(tool, {})
        assert r.decision is PermissionDecision.ALLOW

    assert acc.trust_score(key) >= 2

    # 第三次：trusted low-risk → auto-allow，不再询问
    r3 = pipeline.check(tool, {})
    assert r3.feedback == "TRUST_AUTO_ALLOW"
    assert len(confirmations) == 2  # 第三次未弹确认


def test_rejection_decreases_trust():
    acc = SessionTrustAccumulator(threshold=2)
    key = compute_trust_key("LowTool", {})
    acc.record_approval(key)
    acc.record_approval(key)
    assert acc.is_trusted(key)

    acc.record_rejection(key)
    assert not acc.is_trusted(key), "拒绝后信任必须下降"


def test_trust_decays_over_time():
    """每 10s 信任衰减 50%（测试参数）——防止单次长会话信任无限累积。"""
    acc = SessionTrustAccumulator(
        threshold=1, decay_interval_s=10, decay_rate=0.5,
    )
    key = compute_trust_key("LowTool", {})
    acc.record_approval(key, now=1000.0)
    assert acc.is_trusted(key, now=1000.0)
    # 11s 后：1 * (1-0.5) = 0.5 < 1 → 不再信任
    assert not acc.is_trusted(key, now=1011.0)

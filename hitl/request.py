"""
hitl/request.py

HITL 数据模型：Request / Result / Decision / Stats。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HitlDecision(str, Enum):
    """确认决策类型。"""
    APPROVED = "approved"                # 用户批准
    DENIED = "denied"                    # 用户拒绝
    POLICY_APPROVED = "policy_approved"  # Policy 自动批准
    POLICY_DENIED = "policy_denied"      # Policy 自动拒绝
    SKIPPED = "skipped"                  # 风险低于阈值，跳过确认
    ALWAYS_ALLOWED = "always_allowed"    # 用户选择 "Always Allow"


@dataclass
class HitlRequest:
    """一次 HITL 确认请求。"""
    tool_name: str
    params: dict[str, Any]
    risk_level: str
    thought: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: float = field(default_factory=time.time)

    def summary(self, max_params_len: int = 120) -> str:
        """人类可读的单行摘要。"""
        params_str = str(self.params)
        if len(params_str) > max_params_len:
            params_str = params_str[:max_params_len] + "..."
        return f"[{self.risk_level}] {self.tool_name}({params_str})"


@dataclass
class HitlResult:
    """确认决策结果。"""
    decision: HitlDecision
    feedback_note: str = ""     # 用户 deny 时附带的反馈文本
    wait_ms: float = 0.0       # 用户思考耗时
    policy_rule: str = ""      # 命中的 policy rule ID（如有）

    @property
    def is_approved(self) -> bool:
        return self.decision in (
            HitlDecision.APPROVED,
            HitlDecision.POLICY_APPROVED,
            HitlDecision.SKIPPED,
            HitlDecision.ALWAYS_ALLOWED,
        )

    @property
    def is_denied(self) -> bool:
        return self.decision in (
            HitlDecision.DENIED,
            HitlDecision.POLICY_DENIED,
        )


@dataclass
class HitlStats:
    """会话级累计统计。"""
    total_requests: int = 0
    approvals: int = 0
    denials: int = 0
    policy_matches: int = 0
    total_wait_ms: float = 0.0

    @property
    def approval_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.approvals / self.total_requests

    @property
    def avg_wait_ms(self) -> float:
        actual = self.total_requests - self.policy_matches
        if actual <= 0:
            return 0.0
        return self.total_wait_ms / actual

    def record(self, result: HitlResult) -> None:
        """记录一次决策结果。"""
        self.total_requests += 1
        if result.is_approved:
            self.approvals += 1
        else:
            self.denials += 1
        if result.decision in (HitlDecision.POLICY_APPROVED, HitlDecision.POLICY_DENIED):
            self.policy_matches += 1
        self.total_wait_ms += result.wait_ms

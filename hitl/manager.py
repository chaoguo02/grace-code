"""
hitl/manager.py

HITL 中央管理器（Legacy — 保留向后兼容）。

新代码应使用 hitl.pipeline.PermissionPipeline。
此类保留用于：
- 旧代码路径中仍 import HitlManager 的地方
- 无 PermissionPipeline 时的降级路径

决策流（legacy）：
1. tool.classify_risk(params) → 获取风险等级
2. risk < min_risk_for_confirm → SKIPPED（自动放行）
3. PolicyEngine.match() → POLICY_APPROVED / POLICY_DENIED
4. confirm_callback(request) → 用户决定 APPROVED / DENIED
5. 无 callback → SKIPPED
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from hitl.request import HitlDecision, HitlRequest, HitlResult, HitlStats
from hitl.policy import PolicyEngine
from tools.base import RiskLevel

if TYPE_CHECKING:
    from tools.base import BaseTool

# confirm_callback 签名: 接收 HitlRequest，返回 (approved, note)
ConfirmToolCallback = Callable[["HitlRequest"], tuple[bool, str]]

# 风险等级排序（用于比较）
_RISK_ORDER = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}


def _risk_value(level: str) -> int:
    """将风险等级字符串转为可比较的数值。"""
    return _RISK_ORDER.get(level, 0)


class HitlManager:
    """
    同步 HITL 管理器。

    实例化后传给 ToolRegistry(hitl_manager=...)。
    每次工具调用都经过 check() 方法审批。
    """

    def __init__(
        self,
        confirm_callback: ConfirmToolCallback | None = None,
        policy_engine: PolicyEngine | None = None,
        min_risk_for_confirm: str = RiskLevel.MEDIUM,
        feedback_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._confirm_callback = confirm_callback
        self._policy = policy_engine or PolicyEngine()
        self._min_risk = min_risk_for_confirm
        self._feedback_injector = feedback_injector
        self._stats = HitlStats()

    @property
    def stats(self) -> HitlStats:
        return self._stats

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy

    def check(
        self,
        tool: "BaseTool",
        params: dict[str, Any],
        thought: str = "",
    ) -> HitlResult:
        """
        主入口：决定是否允许工具执行。

        Returns:
            HitlResult — 检查 .is_approved 或 .is_denied
        """
        # 1. 动态风险分类
        risk = tool.classify_risk(params)

        # 2. 风险低于阈值 → 自动放行
        if _risk_value(risk) < _risk_value(self._min_risk):
            result = HitlResult(decision=HitlDecision.SKIPPED)
            self._stats.record(result)
            return result

        # 3. Policy 引擎匹配
        policy_match = self._policy.match(tool.name, params)
        if policy_match is not None:
            if policy_match.action == "approve":
                result = HitlResult(
                    decision=HitlDecision.POLICY_APPROVED,
                    policy_rule=policy_match.id,
                )
            else:
                result = HitlResult(
                    decision=HitlDecision.POLICY_DENIED,
                    policy_rule=policy_match.id,
                )
            self._stats.record(result)
            return result

        # 4. 用户确认
        if self._confirm_callback is None:
            result = HitlResult(decision=HitlDecision.SKIPPED)
            self._stats.record(result)
            return result

        request = HitlRequest(
            tool_name=tool.name,
            params=params,
            risk_level=risk,
            thought=thought,
        )

        t0 = time.time()
        approved, note = self._confirm_callback(request)
        wait_ms = (time.time() - t0) * 1000

        if approved:
            result = HitlResult(
                decision=HitlDecision.APPROVED,
                wait_ms=wait_ms,
            )
        else:
            result = HitlResult(
                decision=HitlDecision.DENIED,
                feedback_note=note,
                wait_ms=wait_ms,
            )
            # 反馈注入
            if note and self._feedback_injector:
                self._feedback_injector(note)

        self._stats.record(result)
        return result

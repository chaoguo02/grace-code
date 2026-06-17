"""
hitl/ — Human-in-the-Loop 框架。

统一管理所有工具的人工确认：
- HitlManager: 中央拦截器，嵌入 ToolRegistry.execute_tool()
- PolicyEngine: 自动审批/拒绝规则
- RiskLevel: 工具风险分级

设计原则：
- 同步阻塞（当前 agent 单线程）
- HitlManager=None 时所有行为不变（向后兼容）
- ShellTool 的 L0 硬拦截保留在 execute() 内
"""

from hitl.request import HitlDecision, HitlRequest, HitlResult, HitlStats
from hitl.manager import HitlManager
from hitl.policy import PolicyEngine

__all__ = [
    "HitlDecision",
    "HitlRequest",
    "HitlResult",
    "HitlStats",
    "HitlManager",
    "PolicyEngine",
]

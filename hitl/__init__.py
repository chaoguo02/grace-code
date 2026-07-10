"""
hitl/ — Human-in-the-Loop 权限框架。

核心组件（新）：
- PermissionPipeline: 5 层权限管道（对齐 Claude Code ToolPermissionPipeline）
- PermissionRule: Tool(pattern) 规则解析与匹配
- PermissionResult/PromptDecision: 管道决策数据模型

Legacy 组件（保留向后兼容）：
- HitlManager: 旧中央拦截器
- PolicyEngine: YAML 规则引擎
"""

from hitl.request import HitlDecision, HitlRequest, HitlResult, HitlStats
from hitl.manager import HitlManager
from hitl.policy import PolicyEngine
from hitl.pipeline import PermissionPipeline, PermissionResult, PermissionRequest, PromptDecision
from hitl.permission_rule import PermissionRule

__all__ = [
    # New (primary)
    "PermissionPipeline",
    "PermissionResult",
    "PermissionRequest",
    "PermissionRule",
    "PromptDecision",
    # Legacy (backward compat)
    "HitlDecision",
    "HitlRequest",
    "HitlResult",
    "HitlStats",
    "HitlManager",
    "PolicyEngine",
]

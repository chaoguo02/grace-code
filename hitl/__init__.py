"""
hitl/ — Human-in-the-Loop 权限框架。

核心组件（新）：
- PermissionPipeline: 5 层权限管道（对齐 Claude Code ToolPermissionPipeline）
- PermissionRule: Tool(pattern) 规则解析与匹配
- PermissionResult/PromptDecision: 管道决策数据模型

"""

from hitl.pipeline import (
    PermissionDecision,
    PermissionLayer,
    PermissionPipeline,
    PermissionRequest,
    PermissionResult,
    PromptAction,
    PromptDecision,
)
from hitl.permission_rule import PermissionRule, PermissionRuleTier

__all__ = [
    # New (primary)
    "PermissionPipeline",
    "PermissionDecision",
    "PermissionLayer",
    "PermissionResult",
    "PermissionRequest",
    "PermissionRule",
    "PermissionRuleTier",
    "PromptAction",
    "PromptDecision",
]

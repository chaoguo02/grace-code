"""
context/structured.py

结构化上下文分层系统。

借鉴 Claude Code 的四层上下文架构：
- Layer 0 (System Identity): 极稳定，可缓存
- Layer 1 (Project Context): session 级稳定
- Layer 2 (Task Context): 轮次级变化

设计原则：
- Layer 0 + Layer 1 = Prompt Cache 稳定前缀（最大化 cache hit rate）
- Layer 2 = 动态后缀（每轮重建）
- 工具定义排序确定化（按 name 字典序）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ContextPriority(IntEnum):
    """上下文层优先级。数值越小优先级越高（越不容易被裁剪）。"""
    SYSTEM = 0       # 角色定义、安全约束、工具规则
    PROJECT = 1      # RepoMap、Skills、Memory
    TASK = 2         # 当前任务、对话历史


@dataclass
class ContextLayer:
    """上下文的一个分层片段。"""
    name: str
    priority: ContextPriority
    content: str
    cacheable: bool = False
    max_tokens: int = 0         # 0 = 无限制，由外部 budget 控制

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()

    def __repr__(self) -> str:
        length = len(self.content)
        return f"ContextLayer({self.name!r}, priority={self.priority.name}, len={length}, cacheable={self.cacheable})"


@dataclass
class StructuredContext:
    """
    组装和管理结构化上下文。

    职责：
    1. 维护分层的 context layers
    2. 按优先级排序确保稳定前缀在前
    3. 标记 cache boundary（稳定/动态分界）
    4. 在 token 预算内裁剪低优先级内容
    """
    layers: list[ContextLayer] = field(default_factory=list)

    def add_layer(self, layer: ContextLayer) -> None:
        self.layers.append(layer)

    def get_stable_prefix(self) -> str:
        """返回可缓存的稳定前缀（Layer 0 + Layer 1）。"""
        parts = []
        for layer in self._sorted_layers():
            if layer.cacheable and not layer.is_empty:
                parts.append(layer.content)
        return "\n\n".join(parts)

    def get_dynamic_suffix(self) -> str:
        """返回动态后缀（Layer 2）。"""
        parts = []
        for layer in self._sorted_layers():
            if not layer.cacheable and not layer.is_empty:
                parts.append(layer.content)
        return "\n\n".join(parts)

    def build_system_content(self, enable_caching: bool = False) -> "str | list[dict[str, Any]]":
        """
        组装最终的 system prompt content。

        如果 enable_caching=True（Anthropic 模式），返回结构化 content blocks：
        [
            {"type": "text", "text": <stable_prefix>, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": <dynamic_suffix>}
        ]

        否则返回纯文本拼接。
        """
        stable = self.get_stable_prefix()
        dynamic = self.get_dynamic_suffix()

        if not enable_caching:
            parts = [p for p in (stable, dynamic) if p]
            return "\n\n".join(parts)

        blocks = []
        if stable:
            blocks.append({
                "type": "text",
                "text": stable,
                "cache_control": {"type": "ephemeral"},
            })
        if dynamic:
            blocks.append({"type": "text", "text": dynamic})
        return blocks if blocks else ""

    def total_content_length(self) -> int:
        return sum(len(layer.content) for layer in self.layers if not layer.is_empty)

    def layer_summary(self) -> list[dict[str, Any]]:
        """返回各层摘要信息（用于调试/统计）。"""
        return [
            {
                "name": layer.name,
                "priority": layer.priority.name,
                "cacheable": layer.cacheable,
                "chars": len(layer.content),
                "empty": layer.is_empty,
            }
            for layer in self._sorted_layers()
        ]

    def _sorted_layers(self) -> list[ContextLayer]:
        """按优先级排序（稳定的在前）。"""
        return sorted(self.layers, key=lambda l: (l.priority, l.name))

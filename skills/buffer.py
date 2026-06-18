"""
skills/buffer.py

SkillContextBuffer — 限制同时激活的 skill 数量，防止上下文膨胀。

设计：
- LRU 淘汰策略：超出上限时淘汰最早激活的 skill
- 每个 skill 内容截断到 MAX_TOKENS_PER_SKILL（字符估算）
- 总上限 MAX_ACTIVE 个同时激活的 skill
"""

from __future__ import annotations

import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


class SkillContextBuffer:
    """
    管理激活的 skill 上下文 buffer。

    限制同时存在于对话中的 skill 数量，避免上下文窗口被多个 skill body 占满。
    采用 LRU 策略：新 skill 激活时，若超出上限则淘汰最早激活的。
    """

    MAX_ACTIVE = 3
    MAX_CHARS_PER_SKILL = 5000  # ~1250 tokens

    def __init__(self, max_active: int = MAX_ACTIVE, max_chars: int = MAX_CHARS_PER_SKILL):
        self._max_active = max_active
        self._max_chars = max_chars
        self._active: OrderedDict[str, str] = OrderedDict()

    def activate(self, name: str, content: str) -> str:
        """
        激活 skill，返回截断后的内容。

        如果 skill 已激活，将其移到最新位置。
        如果超出上限，淘汰最早的 skill。
        """
        # 截断内容
        if len(content) > self._max_chars:
            content = content[:self._max_chars] + "\n\n... (truncated)"
            logger.debug("Skill '%s' content truncated to %d chars", name, self._max_chars)

        # 如果已存在，移到末尾（最新）
        if name in self._active:
            self._active.move_to_end(name)
            self._active[name] = content
            return content

        # 超出上限，淘汰最早的
        while len(self._active) >= self._max_active:
            evicted_name, _ = self._active.popitem(last=False)
            logger.debug("Skill buffer evicted: '%s' (max %d)", evicted_name, self._max_active)

        self._active[name] = content
        return content

    def active_skills(self) -> list[str]:
        """返回当前激活的 skill 名称列表（从最早到最新）。"""
        return list(self._active.keys())

    def is_active(self, name: str) -> bool:
        """检查 skill 是否已激活。"""
        return name in self._active

    def clear(self) -> None:
        """清空所有激活的 skill。"""
        self._active.clear()

"""
skills/tool.py

SkillTool — Agent 可调用的技能工具 (CC-aligned with contextModifier).

当 LLM 在 system prompt 中看到 "Available Skills" 列表后，
可以通过此工具调用指定的 skill，获取 skill 的渲染内容。

CC 对齐:
  - 返回 SkillContextModifier: 携带 allowed-tools/disallowed-tools/model/effort
  - contextModifier 被 PolicyAwareToolRegistry 消费, 影响后续工具调用
  - context: fork 时在隔离子代理中执行 (S2)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from skills.registry import SkillRegistry
    from skills.buffer import SkillContextBuffer


@dataclass
class SkillContextModifier:
    """CC-aligned contextModifier: skill 执行后对 agent 运行时的修改。

    PolicyAwareToolRegistry 消费此对象来:
      - allowed_tools → with_skill_restrictions (SK-05)
      - disallowed_tools → 从工具池移除 (SK-06)
      - model → 覆盖 LLM 模型
      - effort → 覆盖推理力度
      - context → "fork" 时在隔离子代理中执行 (S2)
    """
    allowed_tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    model: str = ""
    effort: str = ""
    context: str = ""  # "" | "fork"


class SkillTool(BaseTool):
    """
    LLM-initiated skill invocation tool (fallback — aligned with Claude Code).

    PRIMARY PATH (Claude Code alignment): users type /skill-name directly
    in the chat REPL → content is injected into shared_history without a
    tool_use round-trip (see entry/chat.py:_handle_slash_skill).

    THIS TOOL (fallback): the LLM can also invoke Skill(skill_name=...)
    to load a skill mid-turn. This is the path used when:
    - The LLM semantically matches a skill from the system prompt listing
    - The model chooses to load a skill autonomously (not user-triggered)

    Flow:
    1. LLM calls Skill(skill_name="code-review", arguments="...")
    2. SkillTool loads and renders the skill from SkillRegistry
    3. SkillContextBuffer manages context budget
    4. Returns ToolResult with rendered skill content as output
    5. Agent sees the skill content in the next turn's observation
    """

    def __init__(
        self,
        skill_registry: "SkillRegistry",
        buffer: "SkillContextBuffer | None" = None,
    ) -> None:
        self._skill_registry = skill_registry
        self._buffer = buffer

    aliases = ("use_skill",)

    @property
    def name(self) -> str:
        return "Skill"

    @property
    def description(self) -> str:
        return (
            "Invoke a skill by name. Skills provide specialized, reusable instructions. "
            "Use the skill name as listed in Available Skills in the system prompt. "
            "Users can also invoke skills directly with /skill-name."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to invoke (as listed in Available Skills)",
                },
                "arguments": {
                    "type": "string",
                    "description": "Arguments to pass to the skill (replaces $ARGUMENTS in skill body)",
                },
            },
            "required": ["skill_name"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        skill_name = params.get("skill_name", "")
        arguments = params.get("arguments", "")

        if not skill_name:
            return ToolResult(
                success=False, output="",
                error="'skill_name' is required",
            )

        rendered = self._skill_registry.load_and_render(skill_name, arguments)

        if rendered is None:
            available = [m.name for m in self._skill_registry.list_skills()]
            return ToolResult(
                success=False, output="",
                error=f"Skill '{skill_name}' not found. Available: {', '.join(available)}",
            )

        # 通过 buffer 管理上下文用量
        if self._buffer:
            rendered = self._buffer.activate(skill_name, rendered)

        return ToolResult(
            success=True,
            output=f"[Skill: {skill_name}]\n\n{rendered}",
        )

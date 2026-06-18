"""
skills/tool.py

SkillTool — Agent 可调用的技能工具。

当 LLM 在 system prompt 中看到 "Available Skills" 列表后，
可以通过此工具调用指定的 skill，获取 skill 的渲染内容。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from skills.registry import SkillRegistry
    from skills.buffer import SkillContextBuffer


class SkillTool(BaseTool):
    """
    Agent 可调用的 Skill 工具。

    LLM 通过此工具触发 skill：
    1. 调用 use_skill(skill_name="code-review", arguments="...")
    2. SkillTool 从 SkillRegistry 加载并渲染 skill
    3. 通过 SkillContextBuffer 管理上下文用量
    4. 返回 ToolResult，渲染后的 skill 内容作为 output
    5. Agent 在下一轮看到 skill 内容，按照指示执行
    """

    def __init__(
        self,
        skill_registry: "SkillRegistry",
        buffer: "SkillContextBuffer | None" = None,
    ) -> None:
        self._registry = skill_registry
        self._buffer = buffer

    @property
    def name(self) -> str:
        return "use_skill"

    @property
    def description(self) -> str:
        available = [m.name for m in self._registry.list_skills()]
        skills_list = ", ".join(available) if available else "(none)"
        return (
            f"Invoke a predefined skill to get specialized instructions. "
            f"Available skills: {skills_list}"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        skill_names = [m.name for m in self._registry.list_skills()]
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": skill_names if skill_names else ["(none)"],
                    "description": "Name of the skill to invoke",
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

        rendered = self._registry.load_and_render(skill_name, arguments)

        if rendered is None:
            available = [m.name for m in self._registry.list_skills()]
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

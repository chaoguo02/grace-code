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

import copy
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from core.base import BaseTool, ToolEffect, ToolMetadata, ToolResult

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
      - scope → TURN (auto-popped after tool call) or RUN (cleaned at end-of-run)
    """
    allowed_tools: frozenset[str] = frozenset()
    disallowed_tools: frozenset[str] = frozenset()
    mcp_servers: frozenset[str] = frozenset()
    model: str = ""
    effort: str = ""
    context: str = ""  # "" | "fork"
    scope: str = "turn"  # "turn" | "run" — Phase 1 ModifierScope


class SkillTool(BaseTool):
    """
    LLM-initiated skill invocation tool (fallback).

    DEPRECATED Phase 1: Replaced by per-skill ``SkillActivationTool``
    instances registered directly in ``ToolRegistry._tools``.  Renamed
    to ``__legacy_skill_loader`` with ``visible_to_llm=False`` —
    excluded from LLM schemas.  Preserved only for internal CLI/HTTP
    paths that still use the generic loader pattern.

    PRIMARY PATH (Claude Code alignment): users type /skill-name directly
    in the chat REPL → content is injected into shared_history without a
    tool_use round-trip (see entry/chat.py:_handle_slash_skill).
    """

    def __init__(
        self,
        skill_registry: "SkillRegistry",
        buffer: "SkillContextBuffer | None" = None,
        runtime: Any = None,
    ) -> None:
        self._skill_registry = skill_registry
        self._buffer = buffer
        self._runtime = runtime

    aliases = ("use_skill",)

    @property
    def name(self) -> str:
        return "__legacy_skill_loader"  # Phase 1: deprecated

    @property
    def visible_to_llm(self) -> bool:
        return False  # Phase 1: excluded from LLM schemas

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

        meta = self._skill_registry.get_skill_meta(skill_name)
        if meta is not None and not meta.model_invocable:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Skill '{skill_name}' cannot be invoked by the model; "
                    "the user must invoke it directly."
                ),
            )
        if meta is not None and meta.context == "fork":
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Skill '{skill_name}' requires fork context; inline Skill "
                    "execution cannot safely emulate an isolated subagent."
                ),
            )

        rendered = self._skill_registry.load_and_render(
            skill_name,
            arguments,
            runtime=self._runtime,
        )

        if rendered is None:
            available = [m.name for m in self._skill_registry.list_skills()]
            return ToolResult(
                success=False, output="",
                error=f"Skill '{skill_name}' not found. Available: {', '.join(available)}",
            )

        # 通过 buffer 管理上下文用量
        if self._buffer:
            rendered = self._buffer.activate(skill_name, rendered)

        # Build CC-aligned SkillContextModifier (consumed by PolicyAwareToolRegistry)
        modifier = SkillContextModifier()
        if meta is not None:
            modifier = SkillContextModifier(
                allowed_tools=meta.allowed_tools,
                disallowed_tools=meta.disallowed_tools,
                mcp_servers=meta.mcp_servers,
                model=meta.model,
                effort=meta.effort,
                context=meta.context,
            )
        from agent.session.evidence_requirements import (
            compile_skill_tool_calls,
        )
        compiled_calls = (
            compile_skill_tool_calls(meta.evidence_contract, arguments)
            if meta is not None else []
        )

        return ToolResult(
            success=True,
            output=f"[Skill: {skill_name}]\n\n{rendered}",
            metadata={
                "skill_modifier": modifier,
                "evidence": {
                    "skill_name": skill_name,
                    "skill_fingerprint": _skill_fingerprint(meta),
                    "mcp_dependencies": sorted(meta.mcp_servers) if meta.mcp_servers else [],
                    "arguments_digest": _sha256(arguments or ""),
                    "required_tool_calls": [
                        {
                            "tool": requirement.tool,
                            "arguments_match": dict(
                                requirement.arguments_match
                            ),
                            "minimum_count": requirement.minimum_count,
                        }
                        for requirement in compiled_calls
                    ],
                },
            },
        )

    def with_run_context(self, context: Any) -> "SkillTool":
        """Preserve skill tool behavior while carrying any bound runtime fact."""
        bound = copy.copy(self)
        runtime = getattr(context, "runtime", None)
        if runtime is not None:
            bound._runtime = runtime
        return bound


# ── SkillActivationTool — Phase 1 Unified Execution ────────────────────


class SkillActivationTool(BaseTool):
    """A Skill modeled as a first-class BaseTool in the unified ToolRegistry.

    Phase 1: Each discovered skill is registered as a SkillActivationTool
    with the skill's original frontmatter name (no prefixes).  The LLM
    sees skills alongside native and MCP tools as flat-namespace entries.

    execute() delegates to the existing SkillTool._internal_execute() logic
    and returns a SkillContextModifier with scope=TURN (auto-popped by
    ToolExecutionPipeline after this tool call completes).
    """

    def __init__(
        self,
        metadata: "SkillMetadata",
        *,
        skill_registry: Any = None,
        skill_buffer: Any = None,
        source: str = "project",
    ) -> None:
        self._meta = metadata
        self._skill_registry = skill_registry
        self._skill_buffer = skill_buffer
        self._source = source

    @property
    def name(self) -> str:
        """Original frontmatter name — flat namespace, no prefix."""
        return self._meta.name

    @property
    def description(self) -> str:
        return (
            self._meta.description
            or f"Activate the '{self._meta.name}' skill"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    @property
    def metadata(self) -> "ToolMetadata":
        return ToolMetadata(
            effects=frozenset({
                ToolEffect.READ_AGENT_STATE,
                ToolEffect.WRITE_AGENT_STATE,
            }),
            source=self._source,
        )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return False  # skill activation modifies agent state

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """Delegate to SkillTool's internal execution logic.

        Phase 2: Always returns a TURN-scoped SkillContextModifier in metadata,
        even on failure — so the after_tool_use hook can always pop the modifier.
        """
        modifier = SkillContextModifier(
            allowed_tools=self._meta.allowed_tools,
            disallowed_tools=self._meta.disallowed_tools,
            mcp_servers=self._meta.mcp_servers,
            model=self._meta.model,
            effort=self._meta.effort,
            context=self._meta.context,
            scope="turn",
        )
        if self._skill_registry is None:
            return ToolResult(
                success=False,
                output="",
                error="Skill registry not available for activation",
                metadata={"skill_modifier": modifier},
            )
        rendered = self._skill_registry.load_and_render(
            self._meta.name,
            project_dir=getattr(self._skill_registry, "_project_dir", ""),
        )
        if rendered is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Skill '{self._meta.name}' failed to load",
                metadata={"skill_modifier": modifier},
            )
        return ToolResult(
            success=True,
            output=f"[Skill: {self._meta.name}]\n\n{rendered}",
            metadata={"skill_modifier": modifier},
        )

    # ── Schema visibility ──────────────────────────────────────────
    # Phase 1: Legacy generic loader renamed to __legacy_skill_loader
    # with visible_to_llm=False.  SkillActivationTool is the new
    # per-skill entry point — always visible.

    @property
    def visible_to_llm(self) -> bool:
        # Override default True for __legacy_skill_loader; normal skills are True
        return getattr(self, "_visible_to_llm", True)

    @visible_to_llm.setter
    def visible_to_llm(self, value: bool) -> None:
        self._visible_to_llm = value


# ── Helpers ─────────────────────────────────────────────────────────────────


def _skill_fingerprint(meta) -> str:
    """Compatibility alias for the single fingerprint implementation."""
    from skills.activation import skill_fingerprint
    return skill_fingerprint(meta)


def _sha256(data: str) -> str:
    import hashlib
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()

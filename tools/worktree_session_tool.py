"""Worktree session tools — CC-aligned EnterWorktree / ExitWorktree.

Wraps the existing worktree infrastructure (agent/v2/worktree_tool.py,
tools/snapshot.py) as BaseTool subclasses for CC compatibility.
"""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolEffect, ToolMetadata, ToolResult


class EnterWorktreeTool(BaseTool):
    """Create or enter an isolated git worktree.

    CC-aligned: Creates a new git worktree on a new branch and switches
    the session into it. The worktree provides isolation for experimental
    changes without affecting the main working tree.
    """

    metadata = ToolMetadata(effects=frozenset({ToolEffect.WRITE_WORKSPACE}))

    @property
    def name(self) -> str:
        return "EnterWorktree"

    @property
    def description(self) -> str:
        return (
            "Create an isolated git worktree and switch into it. "
            "Pass a name for the new worktree (creates on a new branch). "
            "Pass a path to switch into an existing worktree instead. "
            "Use ExitWorktree to leave and clean up."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for a new worktree (mutually exclusive with path)",
                },
                "path": {
                    "type": "string",
                    "description": "Path to an existing worktree to switch into (mutually exclusive with name)",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # The actual worktree creation/switch is handled by a runtime
        # interceptor or PostToolUse hook. This tool signals intent.
        name = params.get("name", "")
        path = params.get("path", "")
        if not name and not path:
            return ToolResult(success=False, output="", error="Either 'name' or 'path' is required")
        target = name or path
        return ToolResult(
            success=True,
            output=(
                f"Entered worktree: {target}\n"
                "Changes in this worktree are isolated. Use ExitWorktree to return."
            ),
        )


class ExitWorktreeTool(BaseTool):
    """Exit a worktree session and return to the original directory."""

    metadata = ToolMetadata(effects=frozenset())

    @property
    def name(self) -> str:
        return "ExitWorktree"

    @property
    def description(self) -> str:
        return (
            "Exit the current worktree session and return to the original "
            "working directory. Pass 'keep' to preserve the worktree, "
            "'remove' to delete it along with its branch."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "'keep' to preserve the worktree, 'remove' to delete it",
                },
                "discard_changes": {
                    "type": "boolean",
                    "description": "Set to true to force removal even with uncommitted changes (only with action='remove')",
                },
            },
            "required": ["action"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        action = params.get("action", "keep")
        if action not in ("keep", "remove"):
            return ToolResult(success=False, output="", error="action must be 'keep' or 'remove'")

        return ToolResult(
            success=True,
            output=(
                f"Exited worktree (action={action}). "
                + ("Worktree preserved." if action == "keep" else "Worktree removed.")
            ),
        )

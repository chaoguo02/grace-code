"""Interaction Adapter — decouples user I/O from approval logic.

Claude Code Intent Layer principle: the approval loop should NOT be bound
to a specific I/O mechanism (click, web, API). This adapter abstracts the
interaction so the same Plan approval logic works with any frontend.

Default implementation: ClickAdapter (CLI). Replace with WebAdapter or
APIAdapter without touching v2_runner.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class ApprovalAction(str, Enum):
    EXECUTE = "execute"
    SAVE = "save"
    EDIT = "edit"
    REVISE = "revise"
    ABORT = "abort"


class PlanExecutionPolicy(str, Enum):
    """Declarative CLI behavior after a valid plan has been produced."""

    REVIEW = "review"
    SAVE = "save"
    EXECUTE = "execute"


@dataclass(frozen=True)
class ApprovalChoice:
    """Result of a single approval interaction."""
    action: ApprovalAction
    feedback: str = ""  # Only for "revise" action

    def __post_init__(self) -> None:
        if not isinstance(self.action, ApprovalAction):
            object.__setattr__(self, "action", ApprovalAction(self.action))


class InteractionAdapter(ABC):
    """Abstract interface for user interaction during plan approval.

    Implementations: ClickAdapter (CLI), WebAdapter, APIAdapter.
    """

    @abstractmethod
    def show_plan(self, plan_text: str, plan_path: str) -> None:
        """Display the plan to the user."""

    @abstractmethod
    def prompt_approval(self) -> ApprovalChoice:
        """Ask the user what to do. Returns an ApprovalChoice."""

    @abstractmethod
    def show_message(self, text: str, style: str = "info") -> None:
        """Display a one-line message. style: info, success, warning, error."""

    @abstractmethod
    def prompt_feedback(self) -> str:
        """Ask the user for revision feedback."""


# ── CLI implementation ─────────────────────────────────────────────────

class ClickAdapter(InteractionAdapter):
    """CLI implementation using click for prompts and echo for output."""

    def __init__(self, preselected_action: ApprovalAction | None = None) -> None:
        self._preselected_action = preselected_action

    def show_plan(self, plan_text: str, plan_path: str) -> None:
        import click
        click.echo("\n" + "─" * 60)
        click.echo(_bold("  Plan ready for review"))
        click.echo(f"  File: {plan_path}")
        click.echo("─" * 60)
        click.echo(plan_text.rstrip())
        click.echo("─" * 60)
        if self._preselected_action is None:
            click.echo("  [1] Execute plan")
            click.echo("  [2] Edit plan file")
            click.echo("  [3] Tell the agent what to change (re-plan)")
            click.echo("  [4] Save plan and exit (default)")
            click.echo("  [5] Abort")
            click.echo("─" * 60)

    def prompt_approval(self) -> ApprovalChoice:
        import click
        if self._preselected_action is not None:
            return ApprovalChoice(action=self._preselected_action)
        _MAP = {
            "1": ApprovalAction.EXECUTE,
            "y": ApprovalAction.EXECUTE,
            "yes": ApprovalAction.EXECUTE,
            "2": ApprovalAction.EDIT,
            "e": ApprovalAction.EDIT,
            "3": ApprovalAction.REVISE,
            "r": ApprovalAction.REVISE,
            "feedback": ApprovalAction.REVISE,
            "4": ApprovalAction.SAVE,
            "s": ApprovalAction.SAVE,
            "save": ApprovalAction.SAVE,
            "5": ApprovalAction.ABORT,
            "n": ApprovalAction.ABORT,
            "no": ApprovalAction.ABORT,
            "q": ApprovalAction.ABORT,
        }
        while True:
            try:
                choice = click.prompt("  Choice", type=str, default="4").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalChoice(action=ApprovalAction.ABORT)
            action = _MAP.get(choice)
            if action is not None:
                return ApprovalChoice(action=action)
            click.echo(_dim(f"  '{choice}' is not a valid choice. Enter 1-5, y/n, or e/r."))

    def show_message(self, text: str, style: str = "info") -> None:
        import click
        _styles = {
            "info": _dim, "success": _green, "warning": _yellow, "error": _red,
        }
        fn = _styles.get(style, _dim)
        click.echo(fn(text))

    def prompt_feedback(self) -> str:
        import click
        try:
            return click.prompt("  What would you like to change?", type=str).strip()
        except (EOFError, KeyboardInterrupt):
            return ""


class PredefinedChoiceAdapter(InteractionAdapter):
    """Returns a caller-specified choice. For API / programmatic use.

    Usage:
        adapter = PredefinedChoiceAdapter(action="revise", feedback="Add tests")
    """

    def __init__(
        self,
        action: ApprovalAction | str = ApprovalAction.SAVE,
        feedback: str = "",
    ) -> None:
        self._choice = ApprovalChoice(action=action, feedback=feedback)

    def show_plan(self, plan_text: str, plan_path: str) -> None:
        pass

    def prompt_approval(self) -> ApprovalChoice:
        return self._choice

    def show_message(self, text: str, style: str = "info") -> None:
        pass

    def prompt_feedback(self) -> str:
        return self._choice.feedback


def cli_plan_adapter(policy: PlanExecutionPolicy | str) -> ClickAdapter:
    """Build the CLI adapter from a typed plan execution policy."""
    typed = PlanExecutionPolicy(policy)
    selected = {
        PlanExecutionPolicy.REVIEW: None,
        PlanExecutionPolicy.SAVE: ApprovalAction.SAVE,
        PlanExecutionPolicy.EXECUTE: ApprovalAction.EXECUTE,
    }[typed]
    return ClickAdapter(preselected_action=selected)


from entry._terminal import bold as _bold, dim as _dim, green as _green, red as _red, yellow as _yellow

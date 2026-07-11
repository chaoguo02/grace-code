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
from typing import Any


@dataclass
class ApprovalChoice:
    """Result of a single approval interaction."""
    action: str  # "execute_auto" | "execute_manual" | "edit" | "revise" | "abort"
    feedback: str = ""  # Only for "revise" action


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

    def show_plan(self, plan_text: str, plan_path: str) -> None:
        import click
        click.echo("\n" + "─" * 60)
        click.echo(_bold("  Plan ready for review"))
        click.echo(f"  File: {plan_path}")
        click.echo("─" * 60)
        click.echo(f"  [1] Yes, and auto-accept edits")
        click.echo(f"  [2] Yes, and manually approve edits")
        click.echo(f"  [3] Edit plan file (opens editor)")
        click.echo(f"  [4] Tell Claude what to change (re-plan)")
        click.echo(f"  [5] Abort")
        click.echo("─" * 60)

    def prompt_approval(self) -> ApprovalChoice:
        import click
        _MAP = {
            "1": "execute_auto",  "y": "execute_auto",   "yes": "execute_auto",
            "2": "execute_manual",
            "3": "edit",          "e": "edit",
            "4": "revise",        "r": "revise",         "feedback": "revise",
            "5": "abort",         "n": "abort",          "no": "abort",  "q": "abort",
        }
        while True:
            try:
                choice = click.prompt("  Choice", type=str, default="1").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalChoice(action="abort")
            action = _MAP.get(choice)
            if action:
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


# ── Non-interactive adapters ────────────────────────────────────────────

class AutoApproveAdapter(InteractionAdapter):
    """Always returns execute_auto — no user interaction.

    Used when --auto-approve is passed. The plan is generated, displayed
    briefly, and execution proceeds immediately. Same behavior as pressing
    [1] in the interactive menu.
    """

    def show_plan(self, plan_text: str, plan_path: str) -> None:
        pass  # No interactive display needed

    def prompt_approval(self) -> ApprovalChoice:
        return ApprovalChoice(action="execute_auto")

    def show_message(self, text: str, style: str = "info") -> None:
        pass

    def prompt_feedback(self) -> str:
        return ""


class PredefinedChoiceAdapter(InteractionAdapter):
    """Returns a caller-specified choice. For API / programmatic use.

    Usage:
        adapter = PredefinedChoiceAdapter(action="revise", feedback="Add tests")
    """

    def __init__(self, action: str = "execute_auto", feedback: str = "") -> None:
        self._choice = ApprovalChoice(action=action, feedback=feedback)

    def show_plan(self, plan_text: str, plan_path: str) -> None:
        pass

    def prompt_approval(self) -> ApprovalChoice:
        return self._choice

    def show_message(self, text: str, style: str = "info") -> None:
        pass

    def prompt_feedback(self) -> str:
        return self._choice.feedback


# ── Colour helpers ─────────────────────────────────────────────────────

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def _green(t: str) -> str:   return _c(t, "32")
def _yellow(t: str) -> str:  return _c(t, "33")
def _red(t: str) -> str:     return _c(t, "31")
def _bold(t: str) -> str:    return _c(t, "1")
def _dim(t: str) -> str:     return _c(t, "2")

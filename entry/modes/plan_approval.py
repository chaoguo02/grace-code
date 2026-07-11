"""Plan Approval Service — pure state machine for plan review workflow.

Decoupled from any UI (CLI, web, API). The service receives events
(ApprovalChoice) and returns actions (PlanAction). The caller is
responsible for executing the action (triggering build, re-plan, etc.).

Claude Code pattern: the approval engine doesn't know or care whether
the user is at a terminal, a browser, or an API endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from entry.modes.interaction import ApprovalChoice


class PlanAction(str, Enum):
    """Action the caller should take after processing a choice."""
    TRIGGER_BUILD = "trigger_build"        # execute the plan
    TRIGGER_REPLAN = "trigger_replan"      # ask model to revise
    CONTINUE_EDIT = "continue_edit"        # user edited the plan, re-display
    ABORT_REVISIONS = "abort_revisions"    # max revisions reached
    ABORT_SESSION = "abort_session"        # user chose to abort
    NO_OUTPUT = "no_output"                # plan produced empty output


@dataclass
class PlanApprovalService:
    """Pure state machine for plan approval. Zero UI dependencies.

    Usage:
        service = PlanApprovalService(max_revisions=5)

        while True:
            choice = adapter.prompt_approval()       # UI layer
            action = service.process(choice)          # business logic
            if action == PlanAction.TRIGGER_BUILD:
                trigger_build()
                break
            elif action == PlanAction.TRIGGER_REPLAN:
                result = replan(feedback)
                # loop continues with new plan
            ...
    """

    max_revisions: int = 5
    revision_count: int = 0

    def evaluate(self, choice: ApprovalChoice) -> PlanAction:
        """Evaluate a choice WITHOUT committing state changes.

        Returns the action to take. For REVISE, checks if max would be hit
        but does NOT increment the counter yet. Call commit_revision() AFTER
        the replan actually executes.
        """
        action = choice.action

        if action in ("execute_auto", "execute_manual"):
            return PlanAction.TRIGGER_BUILD

        if action == "edit":
            return PlanAction.CONTINUE_EDIT

        if action == "revise":
            if self.revision_count >= self.max_revisions:
                return PlanAction.ABORT_REVISIONS
            return PlanAction.TRIGGER_REPLAN

        return PlanAction.ABORT_SESSION

    def commit_revision(self) -> None:
        """Commit a revision AFTER replan successfully executes.

        Called by the approval loop only when replan actually runs.
        Empty feedback or other skips do NOT consume revision quota.
        """
        self.revision_count += 1

    @property
    def revisions_remaining(self) -> int:
        return max(0, self.max_revisions - self.revision_count)

    def handle_empty_plan(self) -> PlanAction:
        return PlanAction.NO_OUTPUT

    def reset(self) -> None:
        self.revision_count = 0

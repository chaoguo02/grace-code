"""V2 mode runner — orchestrates a v2 session (plan, build, or v2-plan).

Extracted from entry/cli.py. Constitution: entry/ is the user entry point.
Mode execution logic belongs in entry/modes/, not in cli.py.

cli.py calls run_v2_mode() with assembled dependencies. This module handles
session creation, the plan approval loop, and recursive build execution.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

import click

from agent.task import RunResult, RunStatus, TaskIntent, TerminationReason

if TYPE_CHECKING:
    from agent.v2.models import ForkResult
    from agent.v2.task_contract import TaskContract
    from llm.base import LLMMessage


@dataclass(frozen=True)
class _ContinueAfterExplicitChild:
    child_result: "ForkResult"
    message: "LLMMessage"
    contract: "TaskContract"


@dataclass(frozen=True)
class _TerminalExplicitChild:
    child_result: "ForkResult"
    message: "LLMMessage"


from entry._terminal import bold, cyan, dim, green, magenta, red, yellow


# ── Event rendering ──────────────────────────────────────────────────────

def _render_v2_event(event, rend, last_tool=None, last_tool_params=None):
    from agent.task import EventType

    payload = event.payload
    if event.event_type == EventType.ACTION:
        step = payload.get("step", 0)
        action = payload.get("action", {})
        tool_calls = action.get("tool_calls") or []
        if tool_calls:
            for tool_call in tool_calls:
                if last_tool is not None:
                    last_tool[0] = tool_call.get("name", "")
                if last_tool_params is not None:
                    last_tool_params[0] = tool_call.get("params", {})
                rend.on_tool_call(step, tool_call.get("name", ""), tool_call.get("params", {}))
        elif action.get("action_type") == "finish":
            rend.on_finish(step, action.get("message", ""))
        elif action.get("action_type") == "give_up":
            rend.on_give_up(step, action.get("message", ""))
    elif event.event_type == EventType.OBSERVATION:
        step = payload.get("step", 0)
        obs = payload.get("observation", {})
        tool_name = obs.get("tool_name") or (last_tool[0] if last_tool else "")
        output = obs.get("output", "")
        status = obs.get("status", "")
        rend.on_observation(step, tool_name, status, output, obs.get("error"))
    elif event.event_type == EventType.REFLECTION:
        rend.on_reflection(payload.get("reason", ""))
    elif event.event_type == EventType.SUBAGENT_START:
        click.echo(magenta(
            f"\n  Subagent {payload.get('agent_name', '')} started "
            f"[{payload.get('session_id', '')}]"
        ))
    elif event.event_type == EventType.SUBAGENT_STOP:
        click.echo(magenta(
            f"\n  Subagent {payload.get('agent_name', '')} finished: "
            f"{payload.get('status', '')} "
            f"({payload.get('turns_used', 0)} turns, "
            f"{payload.get('tokens_used', 0)} tokens)"
        ))


# ── Result printing ──────────────────────────────────────────────────────

def _print_v2_result(agent_name: str, db_path: str, session_id: str, result, *, show_summary: bool = True) -> None:
    from agent.task import RunStatus
    click.echo(dim(f"  Agent   : {agent_name}"))
    click.echo(dim(f"  V2 DB   : {db_path}"))
    click.echo(dim(f"  Session : {session_id}\n"))
    if show_summary and result.summary:
        click.echo(result.summary)
    if result.status == RunStatus.SUCCESS:
        click.echo(green("\n  V2 run completed successfully."))
    else:
        click.echo(yellow(f"\n  V2 run finished with status: {result.status.value}"))


def _read_manual_plan_edit(plan_path: str, interaction) -> str:
    """Wait for an explicit user edit without resolving a host editor from PATH."""
    interaction.show_message(
        f"Edit the plan manually at: {plan_path}", style="info"
    )
    click.pause("Press any key after saving the plan file...")
    return Path(plan_path).read_text(encoding="utf-8")


def _plan_filename(description: str) -> str:
    """Return a stable, cross-platform name without leaking raw task text."""
    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:12]
    return f"plan-{digest}.md"


def _workflow_failure(result: RunResult, detail: str) -> RunResult:
    """Convert a post-model workflow rejection into a truthful run result."""
    return replace(
        result,
        status=RunStatus.FAILED,
        summary=detail,
        error=detail,
        termination_reason=TerminationReason.GUARD_REJECTED,
    )


def _run_explicit_child(
    runtime,
    session,
    *,
    agent_name: str,
    description: str,
    intent: TaskIntent,
    contract,
) -> _ContinueAfterExplicitChild | _TerminalExplicitChild:
    """Dispatch a required child and return its typed result plus remaining budget."""
    from agent.v2 import ExplicitDelegationRequest
    from agent.v2.task_contract import TaskContract
    from llm.base import LLMMessage

    child_result = runtime.run_explicit_delegation(
        session.id,
        request=ExplicitDelegationRequest(
            agent_name=agent_name,
            description=f"Explicit {agent_name} delegation",
            prompt=description,
        ),
        parent_intent=intent,
        contract=contract,
    )
    remaining_tokens = contract.budget_tokens - child_result.tokens_used
    message = LLMMessage(
        role="user",
        content=(
            "[RUNTIME EXPLICIT DELEGATION RESULT]\n"
            "The requested subagent has already run. Treat this typed payload "
            "as its authoritative result and continue the parent task.\n"
            + json.dumps(child_result.to_dict(), ensure_ascii=False)
        ),
    )
    from agent.v2.models import ForkStatus
    if (
        remaining_tokens <= 0
        or child_result.status in {ForkStatus.FAILED, ForkStatus.CANCELLED}
    ):
        return _TerminalExplicitChild(child_result, message)
    return _ContinueAfterExplicitChild(
        child_result=child_result,
        message=message,
        contract=TaskContract(
            max_steps=contract.max_steps,
            budget_tokens=remaining_tokens,
            require_deliverables=dict(contract.require_deliverables),
        ),
    )


def _child_only_run_result(child_result) -> RunResult:
    return RunResult(
        task_id=child_result.session_id,
        status=child_result.status.run_status,
        summary=child_result.summary,
        steps_taken=child_result.turns_used,
        total_tokens=child_result.tokens_used,
        error=child_result.error or None,
        termination_reason=child_result.status.termination_reason,
    )


# ── V2 mode runner ───────────────────────────────────────────────────────

def run_v2_mode(
    *,
    agent_name: str,
    description: str,
    repo_path: Path,
    backend,
    registry,
    agent_config,
    memory_context,
    log_dir: str,
    intent_override: str | None,
    approval_interaction=None,
    plan_file: str | None = None,
    hook_dispatcher=None,
    mcp_integration=None,
    renderer=None,
    explicit_agent: str | None = None,
) -> RunResult:
    """Run a v2 session orchestrated by an AgentDefinition.

    The caller selects the agent by name (e.g. "build", "plan").
    Intent, tools, permissions, and contracts are all derived from the
    AgentDefinition — no string-based mode dispatching.
    """
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore, default_session_db_path
    from agent.v2.models import _BUILTIN_AGENTS
    from llm.base import LLMMessage

    definition = _BUILTIN_AGENTS.get(agent_name)
    if definition is None:
        raise ValueError(f"Unknown agent: {agent_name!r}")
    intent = TaskIntent(intent_override) if intent_override else definition.intent

    db_path = default_session_db_path(str(repo_path))
    from executor.state_paths import migrate_legacy_session_db
    migrate_legacy_session_db(repo_path, db_path)
    store = SessionStore(db_path)
    rend = renderer
    last_tool = [""]
    last_tool_params = [{}]
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=registry,
        agent_registry=AgentRegistryV2(project_dir=repo_path),
        root_agent_config=agent_config,
        log_dir=log_dir,
        memory_context=memory_context,
        hook_dispatcher=hook_dispatcher,
        mcp_integration=mcp_integration,
        event_callback=(
            (lambda event: _render_v2_event(
                event, rend,
                last_tool=last_tool, last_tool_params=last_tool_params,
            )) if rend is not None else None
        ),
    )

    if intent is TaskIntent.EDIT:
        # ── Context continuity: inject plan file content if provided ──
        build_messages: list[LLMMessage] = []
        if plan_file and os.path.isfile(plan_file):
            with open(plan_file, encoding="utf-8") as f:
                plan_content = f.read()
            click.echo(dim(f"  Plan file: {plan_file}"))

            from entry.modes.plan_contract import extract_and_parse_json, PlanContract
            _contract_data = extract_and_parse_json(plan_content)
            _contract_msg = ""
            if _contract_data:
                try:
                    _contract = PlanContract.model_validate(_contract_data)
                    _contract_msg = _contract.render_for_build_agent()
                except Exception:
                    pass

            build_messages.append(LLMMessage(
                role="user",
                content=(
                    f"[PLAN CONTEXT] The following implementation plan has been reviewed and approved. "
                    f"Execute it now.\n\n{plan_content}"
                ),
            ))
            if _contract_msg:
                build_messages.append(LLMMessage(role="user", content=_contract_msg))
        build_messages.append(LLMMessage(role="user", content=description))

        session = runtime.create_root_session(
            agent_name=agent_name,
            repo_path=str(repo_path),
            title=description[:80] or agent_name,
            metadata={"entrypoint": "cli_run_v2", "agent": agent_name},
        )
        from agent.v2.task_contract import TaskContract
        build_contract = TaskContract.for_build(agent_config)
        explicit_tokens_used = 0
        if explicit_agent is not None:
            explicit_outcome = _run_explicit_child(
                runtime,
                session,
                agent_name=explicit_agent,
                description=description,
                intent=intent,
                contract=build_contract,
            )
            explicit_result = explicit_outcome.child_result
            explicit_tokens_used = explicit_result.tokens_used
            build_messages.append(explicit_outcome.message)
            if isinstance(explicit_outcome, _TerminalExplicitChild):
                runtime.finalize_parent_from_explicit_child(
                    session.id, explicit_result,
                )
                result = _child_only_run_result(explicit_result)
                _print_v2_result(agent_name, db_path, session.id, result)
                return result
            build_contract = explicit_outcome.contract
        result = runtime.run_session(
            session.id,
            agent_name=agent_name,
            task_description=description,
            intent=intent,
            messages=build_messages,
            contract=build_contract,
        )
        if explicit_tokens_used:
            result = replace(
                result,
                total_tokens=result.total_tokens + explicit_tokens_used,
            )
        _print_v2_result(
            agent_name,
            db_path,
            session.id,
            result,
            show_summary=rend is None,
        )
        return result

    # --- analysis: read-only plan→approve→execute loop ---
    if intent is TaskIntent.ANALYSIS:
        session = runtime.create_root_session(
            agent_name=agent_name,
            repo_path=str(repo_path),
            title=description[:80] or agent_name,
            metadata={"entrypoint": "cli_run_v2", "agent": agent_name},
        )
        from agent.v2.task_contract import TaskContract
        plan_contract = TaskContract.for_plan(agent_config)

        plan_messages = [LLMMessage(role="user", content=description)]
        explicit_tokens_used = 0
        if explicit_agent is not None:
            explicit_outcome = _run_explicit_child(
                runtime,
                session,
                agent_name=explicit_agent,
                description=description,
                intent=TaskIntent.ANALYSIS,
                contract=plan_contract,
            )
            explicit_result = explicit_outcome.child_result
            explicit_tokens_used = explicit_result.tokens_used
            plan_messages.append(explicit_outcome.message)
            if isinstance(explicit_outcome, _TerminalExplicitChild):
                runtime.finalize_parent_from_explicit_child(
                    session.id, explicit_result,
                )
                result = _child_only_run_result(explicit_result)
                _print_v2_result(agent_name, db_path, session.id, result)
                return result
            plan_contract = explicit_outcome.contract

        # Fixed plan file path (single file, overwrite in-place)
        from executor.state_paths import ProjectStatePaths
        plans_dir = str(ProjectStatePaths.for_project(repo_path).plans)
        os.makedirs(plans_dir, exist_ok=True)
        plan_path = os.path.join(plans_dir, _plan_filename(description))

        # First plan session
        result = runtime.run_session(
            session.id,
            agent_name="plan",
            task_description=description,
            intent=TaskIntent.ANALYSIS,
            messages=plan_messages,
            contract=plan_contract,
        )
        if explicit_tokens_used:
            result = replace(
                result,
                total_tokens=result.total_tokens + explicit_tokens_used,
            )

        # ── Plan approval: service (state machine) + adapter (UI) ──
        from entry.modes.interaction import ClickAdapter
        from entry.modes.plan_approval import PlanAction, PlanApprovalService
        interaction = approval_interaction or ClickAdapter()
        service = PlanApprovalService(max_revisions=5)
        plan_override: str | None = None

        while True:
            plan_text = plan_override if plan_override is not None else (result.summary or "")
            plan_override = None

            _print_v2_result(agent_name, db_path, session.id, result, show_summary=False)

            if not result.is_success():
                interaction.show_message(
                    f"Plan session failed (status={result.status.value}). "
                    "Cannot proceed to approval.", style="error",
                )
                return result

            if not plan_text.strip():
                detail = "Plan session produced no output. Nothing to review."
                interaction.show_message(
                    detail, style="warning",
                )
                return _workflow_failure(result, detail)

            # ── Always save and display the Markdown plan first ──
            # CC-aligned: the plan file IS the contract. JSON extraction is
            # best-effort structured metadata, not a blocking gate.
            Path(plan_path).write_text(plan_text, encoding="utf-8")
            interaction.show_message(f"Plan saved: {plan_path}", style="info")

            # ── Best-effort JSON contract extraction (non-blocking) ──
            from entry.modes.plan_contract import (
                PlanContract, PlanValidator, extract_and_parse_json,
            )
            _contract: PlanContract | None = None
            _data = extract_and_parse_json(plan_text)
            if _data is not None:
                try:
                    _contract = PlanContract.model_validate(_data)
                    _valid, _err = PlanValidator.validate(_contract)
                    if not _valid:
                        interaction.show_message(
                            f"Plan contract noted but has validation gaps: {_err}",
                            style="warning",
                        )
                except Exception:
                    interaction.show_message(
                        "Plan has JSON block but failed contract validation; "
                        "proceeding with Markdown plan only.",
                        style="warning",
                    )

            if _contract is not None and intent_override is not None:
                _contract = _contract.model_copy(update={
                    "execution_intent": TaskIntent(intent_override),
                })
            if _contract is not None:
                plan_text = _contract.render_for_approval()

            # ── UI → event → service → action → execute ──
            interaction.show_plan(plan_text, plan_path)
            choice = interaction.prompt_approval()
            action = service.evaluate(choice)

            if action is PlanAction.TRIGGER_BUILD:
                interaction.show_message(
                    "Plan approved. Executing...",
                    style="success",
                )
                return run_v2_mode(
                    agent_name="build", description=description, repo_path=repo_path,
                    backend=backend, registry=registry, agent_config=agent_config,
                    memory_context=memory_context, log_dir=log_dir,
                    intent_override="edit",
                    plan_file=plan_path,
                    hook_dispatcher=hook_dispatcher,
                    renderer=renderer,
                )

            elif action is PlanAction.COMPLETE_PLAN:
                interaction.show_message(
                    f"Plan saved without execution: {plan_path}", style="success",
                )
                return result

            elif action is PlanAction.CONTINUE_EDIT:
                updated = _read_manual_plan_edit(plan_path, interaction)
                _current_text = Path(plan_path).read_text(encoding="utf-8")
                if updated != _current_text:
                    plan_override = updated
                    interaction.show_message("Plan updated.", style="success")
                else:
                    interaction.show_message("No changes detected.", style="info")
                continue

            elif action is PlanAction.TRIGGER_REPLAN:
                feedback = interaction.prompt_feedback()
                if not feedback.strip():
                    continue
                interaction.show_message(
                    f"Re-planning ({service.revisions_remaining} revisions remaining)...",
                    style="info",
                )
                result = runtime.run_session(
                    session.id,
                    agent_name="plan", task_description=description, intent=TaskIntent.ANALYSIS,
                    messages=[LLMMessage(
                        role="user",
                        content=(
                            f"[USER FEEDBACK ON PLAN]\n{feedback}\n\n"
                            f"Please revise the plan accordingly and output "
                            f"an updated structured plan."
                        ),
                    )],
                    contract=plan_contract,
                )
                service.commit_revision()  # only after replan actually runs
                continue

            elif action is PlanAction.ABORT_REVISIONS:
                detail = f"Max revisions ({service.max_revisions}) reached. Aborting."
                interaction.show_message(
                    detail, style="warning",
                )
                return _workflow_failure(result, detail)

            else:  # ABORT_SESSION
                interaction.show_message(
                    f"Aborted. Plan saved at: {plan_path}", style="info",
                )
                return result
        return result

    raise ValueError(f"Unsupported agent intent for {agent_name!r}: {intent.value}")

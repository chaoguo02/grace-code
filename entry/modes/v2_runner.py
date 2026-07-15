"""V2 mode runner — orchestrates a v2 session (plan, build, or v2-plan).

Extracted from entry/cli.py. Constitution: entry/ is the user entry point.
Mode execution logic belongs in entry/modes/, not in cli.py.

cli.py calls run_v2_mode() with assembled dependencies. This module handles
session creation, the plan approval loop, and recursive build execution.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from agent.task import RunResult, RunStatus, TaskIntent, TerminationReason


# ── Color helpers (from cli.py) ──────────────────────────────────────────

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


# ── Event rendering ──────────────────────────────────────────────────────

def _render_v2_event(event, rend, proactive_memory=None, last_tool=None, last_tool_params=None):
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
        if proactive_memory is not None:
            proactive_memory.check_tool_result(
                tool_name=tool_name,
                params=(last_tool_params[0] if last_tool_params else {}),
                output=output,
                success=(status == "success"),
            )
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

def _print_v2_result(mode: str, db_path: str, session_id: str, result, *, show_summary: bool = True) -> None:
    from agent.task import RunStatus
    click.echo(dim(f"  Mode    : {mode}"))
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


# ── V2 mode runner ───────────────────────────────────────────────────────

def run_v2_mode(
    *,
    mode: str,
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
    proactive_memory=None,
    mcp_integration=None,
    renderer=None,
) -> RunResult:
    """Run a v2 session (plan, build, or v2-plan with approval loop).

    All dependencies are passed in — this module does NOT import agent/
    or memory/ internals. It only orchestrates what it receives.
    """
    from agent.factory import resolve_task_intent
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore, default_session_db_path
    from llm.base import LLMMessage

    db_path = default_session_db_path(str(repo_path))
    from runtime.state_paths import migrate_legacy_session_db
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
                event, rend, proactive_memory=proactive_memory,
                last_tool=last_tool, last_tool_params=last_tool_params,
            )) if rend is not None else None
        ),
    )
    intent = resolve_task_intent(mode, intent_override)

    if mode == "v2-build":
        # ── Context continuity: inject plan file content if provided ──
        build_messages: list[LLMMessage] = []
        if plan_file and os.path.isfile(plan_file):
            with open(plan_file, encoding="utf-8") as f:
                plan_content = f.read()
            click.echo(dim(f"  Plan file: {plan_file}"))

            # Inject the structured contract as a hard constraint for Build agent
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
            agent_name="build",
            repo_path=str(repo_path),
            title=description[:80] or "v2-build",
            metadata={"entrypoint": "cli_run_v2", "mode": mode},
        )
        result = runtime.run_session(
            session.id,
            agent_name="build",
            task_description=description,
            intent=intent,
            messages=build_messages,
        )
        _print_v2_result(
            mode,
            db_path,
            session.id,
            result,
            show_summary=rend is None,
        )
        return result

    # --- plan / v2-plan: read-only tools, plan→approve→execute loop ---
    if mode == "v2-plan":
        session = runtime.create_root_session(
            agent_name="plan",
            repo_path=str(repo_path),
            title=description[:80] or "plan",
            metadata={"entrypoint": "cli_run_v2", "mode": mode},
        )
        from agent.v2.task_contract import TaskContract
        plan_contract = TaskContract.for_plan(agent_config)

        # Fixed plan file path (single file, overwrite in-place)
        from runtime.state_paths import ProjectStatePaths
        plans_dir = str(ProjectStatePaths.for_project(repo_path).plans)
        os.makedirs(plans_dir, exist_ok=True)
        plan_path = os.path.join(plans_dir, _plan_filename(description))

        # First plan session
        result = runtime.run_session(
            session.id,
            agent_name="plan",
            task_description=description,
            intent=TaskIntent.ANALYSIS,
            messages=[LLMMessage(role="user", content=description)],
            contract=plan_contract,
        )

        # ── Plan approval: service (state machine) + adapter (UI) ──
        from entry.modes.interaction import ClickAdapter
        from entry.modes.plan_approval import PlanAction, PlanApprovalService
        interaction = approval_interaction or ClickAdapter()
        service = PlanApprovalService(max_revisions=5)
        contract_repair_attempts = 0
        max_contract_repairs = 2
        plan_override: str | None = None

        while True:
            plan_text = plan_override if plan_override is not None else (result.summary or "")
            plan_override = None

            _print_v2_result(mode, db_path, session.id, result, show_summary=False)

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

            # ── Plan Contract: extract JSON → validate → reject or approve ──
            from entry.modes.plan_contract import (
                PlanContract, PlanValidator, extract_and_parse_json,
            )
            _data = extract_and_parse_json(plan_text)
            if _data is None:
                if contract_repair_attempts >= max_contract_repairs:
                    detail = "Plan contract is still invalid after 2 repair attempts; aborting."
                    interaction.show_message(
                        detail, style="error",
                    )
                    return _workflow_failure(result, detail)
                contract_repair_attempts += 1
                interaction.show_message(
                    "Plan has no valid JSON contract. Asking agent to add one...",
                    style="warning",
                )
                result = runtime.run_session(
                    session.id,
                    agent_name="plan", task_description=description, intent=TaskIntent.ANALYSIS,
                    messages=[LLMMessage(
                        role="user",
                        content=(
                            '[SYSTEM] Your output must include a JSON contract block:\n'
                            '```json\n'
                            '{"objective": "...", "execution_intent": "analysis", '
                            '"target_files": ["..."], '
                            '"expected_behavior": "...", "verification_strategy": "...", '
                            '"potential_conflicts": ["..."]}\n'
                            '```\n'
                            'All six fields are required. Re-read the files, '
                            'then produce a revised plan with the JSON block.'
                        ),
                    )],
                    contract=plan_contract,
                )
                continue

            try:
                _contract = PlanContract.model_validate(_data)
            except Exception as exc:
                if contract_repair_attempts >= max_contract_repairs:
                    detail = "Plan contract is still invalid after 2 repair attempts; aborting."
                    interaction.show_message(
                        detail, style="error",
                    )
                    return _workflow_failure(result, detail)
                contract_repair_attempts += 1
                interaction.show_message(
                    f"Plan contract validation failed: {exc}", style="warning",
                )
                result = runtime.run_session(
                    session.id,
                    agent_name="plan", task_description=description, intent=TaskIntent.ANALYSIS,
                    messages=[LLMMessage(
                        role="user",
                        content=(
                            f"[SYSTEM] Plan contract rejected: {exc}\n\n"
                            f'Required fields: objective, execution_intent, target_files, expected_behavior, '
                            f'verification_strategy, potential_conflicts (can be empty array). '
                            f'Please fix the JSON and try again.'
                        ),
                    )],
                    contract=plan_contract,
                )
                continue

            _valid, _err = PlanValidator.validate(_contract)
            if not _valid:
                if contract_repair_attempts >= max_contract_repairs:
                    detail = "Plan contract is still invalid after 2 repair attempts; aborting."
                    interaction.show_message(
                        detail, style="error",
                    )
                    return _workflow_failure(result, detail)
                contract_repair_attempts += 1
                interaction.show_message(
                    f"Plan contract rejected: {_err}", style="warning",
                )
                result = runtime.run_session(
                    session.id,
                    agent_name="plan", task_description=description, intent=TaskIntent.ANALYSIS,
                    messages=[LLMMessage(
                        role="user",
                        content=(
                            f"[SYSTEM] Plan contract rejected: {_err}\n\n"
                            f"Fix this issue and re-submit the JSON contract."
                        ),
                    )],
                    contract=plan_contract,
                )
                continue

            # An explicit CLI intent is an entry-boundary fact and takes
            # precedence over the model's proposed execution classification.
            if intent_override is not None:
                _contract = _contract.model_copy(update={
                    "execution_intent": TaskIntent(intent_override),
                })

            # Persist only a validated, canonical plan. Failed LLM responses
            # must never overwrite a usable plan from an earlier attempt.
            canonical_document = _contract.render_plan_document()
            Path(plan_path).write_text(canonical_document, encoding="utf-8")
            interaction.show_message(f"Plan saved: {plan_path}", style="info")

            # Replace plan_text with human-readable rendering for display.
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
                    mode="v2-build", description=description, repo_path=repo_path,
                    backend=backend, registry=registry, agent_config=agent_config,
                    memory_context=memory_context, log_dir=log_dir,
                    intent_override=_contract.execution_intent.value,
                    plan_file=plan_path,
                    hook_dispatcher=hook_dispatcher,
                    proactive_memory=proactive_memory, renderer=renderer,
                )

            elif action is PlanAction.COMPLETE_PLAN:
                interaction.show_message(
                    f"Plan saved without execution: {plan_path}", style="success",
                )
                return result

            elif action is PlanAction.CONTINUE_EDIT:
                updated = _read_manual_plan_edit(plan_path, interaction)
                if updated != canonical_document:
                    plan_override = updated
                    interaction.show_message("Plan updated.", style="success")
                else:
                    interaction.show_message("No changes detected.", style="info")
                continue

            elif action is PlanAction.TRIGGER_REPLAN:
                feedback = interaction.prompt_feedback()
                if not feedback.strip():
                    continue
                if proactive_memory:
                    proactive_memory.check_plan_feedback(feedback)
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

    raise ValueError(f"Unsupported v2 mode: {mode!r}")

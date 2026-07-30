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
    from agent.session.models import ForkResult
    from agent.session.task_contract import TaskContract
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


@dataclass(frozen=True)
class _PreparedSessionRun:
    session: Any
    contract: "TaskContract"
    messages: list["LLMMessage"]
    explicit_tokens_used: int = 0


@dataclass(frozen=True)
class _PlanArtifact:
    path: str
    file_text: str
    review_text: str
    contract: Any | None = None


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
    """Return a stable, cross-platform plan filename.

    Keep the canonical ``plan-`` prefix for existing tooling while adding a
    short ASCII slug when available so saved plans are easier for humans to
    identify on disk.
    """
    import re

    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:12]
    slug_parts = re.findall(r"[a-z0-9]+", description.lower())
    slug = "-".join(slug_parts[:6]).strip("-")[:48]
    if slug:
        return f"plan-{slug}-{digest}.md"
    return f"plan-{digest}.md"


def _resolve_plan_path(repo_path: Path, description: str) -> str:
    """Resolve the canonical plan artifact path for one objective."""
    from core.state_paths import ProjectStatePaths

    plans_dir = ProjectStatePaths.for_project(repo_path).plans
    plans_dir.mkdir(parents=True, exist_ok=True)
    return str(plans_dir / _plan_filename(description))


def _build_plan_artifact(
    *,
    plan_path: str,
    raw_plan_text: str,
    intent_override: str | None,
    interaction,
) -> _PlanArtifact:
    """Materialize the current plan as a persisted artifact plus review text."""
    from entry.modes.plan_contract import (
        PlanContract, PlanValidator, extract_and_parse_json,
    )

    contract = None
    review_text = raw_plan_text
    file_text = raw_plan_text
    data = extract_and_parse_json(raw_plan_text)
    if data is not None:
        try:
            contract = PlanContract.model_validate(data)
            valid, err = PlanValidator.validate(contract)
            if not valid:
                interaction.show_message(
                    f"Plan contract noted but has validation gaps: {err}",
                    style="warning",
                )
            if intent_override is not None:
                contract = contract.model_copy(update={
                    "execution_intent": TaskIntent(intent_override),
                })
            review_text = contract.render_for_approval()
            file_text = contract.render_plan_document()
        except Exception:
            interaction.show_message(
                "Plan has JSON block but failed contract validation; "
                "proceeding with Markdown plan only.",
                style="warning",
            )
    return _PlanArtifact(
        path=plan_path,
        file_text=file_text,
        review_text=review_text,
        contract=contract,
    )


def _write_plan_artifact(artifact: _PlanArtifact, interaction) -> None:
    """Persist the canonical plan artifact to disk and announce its path."""
    Path(artifact.path).write_text(artifact.file_text, encoding="utf-8")
    interaction.show_message(f"Plan saved: {artifact.path}", style="info")


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
    from agent.session import ExplicitDelegationRequest
    from agent.session.task_contract import TaskContract
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
    from agent.session.models import ForkStatus
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


def _create_root_session(runtime, *, agent_name: str, repo_path: Path, description: str):
    """Create the canonical root session record for a CLI-triggered run."""
    return runtime.create_root_session(
        agent_name=agent_name,
        repo_path=str(repo_path),
        title=description[:80] or agent_name,
        metadata={"entrypoint": "cli_run_v2", "agent": agent_name},
    )


def _prepare_session_run(
    runtime,
    *,
    session,
    agent_name: str,
    description: str,
    intent: TaskIntent,
    contract: "TaskContract",
    messages: list["LLMMessage"],
    explicit_agent: str | None,
    db_path: str,
) -> _PreparedSessionRun | RunResult:
    """Apply optional explicit delegation for build/plan runs.

    This preserves the shared budget-continuation semantics without duplicating
    the same orchestration block in both intent branches.
    """
    prepared_messages = list(messages)
    prepared_contract = contract
    explicit_tokens_used = 0
    if explicit_agent is None:
        return _PreparedSessionRun(
            session=session,
            contract=prepared_contract,
            messages=prepared_messages,
            explicit_tokens_used=explicit_tokens_used,
        )

    explicit_outcome = _run_explicit_child(
        runtime,
        session,
        agent_name=explicit_agent,
        description=description,
        intent=intent,
        contract=prepared_contract,
    )
    explicit_result = explicit_outcome.child_result
    explicit_tokens_used = explicit_result.tokens_used
    prepared_messages.append(explicit_outcome.message)
    if isinstance(explicit_outcome, _TerminalExplicitChild):
        runtime.finalize_parent_from_explicit_child(
            session.id, explicit_result,
        )
        result = _child_only_run_result(explicit_result)
        _print_v2_result(agent_name, db_path, session.id, result)
        return result
    prepared_contract = explicit_outcome.contract
    return _PreparedSessionRun(
        session=session,
        contract=prepared_contract,
        messages=prepared_messages,
        explicit_tokens_used=explicit_tokens_used,
    )


# ── Plan approval loop (extracted from run_v2_mode) ────────────────────

def _plan_approval_loop(
    *,
    result,
    plan_path: str,
    plan_override: str | None,
    plan_contract,
    runtime,
    session,
    description: str,
    agent_name: str,
    db_path: str,
    agent_config,
    backend,
    registry,
    hook_dispatcher,
    mcp_integration,
    renderer,
    memory_context,
    log_dir: str,
    repo_path,
    approval_interaction=None,
    intent_override: str | None = None,
) -> "RunResult":
    """Plan review → approve → execute/re-plan/save loop.

    Extracted from run_v2_mode() to separate UI/adapter concerns from
    the session orchestration.  Behavior is unchanged.
    """
    from entry.modes.interaction import ClickAdapter
    from entry.modes.plan_approval import PlanAction, PlanApprovalService
    from agent.task import TaskIntent
    from llm.base import LLMMessage

    interaction = approval_interaction or ClickAdapter()
    service = PlanApprovalService(max_revisions=5)

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
            interaction.show_message(detail, style="warning")
            return _workflow_failure(result, detail)

        artifact = _build_plan_artifact(
            plan_path=plan_path,
            raw_plan_text=plan_text,
            intent_override=intent_override,
            interaction=interaction,
        )
        _write_plan_artifact(artifact, interaction)

        # UI → event → service → action → execute
        interaction.show_plan(artifact.review_text, artifact.path)
        choice = interaction.prompt_approval()
        action = service.evaluate(choice)

        if action is PlanAction.TRIGGER_BUILD:
            interaction.show_message("Plan approved. Executing...", style="success")
            # CC-aligned: continue on same session, inject plan as context
            return run_v2_mode(
                agent_name="build", description=description, repo_path=repo_path,
                backend=backend, registry=registry, agent_config=agent_config,
                memory_context=memory_context, log_dir=log_dir,
                intent_override="edit", plan_file=plan_path,
                hook_dispatcher=hook_dispatcher,
                renderer=renderer,
                reuse_session_id=session.id,
            )
        elif action is PlanAction.COMPLETE_PLAN:
            interaction.show_message(
                f"Plan saved without execution: {artifact.path}",
                style="success",
            )
            return result
        elif action is PlanAction.CONTINUE_EDIT:
            prior_text = Path(artifact.path).read_text(encoding="utf-8")
            updated = _read_manual_plan_edit(artifact.path, interaction)
            if updated != prior_text:
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
                agent_name="plan",
                task_description=description,
                intent=TaskIntent.ANALYSIS,
                messages=[LLMMessage(
                    role="user",
                    content=f"[USER FEEDBACK ON PLAN]\n{feedback}\n\nPlease revise the plan accordingly.",
                )],
                contract=plan_contract,
            )
            service.commit_revision()
            continue
        elif action is PlanAction.ABORT_REVISIONS:
            detail = f"Max revisions ({service.max_revisions}) reached. Aborting."
            interaction.show_message(detail, style="warning")
            return _workflow_failure(result, detail)
        else:
            interaction.show_message(f"Aborted. Plan saved at: {plan_path}", style="info")
            return result


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
    reuse_session_id: str = "",
) -> RunResult:
    """Run a v2 session orchestrated by an AgentDefinition.

    The caller selects the agent by name (e.g. "build", "plan").
    Intent, tools, permissions, and contracts are all derived from the
    AgentDefinition — no string-based mode dispatching.
    """
    from agent.session import AgentRegistryV2, SessionRuntime, SessionStore, default_session_db_path
    from agent.session.models import _BUILTIN_AGENTS
    from llm.base import LLMMessage

    definition = _BUILTIN_AGENTS.get(agent_name)
    if definition is None:
        raise ValueError(f"Unknown agent: {agent_name!r}")
    intent = TaskIntent(intent_override) if intent_override else definition.intent

    db_path = default_session_db_path(str(repo_path))
    from core.state_paths import migrate_legacy_session_db
    migrate_legacy_session_db(repo_path, db_path)
    store = SessionStore(db_path)
    rend = renderer
    last_tool = [""]
    last_tool_params = [{}]
    # Phase 4: ResourceGovernor for v2 runner (observe by default)
    from core.resource_governor import ResourceGovernor
    from config.schema import ResourceGovernanceConfig
    gov = ResourceGovernor(ResourceGovernanceConfig())

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
        governor=gov,
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

        if reuse_session_id:
            session = runtime._store.get_session(reuse_session_id)
            if session is None:
                raise ValueError(f"Unknown session to reuse: {reuse_session_id}")
            # CC-aligned: continue from plan session, preserving conversation history
            persisted = runtime._store.list_messages(reuse_session_id)
            build_messages = persisted + build_messages
        else:
            session = _create_root_session(
                runtime, agent_name=agent_name, repo_path=repo_path, description=description,
            )
        from agent.session.task_contract import TaskContract
        build_contract = TaskContract.for_build(agent_config)
        prepared = _prepare_session_run(
            runtime,
            session=session,
            agent_name=agent_name,
            description=description,
            intent=intent,
            contract=build_contract,
            messages=build_messages,
            explicit_agent=explicit_agent,
            db_path=db_path,
        )
        if isinstance(prepared, RunResult):
            return prepared
        result = runtime.run_session(
            prepared.session.id,
            agent_name=agent_name,
            task_description=description,
            intent=intent,
            messages=prepared.messages,
            contract=prepared.contract,
        )
        if prepared.explicit_tokens_used:
            result = replace(
                result,
                total_tokens=result.total_tokens + prepared.explicit_tokens_used,
            )
        _print_v2_result(
            agent_name,
            db_path,
            prepared.session.id,
            result,
            show_summary=rend is None,
        )
        return result

    # --- analysis: read-only plan→approve→execute loop ---
    if intent is TaskIntent.ANALYSIS:
        session = _create_root_session(
            runtime, agent_name=agent_name, repo_path=repo_path, description=description,
        )
        from agent.session.task_contract import TaskContract
        plan_contract = TaskContract.for_plan(agent_config)

        plan_messages = [LLMMessage(role="user", content=description)]
        prepared = _prepare_session_run(
            runtime,
            session=session,
            agent_name=agent_name,
            description=description,
            intent=TaskIntent.ANALYSIS,
            contract=plan_contract,
            messages=plan_messages,
            explicit_agent=explicit_agent,
            db_path=db_path,
        )
        if isinstance(prepared, RunResult):
            return prepared
        plan_contract = prepared.contract

        # Fixed plan file path (single file, overwrite in-place)
        plan_path = _resolve_plan_path(repo_path, description)

        # First plan session
        result = runtime.run_session(
            prepared.session.id,
            agent_name="plan",
            task_description=description,
            intent=TaskIntent.ANALYSIS,
            messages=prepared.messages,
            contract=plan_contract,
        )
        if prepared.explicit_tokens_used:
            result = replace(
                result,
                total_tokens=result.total_tokens + prepared.explicit_tokens_used,
            )

        # Plan approval loop (extracted to _plan_approval_loop for CC-aligned separation)
        plan_override: str | None = None
        return _plan_approval_loop(
            result=result,
            plan_path=plan_path,
            plan_override=plan_override,
            plan_contract=plan_contract,
            repo_path=repo_path,
            runtime=runtime,
            session=prepared.session,
            description=description,
            agent_name=agent_name,
            db_path=db_path,
            agent_config=agent_config,
            backend=backend,
            registry=registry,
            hook_dispatcher=hook_dispatcher,
            mcp_integration=mcp_integration,
            renderer=renderer,
            memory_context=memory_context,
            log_dir=log_dir,
            approval_interaction=approval_interaction,
            intent_override=intent_override,
        )


    raise ValueError(f"Unsupported agent intent for {agent_name!r}: {intent.value}")

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from agent.task import RunResult, Task, TerminationReason
from llm.base import LLMMessage, LLMResponse, LLMToolSchema
from core.base import ToolResult

from observability.masking import truncate_text


REPLAY_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ReplayToolVisibility:
    name: str
    visible: bool
    source: str = ""
    reason: str = ""
    schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayRuntimeDecision:
    action: str
    reason: str = ""
    strip_tools: bool = False
    inject_message: str = ""
    terminate_reason: str = "none"
    terminate_status: str = ""
    terminate_detail: str = ""


@dataclass(frozen=True)
class ReplayToolExecution:
    tool_name: str
    tool_call_id: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    output_summary: str = ""
    error: str = ""
    duration_ms: float = 0.0
    outcome: str = "none"


@dataclass(frozen=True)
class ReplayStepRecord:
    step: int
    runtime_decision: ReplayRuntimeDecision
    visible_tools: tuple[ReplayToolVisibility, ...] = ()
    model_action: dict[str, Any] = field(default_factory=dict)
    tool_executions: tuple[ReplayToolExecution, ...] = ()
    outcome: str = "continue"
    termination_reason: str = "none"
    termination_status: str = ""


@dataclass(frozen=True)
class ReplayRunRecord:
    version: int
    run_id: str
    task_id: str
    session_id: str = ""
    generation: int = 0
    task: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    permission_snapshot: dict[str, Any] = field(default_factory=dict)
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)
    visible_tools: tuple[ReplayToolVisibility, ...] = ()
    steps: tuple[ReplayStepRecord, ...] = ()
    termination_reason: str = "none"
    termination_status: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ReplayContractSnapshot:
    version: int
    run: ReplayRunRecord


def build_replay_visibility(tool_name: str, *, visible: bool, source: str = "", reason: str = "", schema: dict[str, Any] | None = None) -> ReplayToolVisibility:
    return ReplayToolVisibility(
        name=tool_name,
        visible=visible,
        source=source,
        reason=reason,
        schema=schema or {},
    )


def build_replay_runtime_decision(decision: Any) -> ReplayRuntimeDecision:
    return ReplayRuntimeDecision(
        action=getattr(getattr(decision, "action", None), "value", str(getattr(decision, "action", ""))),
        reason=getattr(decision, "terminate_summary", "") or getattr(decision, "inject_message", "") or "",
        strip_tools=bool(getattr(decision, "strip_tools", False)),
        inject_message=str(getattr(decision, "inject_message", "") or ""),
        terminate_reason=getattr(getattr(decision, "terminate_reason", None), "value", "none"),
        terminate_status=getattr(getattr(decision, "terminate_status", None), "value", ""),
        terminate_detail=str(getattr(decision, "terminate_detail", "") or ""),
    )


def build_replay_action_snapshot(action: Any | None) -> dict[str, Any]:
    if action is None:
        return {}
    return {
        "action_type": getattr(getattr(action, "action_type", None), "value", str(getattr(action, "action_type", ""))),
        "thought": str(getattr(action, "thought", "") or ""),
        "message": str(getattr(action, "message", "") or ""),
        "tool_calls": [
            {
                "id": getattr(tc, "id", ""),
                "name": getattr(tc, "name", ""),
                "params": dict(getattr(tc, "params", {}) or {}),
            }
            for tc in getattr(action, "tool_calls", []) or []
        ],
    }



def build_replay_tool_execution(observation: Any, *, tool_call_id: str = "", params: dict[str, Any] | None = None) -> ReplayToolExecution:
    outcome = getattr(getattr(observation, "outcome", None), "value", None)
    return ReplayToolExecution(
        tool_name=str(getattr(observation, "tool_name", "") or ""),
        tool_call_id=tool_call_id,
        params=dict(params or getattr(observation, "params", {}) or {}),
        success=bool(getattr(observation, "is_success", lambda: False)()),
        output_summary=truncate_text(str(getattr(observation, "output", "") or ""), max_length=300),
        error=str(getattr(observation, "error", "") or ""),
        duration_ms=float(getattr(observation, "duration_ms", 0.0) or 0.0),
        outcome=str(outcome or "none"),
    )


def build_replay_step_record(
    *,
    step: int,
    decision: Any,
    visible_tools: list[Any],
    action: Any | None = None,
    tool_executions: list[ReplayToolExecution] | None = None,
    outcome: str = "continue",
    termination_reason: TerminationReason | str | None = None,
    termination_status: Any | None = None,
) -> ReplayStepRecord:
    visible = tuple(
        build_replay_visibility(
            getattr(tool, "name", ""),
            visible=True,
            source="registry",
            reason="visible in current step",
            schema=dataclass_to_dict(getattr(tool, "to_llm_schema", lambda: {})()),
        )
        for tool in visible_tools
    )
    return ReplayStepRecord(
        step=step,
        runtime_decision=build_replay_runtime_decision(decision),
        visible_tools=visible,
        model_action=build_replay_action_snapshot(action),
        tool_executions=tuple(tool_executions or []),
        outcome=outcome,
        termination_reason=(
            termination_reason.value if isinstance(termination_reason, TerminationReason)
            else str(termination_reason or "none")
        ),
        termination_status=getattr(termination_status, "value", str(termination_status or "")),
    )


def build_replay_run_record(
    *,
    run_id: str,
    task: Task,
    provenance: dict[str, Any] | None = None,
    permission_snapshot: dict[str, Any] | None = None,
    runtime_snapshot: dict[str, Any] | None = None,
    visible_tools: list[Any] | None = None,
    steps: list[ReplayStepRecord | dict[str, Any]] | None = None,
    termination_reason: TerminationReason | str = TerminationReason.NONE,
    termination_status: str = "",
    summary: str = "",
    session_id: str = "",
    generation: int = 0,
) -> ReplayRunRecord:
    return ReplayRunRecord(
        version=REPLAY_CONTRACT_VERSION,
        run_id=run_id,
        task_id=task.task_id,
        session_id=session_id,
        generation=generation,
        task=task.to_dict(),
        provenance=provenance or {},
        permission_snapshot=permission_snapshot or {},
        runtime_snapshot=runtime_snapshot or {},
        visible_tools=tuple(
            build_replay_visibility(
                getattr(tool, "name", ""),
                visible=True,
                source="registry",
                reason="visible at run start",
                schema=dataclass_to_dict(getattr(tool, "to_llm_schema", lambda: {})()),
            )
            for tool in (visible_tools or [])
        ),
        steps=tuple(steps or []),
        termination_reason=(
            termination_reason.value if isinstance(termination_reason, TerminationReason)
            else str(termination_reason)
        ),
        termination_status=termination_status,
        summary=summary,
    )


def build_replay_snapshot(run: ReplayRunRecord) -> ReplayContractSnapshot:
    return ReplayContractSnapshot(version=run.version, run=run)


# Existing observability helpers below

def build_task_input(task: Task) -> dict[str, Any]:
    return {
        "description": task.description,
        "repo_path": task.repo_path,
        "intent": task.intent,
        "issue_url": task.issue_url,
    }


def build_task_metadata(task: Task) -> dict[str, Any]:
    metadata = {
        "task_id": task.task_id,
        "intent": task.intent,
        "repo_path": task.repo_path,
        "max_steps": task.max_steps,
        "budget_tokens": task.budget_tokens,
        "has_issue_url": bool(task.issue_url),
        "entrypoint": task.metadata.get("entrypoint"),
        "mode": task.metadata.get("mode"),
        "phase": task.metadata.get("phase"),
        "session_id": task.metadata.get("session_id"),
        "parent_task_id": task.metadata.get("parent_task_id"),
        "subtask_id": task.metadata.get("subtask_id"),
        "subtask_type": task.metadata.get("subtask_type"),
        "role": task.metadata.get("role"),
        "agent_id": task.metadata.get("agent_id"),
        "coordinator_task_id": task.metadata.get("coordinator_task_id"),
        "isolation": task.metadata.get("isolation"),
        "depends_on": task.metadata.get("depends_on"),
        "spawn_index": task.metadata.get("spawn_index"),
        "round": task.metadata.get("round"),
        "provider": task.metadata.get("provider"),
        "model": task.metadata.get("model"),
    }
    metadata.update(task.metadata)
    return {k: v for k, v in metadata.items() if v is not None}


def build_run_output(result: RunResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "summary": truncate_text(result.summary or "", max_length=2_000),
        "steps_taken": result.steps_taken,
        "total_tokens": result.total_tokens,
        "has_patch": bool(result.patch),
    }


def build_run_metadata(result: RunResult) -> dict[str, Any]:
    metadata = {
        "status": result.status.value,
        "steps_taken": result.steps_taken,
        "total_tokens": result.total_tokens,
        "has_patch": bool(result.patch),
        "patch_length": len(result.patch or ""),
        "error": result.error,
    }
    if result.cache_stats is not None:
        metadata["cache_stats"] = dataclass_to_dict(result.cache_stats)
    return {k: v for k, v in metadata.items() if v is not None}


def build_analysis_run_metadata(
    *,
    run_stats: dict[str, Any] | None = None,
    context_stats: Any = None,
) -> dict[str, Any]:
    run_stats = run_stats or {}
    metadata: dict[str, Any] = {
        "tool_decisions": run_stats.get("tool_decisions"),
        "recovery_actions": run_stats.get("recovery_actions"),
        "claims_created": run_stats.get("claims_created"),
        "analysis_deferred_reads": run_stats.get("analysis_deferred_reads"),
        "phase_starts": run_stats.get("phase_starts"),
        "phase_ends": run_stats.get("phase_ends"),
        "analysis_phase_token_costs": run_stats.get("analysis_phase_token_costs"),
        "analysis_phase_llm_calls": run_stats.get("analysis_phase_llm_calls"),
    }
    if context_stats is not None:
        metadata.update({
            "analysis_phase": getattr(context_stats, "analysis_phase", ""),
            "analysis_files_read": getattr(context_stats, "analysis_files_read", 0),
            "analysis_inspect_reads": getattr(context_stats, "analysis_inspect_reads", 0),
            "analysis_verify_reads": getattr(context_stats, "analysis_verify_reads", 0),
            "analysis_evidence_records": getattr(context_stats, "analysis_evidence_records", 0),
            "analysis_phase_summaries": getattr(context_stats, "analysis_phase_summaries", 0),
            "analysis_claims": getattr(context_stats, "analysis_claims", 0),
            "analysis_tool_decisions": getattr(context_stats, "analysis_tool_decisions", 0),
            "analysis_recovery_actions": getattr(context_stats, "analysis_recovery_actions", 0),
            "analysis_deferred_reads_context": getattr(context_stats, "analysis_deferred_reads", 0),
            "analysis_phase_token_costs_context": getattr(context_stats, "analysis_phase_token_costs", {}),
        })
    return {k: v for k, v in metadata.items() if v not in (None, "", [])}


def summarize_messages(
    messages: list[LLMMessage],
    *,
    capture_prompts: bool,
) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for message in messages[-12:]:
        item: dict[str, Any] = {"role": message.role}
        if capture_prompts:
            item["content"] = summarize_content(message.content)
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [tool_call.to_dict() for tool_call in message.tool_calls]
        summarized.append(item)
    return summarized


def summarize_tools(tools: list[LLMToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": truncate_text(tool.description, max_length=300),
        }
        for tool in tools[:30]
    ]


def build_generation_input(
    messages: list[LLMMessage],
    tools: list[LLMToolSchema],
    *,
    capture_prompts: bool,
) -> dict[str, Any]:
    return {
        "message_count": len(messages),
        "messages": summarize_messages(messages, capture_prompts=capture_prompts),
        "tools": summarize_tools(tools),
    }


def build_generation_output(
    response: LLMResponse,
    *,
    capture_llm_outputs: bool,
) -> dict[str, Any]:
    output = {
        "action_type": response.action.action_type.value,
        "message": response.action.message,
        "tool_calls": [tool_call.to_dict() for tool_call in response.action.tool_calls],
    }
    if capture_llm_outputs:
        output["raw_content"] = response.raw_content
        output["thought"] = response.action.thought
    return output


def build_generation_metadata(
    response: LLMResponse,
    *,
    attempt: int,
    provider: str | None,
    model: str,
) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "provider": provider,
        "model": model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.total_tokens,
        "cache_stats": dataclass_to_dict(response.cache_stats),
    }


def build_tool_input(name: str, params: dict[str, Any], thought: str, step: int) -> dict[str, Any]:
    return {
        "tool_name": name,
        "params": params,
        "thought": thought,
        "step": step,
    }


def build_tool_output(
    result: ToolResult,
    *,
    capture_tool_outputs: bool,
) -> dict[str, Any]:
    output = {
        "success": result.success,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "outcome": getattr(result.normalized_outcome(), "value", "none"),
    }
    if capture_tool_outputs:
        output["output"] = result.output
    return output


def merge_metadata(*parts: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        merged.update({k: v for k, v in part.items() if v is not None})
    return merged


def dataclass_to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def summarize_content(content: str | list[dict[str, Any]]) -> Any:
    if isinstance(content, str):
        return truncate_text(content, max_length=1_500)
    return content[:10]

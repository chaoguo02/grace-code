"""runtime_core/native_agent_tool.py

Phase 2: NativeAgentTool — CC-aligned Agent delegation tool for Native path.

Dependency on AgentRuntime, not SessionRuntime.  No legacy imports.
Delegates to Phase 1 functions: child_runtime_ports, filter_tool_schemas,
build_child_conversation, run_native_child.
"""

from __future__ import annotations

import json
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from core.eventing.identifiers import SessionId, RunId
from runtime_core.execution import CancellationHandle
from runtime_core.native_child_contract import NativeChildRequest, NativeChildResult
from runtime_core.native_child_runner import (
    build_child_conversation,
    child_runtime_ports,
    filter_tool_schemas,
    run_native_child,
    run_native_child_background,
    run_native_child_in_worktree,
)

if TYPE_CHECKING:
    from agent.session.models import AgentDefinition
    from runtime_core.ports import RuntimePorts


class NativeAgentTool:
    """CC Agent Tool — spawn a named subagent on the Native execution path.

    Not a BaseTool subclass — does not depend on SessionRuntime, LLMBackend,
    or any legacy execution concepts.  Works directly with RuntimePorts +
    NativeStepLoop infrastructure.

    CC Agent Tool schema fields:
      description, prompt, subagent_type, model, run_in_background, isolation
    """

    name: str = "Agent"

    def __init__(
        self,
        definition_registry: dict[str, "AgentDefinition"],
        parent_ports: "RuntimePorts",
        parent_backend=None,  # NativeBackend | None (test mode)
        *,
        project_dir: str = "",  # Phase 3: for GRACE.md injection
    ) -> None:
        self._definitions = definition_registry
        self._parent_ports = parent_ports
        self._parent_backend = parent_backend
        self._project_dir = project_dir

    # ── Tool schema (CC Agent Tool) ──────────────────────────────────────────

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """CC Agent Tool JSON Schema — matches CC's Agent tool input fields."""
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the subagent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": (
                        "Which subagent to spawn. Available: "
                        + ", ".join(repr(n) for n in sorted(self._definitions))
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override. Use a cheaper/faster model "
                        "for read-only exploration. Empty = inherit parent."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run this agent in the background. When false, the "
                        "parent waits for completion. Phase 5."
                    ),
                },
                "isolation": {
                    "type": "string",
                    "description": (
                        "Filesystem isolation mode. 'worktree' = isolated git "
                        "worktree. Empty = current workspace. Phase 6."
                    ),
                },
            },
            "required": ["description", "prompt", "subagent_type"],
        }

    # ── Execute ──────────────────────────────────────────────────────────────

    def execute(self, params: dict) -> Any:
        """Execute one child run — CC Agent Tool semantics.

        1. Parse params → NativeChildRequest
        2. Resolve AgentDefinition
        3. Filter tool schemas (if parent_backend set)
        4. Construct child ports + conversation
        5. run_native_child → RuntimeOutcome
        6. Convert → NativeChildResult → JSON → ToolResult
        """
        import time as _time
        started = _time.monotonic()

        # Phase 1: parse request
        request = NativeChildRequest(
            description=str(params.get("description", "")),
            prompt=str(params.get("prompt", "")),
            subagent_type=str(params.get("subagent_type", "")),
            model=str(params.get("model", "")),
            run_in_background=bool(params.get("run_in_background", False)),
            isolation=str(params.get("isolation", "")),
        )

        # Phase 2: resolve definition
        definition = self._definitions.get(request.subagent_type)
        if definition is None:
            return _error_result(
                f"Unknown subagent type: {request.subagent_type!r}. "
                f"Available: {', '.join(sorted(self._definitions))}",
                duration_ms=(_time.monotonic() - started) * 1000,
            )

        # Phase 3: resolve model + filter tool schemas for child backend
        from runtime_core.native_child_context import resolve_child_model
        child_model = resolve_child_model(
            request.model,
            getattr(definition, "model", "") or "",
            self._parent_backend,
        ) if self._parent_backend is not None else ""

        child_backend = None
        if self._parent_backend is not None:
            allowed = getattr(definition, "tools", frozenset()) or frozenset()
            disallowed = getattr(definition, "disallowed_tools", frozenset()) or frozenset()
            parent_schemas = getattr(self._parent_backend, "tool_schemas", None)
            if parent_schemas is not None and allowed:
                filtered = filter_tool_schemas(parent_schemas, allowed, disallowed)
                child_backend = self._parent_backend.from_backend(
                    self._parent_backend, tool_schemas=filtered,
                )
                # Phase 3: apply model override to child backend
                if child_model and hasattr(child_backend, '_model'):
                    object.__setattr__(child_backend, '_model', child_model)

        # Phase 4: construct child execution
        child_ports = child_runtime_ports(self._parent_ports, definition)

        # If we have a child backend, swap the LLM port
        if child_backend is not None:
            from runtime_core.native_llm_adapter import NativeBackendAdapter
            child_ports = type(child_ports)(
                llm=NativeBackendAdapter(child_backend),
                tools=child_ports.tools,
                hooks=child_ports.hooks,
                live_events=child_ports.live_events,
                clock=child_ports.clock,
                token_usage=child_ports.token_usage,
            )

        child_session_id = SessionId(str(_uuid.uuid4()))
        child_run_id = RunId(str(_uuid.uuid4()))

        # Phase 5: build conversation (Phase 3: project_dir for GRACE.md)
        conversation = build_child_conversation(
            definition=definition,
            prompt=request.prompt,
            description=request.description,
            project_dir=self._project_dir,
        )

        # Phase 6: execute (foreground or background)
        max_steps = getattr(definition, "max_turns", 25) or 25

        if request.run_in_background:
            # Phase 5: background execution — CC async_launched
            run_native_child_background(
                ports=child_ports,
                session_id=child_session_id,
                run_id=child_run_id,
                conversation=conversation,
                cancellation=CancellationHandle(),
                max_steps=max_steps,
                budget_tokens=200_000,
            )
            output = json.dumps({
                "status": "async_launched",
                "agent_id": str(child_session_id),
                "description": request.description,
                "prompt": request.prompt,
            }, ensure_ascii=False)
            return _success_result(
                output=output,
                duration_ms=(_time.monotonic() - started) * 1000,
            )
        else:
            # Foreground: sync execution (with optional worktree isolation)
            if request.isolation == "worktree" and self._project_dir:
                outcome, disposition = run_native_child_in_worktree(
                    repo_path=self._project_dir,
                    definition_name=request.subagent_type,
                    agent_id=str(child_session_id),
                    ports=child_ports,
                    conversation=conversation,
                    session_id=child_session_id,
                    run_id=child_run_id,
                    max_steps=max_steps,
                    budget_tokens=200_000,
                    cancellation=CancellationHandle(),
                )
                child_result = NativeChildResult(
                    status=outcome.status.value,
                    agent_id=str(child_session_id),
                    content=outcome.summary or "",
                    total_tool_use_count=outcome.steps_taken,
                    total_duration_ms=(_time.monotonic() - started) * 1000,
                    total_tokens=outcome.tokens_used,
                    error=outcome.error,
                    worktree_disposition=disposition,
                )
            else:
                outcome = run_native_child(
                    ports=child_ports,
                    session_id=child_session_id,
                    run_id=child_run_id,
                    conversation=conversation,
                    cancellation=CancellationHandle(),
                    max_steps=max_steps,
                    budget_tokens=200_000,
                )
                child_result = NativeChildResult(
                    status=outcome.status.value,
                    agent_id=str(child_session_id),
                    content=outcome.summary or "",
                    total_tool_use_count=outcome.steps_taken,
                    total_duration_ms=(_time.monotonic() - started) * 1000,
                    total_tokens=outcome.tokens_used,
                    error=outcome.error,
                )
            output = json.dumps(child_result.to_dict(), ensure_ascii=False)

            return _success_result(
                output=output,
                duration_ms=(_time.monotonic() - started) * 1000,
                tokens=outcome.tokens_used,
            )


# ── ToolResult helpers (compatible with _RealTools._execute_dynamic) ──────────

def _success_result(output: str, duration_ms: float = 0.0, tokens: int = 0) -> Any:
    """Return an object compatible with _RealTools._execute_dynamic()."""
    from core.base import ToolResult
    return ToolResult(success=True, output=output, duration_ms=duration_ms, subagent_tokens_used=tokens)


def _error_result(error: str, duration_ms: float = 0.0) -> Any:
    """Return an error ToolResult."""
    from core.base import ToolResult
    return ToolResult(success=False, output=error, error=error, duration_ms=duration_ms)

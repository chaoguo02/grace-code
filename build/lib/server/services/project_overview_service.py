"""Project-level evidence map and reusable demo journeys."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class ProjectOverviewService:
    """Compose existing read models without turning absence into success."""

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service

    def get_overview(self, session_id: str = "") -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        architecture = self._capture(
            "architecture",
            lambda: self._service._architecture_service.get_snapshot(""),
            errors,
        )
        safety = self._capture(
            "safety",
            lambda: self._service._safety_service.get_snapshot(session_id),
            errors,
        )
        reliability = self._capture(
            "reliability",
            lambda: self._service._reliability_service.get_overview(30),
            errors,
        )
        evaluation = self._capture(
            "evaluation",
            self._service._evaluation_service.get_overview,
            errors,
        )
        multi_agent = (
            self._capture(
                "multi_agent",
                lambda: self._service._multi_agent_service.get_snapshot(
                    session_id
                ),
                errors,
            )
            if session_id else None
        )
        replay = (
            self._capture(
                "replay",
                lambda: self._service._replay_service.get_session_replay(
                    session_id
                ),
                errors,
            )
            if session_id else None
        )
        sessions = self._service.session_service.list_sessions(limit=8)

        runtime = architecture.get("runtime", {})
        agents = architecture.get("agents", [])
        tools = architecture.get("tools", [])
        skills = architecture.get("skills", [])
        mcp = architecture.get("mcp", {})
        reliability_summary = reliability.get("summary", {})
        evaluation_summary = evaluation.get("summary", {})
        safety_summary = safety.get("rule_summary", {})
        multi_summary = (
            (multi_agent or {}).get("scheduler", {}) if multi_agent else {}
        )
        replay_summary = (replay or {}).get("summary", {}) if replay else {}

        capabilities = self._capabilities(
            session_id=session_id,
            run_count=int(reliability_summary.get("run_count") or 0),
            tool_steps=int(
                reliability.get("coverage", {}).get("tool_steps") or 0
            ),
            agent_count=len(agents),
            topology_count=int(multi_summary.get("total_agents") or 0),
            safety_layer_count=len(safety.get("layers", [])),
            replay_contracts=int(replay_summary.get("contract_count") or 0),
            evaluation_runs=int(evaluation_summary.get("run_count") or 0),
        )
        observed_count = sum(
            item["evidence_state"] == "observed" for item in capabilities
        )
        configured_count = sum(
            item["evidence_state"] == "configured" for item in capabilities
        )

        return {
            "project": {
                "name": Path(self._service.repo_path).name or "Grace Code",
                "product_name": "Grace Code",
                "tagline": (
                    "An evidence-driven coding-agent workbench with explicit "
                    "safety, context, and multi-agent boundaries."
                ),
                "provider": str(runtime.get("provider") or "unknown"),
                "model": str(runtime.get("model") or "unknown"),
                "selected_session_id": session_id,
            },
            "headline": {
                "configured_agents": len(agents),
                "registered_tools": len(tools),
                "skills": len(skills),
                "mcp_servers": len(mcp.get("servers", [])),
                "recent_sessions": len(sessions),
                "persisted_runs_30d": int(
                    reliability_summary.get("run_count") or 0
                ),
                "run_success_rate": reliability_summary.get("success_rate"),
                "evaluation_pass_rate": evaluation_summary.get(
                    "latest_pass_rate"
                ),
            },
            "evidence_coverage": {
                "observed": observed_count,
                "configured": configured_count,
                "unavailable": len(capabilities)
                - observed_count - configured_count,
                "total": len(capabilities),
                "state": (
                    "evidence_ready"
                    if observed_count >= 5
                    else "partially_observed"
                    if observed_count
                    else "configured_only"
                ),
            },
            "capabilities": capabilities,
            "journeys": self._journeys(
                has_session=bool(session_id),
                has_multi_agent=int(multi_summary.get("total_agents") or 0) > 1,
                has_failure=bool(
                    reliability.get("failure_reasons", [])
                ),
            ),
            "recent_sessions": [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(
                        item.get("title") or item.get("summary") or "Untitled"
                    ),
                    "agent_name": str(item.get("agent_name") or "unknown"),
                    "status": str(item.get("status") or "unknown"),
                    "updated_at": str(item.get("updated_at") or ""),
                    "message_count": int(item.get("message_count") or 0),
                    "selected": item.get("id") == session_id,
                }
                for item in sessions
            ],
            "signals": {
                "reliability": {
                    "success_rate": reliability_summary.get("success_rate"),
                    "duration_p95_ms": reliability_summary.get(
                        "duration_p95_ms"
                    ),
                    "tool_error_rate": reliability_summary.get(
                        "tool_error_rate"
                    ),
                    "terminal_runs": int(
                        reliability_summary.get("terminal_run_count") or 0
                    ),
                },
                "evaluation": {
                    "run_count": int(
                        evaluation_summary.get("run_count") or 0
                    ),
                    "latest_pass_rate": evaluation_summary.get(
                        "latest_pass_rate"
                    ),
                    "regression_count": int(
                        evaluation_summary.get("regression_count") or 0
                    ),
                },
                "safety": {
                    "layers": len(safety.get("layers", [])),
                    "rules": int(safety_summary.get("total") or 0),
                    "tools": len(safety.get("tools", [])),
                    "session_approvals": int(
                        (safety.get("session") or {})
                        .get("approval_summary", {})
                        .get("total", 0)
                    ),
                },
                "multi_agent": {
                    "available_for_selected_session": multi_agent is not None,
                    "agents": int(multi_summary.get("total_agents") or 0),
                    "peak_parallelism": int(
                        multi_summary.get("peak_observed_parallelism") or 0
                    ),
                    "consistency": (
                        (multi_agent or {}).get("consistency", {}).get("state")
                        if multi_agent else None
                    ),
                },
                "replay": {
                    "available_for_selected_session": replay is not None,
                    "runs": int(replay_summary.get("run_count") or 0),
                    "contracts": int(
                        replay_summary.get("contract_count") or 0
                    ),
                    "valid": int(replay_summary.get("valid_count") or 0),
                },
            },
            "section_errors": errors,
            "disclosure": {
                "source": "composed_existing_read_models",
                "read_only": True,
                "capability_is_not_runtime_success": True,
                "missing_evidence_is_not_failure": True,
                "sections_degrade_independently": True,
            },
        }

    @staticmethod
    def _capture(
        section: str,
        loader: Callable[[], dict[str, Any]],
        errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        try:
            value = loader()
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            errors.append({
                "section": section,
                "message": str(exc) or type(exc).__name__,
            })
            return {}

    @staticmethod
    def _capabilities(
        *,
        session_id: str,
        run_count: int,
        tool_steps: int,
        agent_count: int,
        topology_count: int,
        safety_layer_count: int,
        replay_contracts: int,
        evaluation_runs: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "execution",
                "label": "Agent execution",
                "claim": "Streaming ReAct runs with typed terminal outcomes.",
                "route": "chat",
                "evidence_route": "runs",
                "evidence_state": "observed" if run_count else "configured",
                "evidence": (
                    f"{run_count} persisted run(s) in 30 days"
                    if run_count else "Runtime configured; no recent run evidence"
                ),
            },
            {
                "id": "explainability",
                "label": "Run explainability",
                "claim": "Tools, context, verification, and workspace facts remain inspectable.",
                "route": "runs",
                "evidence_route": "context",
                "evidence_state": "observed" if tool_steps else "configured",
                "evidence": (
                    f"{tool_steps} persisted tool step(s)"
                    if tool_steps else "Inspectors available; no tool steps in window"
                ),
            },
            {
                "id": "multi_agent",
                "label": "Multi-agent orchestration",
                "claim": "Explicit delegation, context origin, scheduling, and worktree convergence.",
                "route": "agents",
                "evidence_route": "agents",
                "evidence_state": (
                    "observed" if topology_count > 1 else
                    "configured" if agent_count > 1 else "unavailable"
                ),
                "evidence": (
                    f"{topology_count} Agent sessions in selected topology"
                    if topology_count > 1 else
                    f"{agent_count} Agent definitions; select a delegated session"
                ),
            },
            {
                "id": "safety",
                "label": "Safety & HITL",
                "claim": "Fail-closed policy layers with durable approval evidence.",
                "route": "safety",
                "evidence_route": "safety",
                "evidence_state": (
                    "observed" if safety_layer_count else "unavailable"
                ),
                "evidence": f"{safety_layer_count} live authority layer(s)",
            },
            {
                "id": "replay",
                "label": "Replay & failure analysis",
                "claim": "Persisted contracts distinguish replayable evidence from reconstruction.",
                "route": "replay",
                "evidence_route": "replay",
                "evidence_state": (
                    "observed" if replay_contracts else
                    "configured" if session_id else "unavailable"
                ),
                "evidence": (
                    f"{replay_contracts} replay contract(s)"
                    if replay_contracts else
                    "Select a session with a new run to inspect contracts"
                ),
            },
            {
                "id": "quality",
                "label": "Evaluation & regression",
                "claim": "Scenario artifacts are kept separate from ordinary chat completion.",
                "route": "evaluations",
                "evidence_route": "evaluations",
                "evidence_state": (
                    "observed" if evaluation_runs else "configured"
                ),
                "evidence": (
                    f"{evaluation_runs} evaluation artifact run(s)"
                    if evaluation_runs else "Evaluation harness configured; no artifacts"
                ),
            },
            {
                "id": "operations",
                "label": "Operational health",
                "claim": "Cross-session success, latency, token, and tool reliability signals.",
                "route": "reliability",
                "evidence_route": "reliability",
                "evidence_state": "observed" if run_count else "configured",
                "evidence": (
                    f"{run_count} run(s) contribute to the health window"
                    if run_count else "Health model configured; no recent run evidence"
                ),
            },
        ]

    @staticmethod
    def _journeys(
        *,
        has_session: bool,
        has_multi_agent: bool,
        has_failure: bool,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "normal_development",
                "number": "01",
                "title": "Normal development",
                "duration_minutes": 6,
                "readiness": "ready" if has_session else "needs_session",
                "goal": "Show a request becoming an explainable, reviewable code change.",
                "steps": [
                    {"route": "chat", "label": "Run or open a coding session", "proof": "streaming reasoning and typed tools"},
                    {"route": "runs", "label": "Inspect the terminal run", "proof": "outcome, verification, workspace delta"},
                    {"route": "context", "label": "Explain the context boundary", "proof": "tokens, capabilities, compaction"},
                    {"route": "reviews", "label": "Review the resulting diff", "proof": "human decision remains explicit"},
                ],
            },
            {
                "id": "delegation_worktree",
                "number": "02",
                "title": "Subagent + worktree",
                "duration_minutes": 7,
                "readiness": (
                    "ready" if has_multi_agent else
                    "needs_delegated_session" if has_session else "needs_session"
                ),
                "goal": "Show parallel delegation without shared-context or workspace ambiguity.",
                "steps": [
                    {"route": "agents", "label": "Open the delegation topology", "proof": "parent, placement, generation"},
                    {"route": "agents", "label": "Compare Agent contexts", "proof": "fresh, snapshot, resumed"},
                    {"route": "reviews", "label": "Inspect isolated changes", "proof": "worktree evidence and review"},
                    {"route": "safety", "label": "Explain inherited authority", "proof": "child cannot relax parent policy"},
                ],
            },
            {
                "id": "failure_recovery",
                "number": "03",
                "title": "Failure, cancellation & recovery",
                "duration_minutes": 6,
                "readiness": (
                    "ready" if has_failure else
                    "needs_failure_evidence" if has_session else "needs_session"
                ),
                "goal": "Show that failures become typed evidence instead of disappearing from the UI.",
                "steps": [
                    {"route": "replay", "label": "Open the replay contract", "proof": "termination boundary and provenance"},
                    {"route": "events", "label": "Inspect the event sequence", "proof": "ordered persisted lifecycle"},
                    {"route": "reliability", "label": "Locate the failure category", "proof": "cross-session taxonomy"},
                    {"route": "chat", "label": "Resume or retry deliberately", "proof": "new generation, preserved history"},
                ],
            },
        ]

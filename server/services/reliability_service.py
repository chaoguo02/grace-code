"""Cross-session reliability and resource-consumption projection."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[int], percentile: float) -> int | None:
    """Return a deterministic nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(max(0, int(value)) for value in values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return ordered[index]


class ReliabilityService:
    """Aggregate persisted runs and tool outcomes without assigning money cost."""

    TERMINAL = {"completed", "failed", "cancelled", "partial"}
    SUCCESS = {"completed", "success", "finish"}
    MAX_SESSIONS = 500
    RUNS_PER_SESSION = 100

    def __init__(self, agent_service: Any) -> None:
        self._service = agent_service

    def get_overview(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        sessions = self._service.session_service.list_sessions(
            limit=self.MAX_SESSIONS,
        )
        session_by_id = {str(item["id"]): item for item in sessions}
        recent_sessions = [
            item for item in sessions
            if (_parse_time(item.get("created_at")) or now) >= cutoff
        ]
        runs = self._runs(recent_sessions, cutoff)
        terminal = [
            run for run in runs if str(run.get("status", "")) in self.TERMINAL
        ]
        durations = [
            duration for run in terminal
            if (duration := self._duration_ms(run)) is not None
        ]
        tokens = [max(0, int(run.get("total_tokens") or 0)) for run in terminal]
        statuses = Counter(str(run.get("status") or "unknown") for run in runs)
        success_count = sum(
            1 for run in terminal
            if str(run.get("status") or "") in self.SUCCESS
        )
        failure_reasons = Counter(
            self._failure_reason(run)
            for run in terminal
            if str(run.get("status") or "") not in self.SUCCESS
        )
        tools = self._tools(recent_sessions)
        tool_calls = sum(item["calls"] for item in tools)
        tool_failures = sum(item["failures"] for item in tools)
        success_rate = success_count / len(terminal) if terminal else None
        tool_error_rate = tool_failures / tool_calls if tool_calls else None
        duration_p95 = _percentile(durations, 0.95)
        terminal_evidence_rate = (
            sum(bool(run.get("completed_at")) for run in terminal) / len(terminal)
            if terminal else None
        )

        return {
            "window": {
                "days": days,
                "from": cutoff.isoformat(),
                "to": now.isoformat(),
                "session_limit": self.MAX_SESSIONS,
                "runs_per_session_limit": self.RUNS_PER_SESSION,
            },
            "summary": {
                "session_count": len(recent_sessions),
                "run_count": len(runs),
                "terminal_run_count": len(terminal),
                "active_run_count": len(runs) - len(terminal),
                "success_rate": success_rate,
                "total_tokens": sum(tokens),
                "average_tokens": (
                    sum(tokens) / len(tokens) if tokens else None
                ),
                "duration_p50_ms": _percentile(durations, 0.50),
                "duration_p95_ms": duration_p95,
                "tool_call_count": tool_calls,
                "tool_error_rate": tool_error_rate,
            },
            "status_counts": dict(statuses),
            "failure_reasons": [
                {"reason": reason, "count": count}
                for reason, count in failure_reasons.most_common()
            ],
            "tools": tools,
            "trend": self._trend(runs, days, now),
            "agents": self._agents(terminal, session_by_id),
            "recent_runs": self._recent_runs(runs, session_by_id),
            "objectives": [
                self._objective(
                    "run_success",
                    "Terminal run success",
                    success_rate,
                    0.90,
                    "gte",
                    "Completed terminal runs / all terminal runs",
                ),
                self._objective(
                    "p95_latency",
                    "P95 completion latency",
                    duration_p95,
                    300_000,
                    "lte",
                    "Reference threshold: five minutes",
                ),
                self._objective(
                    "tool_errors",
                    "Tool error rate",
                    tool_error_rate,
                    0.05,
                    "lte",
                    "Failed persisted tool steps / all persisted tool steps",
                ),
                self._objective(
                    "terminal_evidence",
                    "Terminal timestamp coverage",
                    terminal_evidence_rate,
                    0.95,
                    "gte",
                    "Terminal runs with a persisted completion timestamp",
                ),
            ],
            "coverage": {
                "sessions_scanned": len(recent_sessions),
                "sessions_with_runs": len({
                    str(run.get("session_id") or "") for run in runs
                }),
                "terminal_runs": len(terminal),
                "runs_with_duration": len(durations),
                "failed_runs_with_reason": sum(
                    reason != "unclassified"
                    for reason in failure_reasons.elements()
                ),
                "tool_steps": tool_calls,
            },
            "disclosure": {
                "source": "persisted_runs_and_step_log",
                "currency_cost_available": False,
                "token_usage_is_not_currency_cost": True,
                "reference_objectives_are_not_production_slas": True,
                "zero_token_legacy_runs_are_preserved": True,
            },
        }

    def _runs(
        self,
        sessions: list[dict[str, Any]],
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for session in sessions:
            for raw in self._service.session_service.list_runs(
                str(session["id"]),
                limit=self.RUNS_PER_SESSION,
            ):
                run = dict(raw)
                created = (
                    _parse_time(run.get("started_at"))
                    or _parse_time(run.get("created_at"))
                )
                if created is not None and created < cutoff:
                    continue
                run.setdefault("session_id", str(session["id"]))
                runs.append(run)
        return sorted(
            runs,
            key=lambda run: str(
                run.get("started_at") or run.get("created_at") or ""
            ),
            reverse=True,
        )

    def _tools(
        self, sessions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        totals: dict[str, Counter] = defaultdict(Counter)
        for session in sessions:
            steps = self._service._stats_service.get_session_steps(
                str(session["id"])
            )
            for step in steps:
                name = str(step.get("tool_name") or "unknown")
                status = str(step.get("status") or "unknown").lower()
                failed = status in {"error", "failed", "failure"}
                totals[name]["calls"] += 1
                totals[name]["failures" if failed else "successes"] += 1
                totals[name]["duration_ms"] += max(
                    0, int(step.get("duration_ms") or 0)
                )
        return sorted(
            [
                {
                    "name": name,
                    "calls": facts["calls"],
                    "successes": facts["successes"],
                    "failures": facts["failures"],
                    "error_rate": (
                        facts["failures"] / facts["calls"]
                        if facts["calls"] else 0.0
                    ),
                    "average_duration_ms": (
                        facts["duration_ms"] / facts["calls"]
                        if facts["calls"] else 0.0
                    ),
                }
                for name, facts in totals.items()
            ],
            key=lambda item: (
                -item["failures"], -item["calls"], item["name"]
            ),
        )

    @staticmethod
    def _duration_ms(run: dict[str, Any]) -> int | None:
        start = _parse_time(run.get("started_at"))
        end = _parse_time(run.get("completed_at"))
        if start is None or end is None:
            return None
        return max(0, int((end - start).total_seconds() * 1000))

    @staticmethod
    def _failure_reason(run: dict[str, Any]) -> str:
        reason = str(run.get("termination_reason") or "").strip().lower()
        if reason and reason != "none":
            return reason
        if str(run.get("status") or "") == "cancelled":
            return "cancelled"
        return "unclassified"

    def _trend(
        self, runs: list[dict[str, Any]], days: int, now: datetime,
    ) -> list[dict[str, Any]]:
        bucket_count = min(days, 30)
        buckets = {
            (now - timedelta(days=offset)).date().isoformat(): []
            for offset in range(bucket_count - 1, -1, -1)
        }
        for run in runs:
            stamp = (
                _parse_time(run.get("completed_at"))
                or _parse_time(run.get("started_at"))
                or _parse_time(run.get("created_at"))
            )
            if stamp is not None and stamp.date().isoformat() in buckets:
                buckets[stamp.date().isoformat()].append(run)
        trend = []
        for date, items in buckets.items():
            terminal = [
                item for item in items
                if str(item.get("status") or "") in self.TERMINAL
            ]
            successes = sum(
                str(item.get("status") or "") in self.SUCCESS
                for item in terminal
            )
            trend.append({
                "date": date,
                "runs": len(items),
                "success_rate": (
                    successes / len(terminal) if terminal else None
                ),
                "tokens": sum(
                    max(0, int(item.get("total_tokens") or 0))
                    for item in terminal
                ),
            })
        return trend

    def _agents(
        self,
        runs: list[dict[str, Any]],
        sessions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            session = sessions.get(str(run.get("session_id") or ""), {})
            grouped[str(session.get("agent_name") or "unknown")].append(run)
        items = []
        for name, agent_runs in grouped.items():
            successes = sum(
                str(run.get("status") or "") in self.SUCCESS
                for run in agent_runs
            )
            durations = [
                value for run in agent_runs
                if (value := self._duration_ms(run)) is not None
            ]
            items.append({
                "name": name,
                "runs": len(agent_runs),
                "success_rate": successes / len(agent_runs),
                "tokens": sum(
                    max(0, int(run.get("total_tokens") or 0))
                    for run in agent_runs
                ),
                "duration_p95_ms": _percentile(durations, 0.95),
            })
        return sorted(items, key=lambda item: (-item["runs"], item["name"]))

    def _recent_runs(
        self,
        runs: list[dict[str, Any]],
        sessions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = []
        for run in runs[:20]:
            session = sessions.get(str(run.get("session_id") or ""), {})
            items.append({
                "id": str(run.get("id") or ""),
                "session_id": str(run.get("session_id") or ""),
                "session_title": str(session.get("title") or "Untitled"),
                "agent_name": str(session.get("agent_name") or "unknown"),
                "status": str(run.get("status") or "unknown"),
                "termination_reason": self._failure_reason(run),
                "tokens": max(0, int(run.get("total_tokens") or 0)),
                "steps": max(0, int(run.get("steps_taken") or 0)),
                "duration_ms": self._duration_ms(run),
                "started_at": str(
                    run.get("started_at") or run.get("created_at") or ""
                ),
            })
        return items

    @staticmethod
    def _objective(
        objective_id: str,
        label: str,
        observed: float | int | None,
        target: float | int,
        comparator: str,
        detail: str,
    ) -> dict[str, Any]:
        met = None
        if observed is not None:
            met = observed >= target if comparator == "gte" else observed <= target
        return {
            "id": objective_id,
            "label": label,
            "observed": observed,
            "target": target,
            "comparator": comparator,
            "met": met,
            "detail": detail,
        }

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from server.services.reliability_service import ReliabilityService, _percentile


class _Sessions:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.sessions = [
            {
                "id": "s1",
                "agent_name": "build",
                "title": "Successful run",
                "created_at": (now - timedelta(hours=2)).isoformat(),
            },
            {
                "id": "s2",
                "agent_name": "review",
                "title": "Failed run",
                "created_at": (now - timedelta(hours=1)).isoformat(),
            },
        ]
        self.now = now

    def list_sessions(self, *, limit):
        assert limit == 500
        return self.sessions

    def list_runs(self, session_id, *, limit):
        assert limit == 100
        base = self.now - timedelta(minutes=10)
        if session_id == "s1":
            return [{
                "id": "r1",
                "session_id": "s1",
                "status": "completed",
                "total_tokens": 1200,
                "steps_taken": 4,
                "termination_reason": "goal_achieved",
                "created_at": base.isoformat(),
                "started_at": base.isoformat(),
                "completed_at": (base + timedelta(seconds=10)).isoformat(),
            }]
        return [{
            "id": "r2",
            "session_id": "s2",
            "status": "failed",
            "total_tokens": 800,
            "steps_taken": 2,
            "termination_reason": "max_steps",
            "created_at": base.isoformat(),
            "started_at": base.isoformat(),
            "completed_at": (base + timedelta(seconds=20)).isoformat(),
        }]


class _Stats:
    def get_session_steps(self, session_id):
        if session_id == "s1":
            return [
                {"tool_name": "Read", "status": "success", "duration_ms": 10},
                {"tool_name": "Edit", "status": "success", "duration_ms": 30},
            ]
        return [
            {"tool_name": "Read", "status": "error", "duration_ms": 20},
        ]


def test_reliability_overview_uses_persisted_runs_and_steps() -> None:
    service = ReliabilityService(SimpleNamespace(
        session_service=_Sessions(),
        _stats_service=_Stats(),
    ))

    overview = service.get_overview(7)

    assert overview["summary"]["run_count"] == 2
    assert overview["summary"]["success_rate"] == 0.5
    assert overview["summary"]["total_tokens"] == 2000
    assert overview["summary"]["duration_p95_ms"] == 20_000
    assert overview["failure_reasons"] == [{"reason": "max_steps", "count": 1}]
    assert overview["tools"][0]["name"] == "Read"
    assert overview["tools"][0]["error_rate"] == 0.5
    assert overview["agents"][0]["name"] == "build"
    assert overview["disclosure"]["currency_cost_available"] is False


def test_percentile_uses_nearest_rank_and_empty_is_unknown() -> None:
    assert _percentile([], 0.95) is None
    assert _percentile([10, 20, 30, 40], 0.50) == 20
    assert _percentile([10, 20, 30, 40], 0.95) == 40

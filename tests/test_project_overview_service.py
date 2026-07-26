from types import SimpleNamespace

from server.services.project_overview_service import ProjectOverviewService


class _Sessions:
    def list_sessions(self, *, limit):
        assert limit == 8
        return [{
            "id": "s1",
            "title": "Demo session",
            "summary": "",
            "agent_name": "build",
            "status": "completed",
            "updated_at": "2026-07-26T10:00:00+00:00",
            "message_count": 4,
        }]


def _service(*, broken_evaluation=False):
    def evaluation():
        if broken_evaluation:
            raise RuntimeError("artifact reader unavailable")
        return {
            "summary": {
                "run_count": 2,
                "latest_pass_rate": 1.0,
                "regression_count": 0,
            },
        }

    return ProjectOverviewService(SimpleNamespace(
        repo_path="D:/repo/grace-code",
        session_service=_Sessions(),
        _architecture_service=SimpleNamespace(get_snapshot=lambda _: {
            "runtime": {"provider": "openai", "model": "gpt-test"},
            "agents": [{"name": "build"}, {"name": "review"}],
            "tools": [{"name": "Read"}, {"name": "Edit"}],
            "skills": [{"name": "demo"}],
            "mcp": {"servers": []},
        }),
        _safety_service=SimpleNamespace(get_snapshot=lambda _: {
            "layers": [{}, {}],
            "tools": [{}, {}],
            "rule_summary": {"total": 1},
            "session": {"approval_summary": {"total": 1}},
        }),
        _reliability_service=SimpleNamespace(get_overview=lambda _: {
            "summary": {
                "run_count": 3,
                "success_rate": 2 / 3,
                "duration_p95_ms": 1000,
                "tool_error_rate": 0.1,
                "terminal_run_count": 3,
            },
            "coverage": {"tool_steps": 5},
            "failure_reasons": [{"reason": "max_steps", "count": 1}],
        }),
        _evaluation_service=SimpleNamespace(get_overview=evaluation),
        _multi_agent_service=SimpleNamespace(get_snapshot=lambda _: {
            "scheduler": {
                "total_agents": 2,
                "peak_observed_parallelism": 2,
            },
            "consistency": {"state": "healthy"},
        }),
        _replay_service=SimpleNamespace(get_session_replay=lambda _: {
            "summary": {
                "run_count": 2,
                "contract_count": 1,
                "valid_count": 1,
            },
        }),
    ))


def test_overview_composes_evidence_and_demo_readiness() -> None:
    overview = _service().get_overview("s1")

    assert overview["project"]["name"] == "grace-code"
    assert overview["headline"]["persisted_runs_30d"] == 3
    assert overview["evidence_coverage"]["state"] == "evidence_ready"
    capabilities = {
        item["id"]: item for item in overview["capabilities"]
    }
    assert capabilities["multi_agent"]["evidence_state"] == "observed"
    assert capabilities["replay"]["evidence_state"] == "observed"
    assert all(
        journey["readiness"] == "ready"
        for journey in overview["journeys"]
    )
    assert overview["recent_sessions"][0]["selected"] is True
    assert overview["section_errors"] == []


def test_one_broken_section_degrades_without_losing_overview() -> None:
    overview = _service(broken_evaluation=True).get_overview("")

    assert overview["project"]["product_name"] == "Grace Code"
    assert overview["headline"]["evaluation_pass_rate"] is None
    assert overview["section_errors"] == [{
        "section": "evaluation",
        "message": "artifact reader unavailable",
    }]
    quality = next(
        item for item in overview["capabilities"] if item["id"] == "quality"
    )
    assert quality["evidence_state"] == "configured"

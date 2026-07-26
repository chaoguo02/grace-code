from agent.session.task_shape import (
    AgentTopology,
    ContextVolume,
    CoordinationNeed,
    TaskPurpose,
    TaskShape,
    WorkItem,
)
from agent.session.topology_planner import TopologyPlanner, TopologyPolicy
from agent.task import TaskIntent


def _shape(*items, **overrides):
    values = {
        "intent": TaskIntent.ANALYSIS,
        "purpose": TaskPurpose.EXPLORATION,
        "domains": ("repo",),
        "work_items": tuple(items),
    }
    values.update(overrides)
    return TaskShape(**values)


def test_planner_selects_single_one_to_one_fanout_and_chain():
    planner = TopologyPlanner()
    assert planner.plan(_shape()).topology is AgentTopology.SINGLE

    one = WorkItem("one", "Inspect one", "repo", estimated_tokens=3_000)
    assert planner.plan(_shape(one)).topology is AgentTopology.ONE_TO_ONE

    two = WorkItem("two", "Inspect two", "repo", estimated_tokens=3_000)
    fanout = planner.plan(_shape(one, two))
    assert fanout.topology is AgentTopology.FAN_OUT_FAN_IN
    assert fanout.estimated_budget.parent_reserve_tokens == 25_000
    assert fanout.estimated_budget.recovery_reserve_tokens == 10_000

    dependent = WorkItem(
        "two", "Inspect after one", "repo", depends_on=("one",)
    )
    assert planner.plan(_shape(one, dependent)).topology is AgentTopology.CHAIN


def test_planner_downgrades_conflicting_writes_and_budget():
    shape = _shape(
        WorkItem("a", "Edit A", "repo", write_files=("same.py",)),
        WorkItem("b", "Edit B", "repo", write_files=("same.py",)),
        intent=TaskIntent.EDIT,
        purpose=TaskPurpose.IMPLEMENTATION,
    )
    decision = TopologyPlanner().plan(
        shape, TopologyPolicy(available_tokens=20_000, worktree_writes=True)
    )
    assert decision.topology is AgentTopology.CHAIN
    assert decision.reason_code == "write_conflict_requires_serial"

    read_shape = _shape(
        WorkItem("a", "Read A", "repo"),
        WorkItem("b", "Read B", "repo"),
    )
    low_budget = TopologyPlanner().plan(
        read_shape,
        TopologyPolicy(available_tokens=2_500, minimum_worker_tokens=2_000),
    )
    assert low_budget.topology is AgentTopology.SINGLE
    assert low_budget.reason_code == "delegation_budget_insufficient"


def test_team_and_nested_require_explicit_gates():
    team_shape = _shape(
        WorkItem("a", "Challenge A", "repo"),
        WorkItem("b", "Challenge B", "repo"),
        coordination_need=CoordinationNeed.PEER_TO_PEER,
    )
    disabled = TopologyPlanner().plan(team_shape)
    assert disabled.topology is AgentTopology.FAN_OUT_FAN_IN
    assert disabled.downgraded_from is AgentTopology.TEAM
    assert disabled.reason_code == "team_feature_disabled"

    approved = TopologyPlanner().plan(
        team_shape, TopologyPolicy(team_enabled=True, team_approved=True)
    )
    assert approved.topology is AgentTopology.TEAM

    nested_shape = _shape(
        *(WorkItem(str(i), f"Inspect {i}", "repo") for i in range(5)),
        context_volume=ContextVolume.LARGE,
        user_requested_topology=AgentTopology.NESTED,
    )
    nested = TopologyPlanner().plan(
        nested_shape,
        TopologyPolicy(
            nested_enabled=True,
            max_subagent_spawn_depth=2,
            max_fanout=3,
        ),
    )
    assert nested.topology is AgentTopology.NESTED


def test_fanout_is_capped_by_concurrency_and_budget():
    shape = _shape(
        *(WorkItem(str(i), f"Inspect {i}", "repo") for i in range(6))
    )
    decision = TopologyPlanner().plan(
        shape,
        TopologyPolicy(
            max_fanout=4,
            max_concurrent_subagents=3,
            active_count=1,
            available_tokens=100_000,
        ),
    )
    assert decision.topology is AgentTopology.FAN_OUT_FAN_IN
    assert len(decision.work_items) == 2
    assert decision.estimated_budget.max_workers == 2


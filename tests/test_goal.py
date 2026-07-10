from __future__ import annotations

import pytest

from agent.task import Action, ActionType
from llm.base import LLMResponse, LLMToolSchema, LLMBackend
from runtime.goal import GoalState, GoalStore, JudgeResult, goal_stop_hook, judge_goal


class JudgeBackend(LLMBackend):
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[list, list]] = []

    def complete(self, messages, tools: list[LLMToolSchema]) -> LLMResponse:
        self.calls.append((messages, tools))
        return LLMResponse(
            action=Action(action_type=ActionType.FINISH, thought="judge", message=self.text),
            raw_content=self.text,
        )

    @property
    def model_name(self) -> str:
        return "judge"


def test_goal_state_rejects_overlong_condition():
    with pytest.raises(ValueError):
        GoalState("x" * 4001)


def test_judge_goal_uses_independent_backend_with_no_tools():
    backend = JudgeBackend('{"satisfied": true, "reason": "tests passed"}')
    result = judge_goal(
        GoalState("tests pass"),
        [{"role": "assistant", "content": "pytest passed"}],
        backend_factory=lambda model: backend,
    )

    assert result == JudgeResult(satisfied=True, reason="tests passed")
    assert backend.calls
    assert backend.calls[0][1] == []
    assert "Completion condition" in backend.calls[0][0][0].content
    assert "pytest passed" in backend.calls[0][0][0].content


def test_judge_goal_non_json_blocks_by_default():
    backend = JudgeBackend("looks good")
    result = judge_goal(
        GoalState("be done"),
        [],
        backend_factory=lambda model: backend,
    )

    assert result.satisfied is False
    assert "non-JSON" in result.reason


def test_goal_stop_hook_blocks_until_satisfied():
    responses = iter([
        JudgeBackend('{"satisfied": false, "reason": "lint not shown"}'),
        JudgeBackend('{"satisfied": true, "reason": "lint passed"}'),
    ])
    goal = GoalState("lint passes")

    first = goal_stop_hook(goal, [], backend_factory=lambda model: next(responses))
    second = goal_stop_hook(goal, [], backend_factory=lambda model: next(responses))

    assert first is not None
    assert "Goal not yet met" in first[0]["content"]
    assert second is None
    assert goal.active is False
    assert goal.turn_count == 2


def test_goal_stop_hook_allows_after_max_turns():
    goal = GoalState("finish", max_turns=1)
    backend = JudgeBackend('{"satisfied": false, "reason": "not enough evidence"}')

    result = goal_stop_hook(goal, [], backend_factory=lambda model: backend)

    assert result is None
    assert goal.active is False
    assert goal.turn_count == 1


def test_goal_store_persists_and_restore_resets_turn_count(tmp_path):
    path = tmp_path / "goal.json"
    store = GoalStore(path)
    store.set(GoalState("tests pass", session_id="s1", turn_count=3))

    restored_store = GoalStore(path)
    goal = restored_store.restore()

    assert goal is not None
    assert goal.condition == "tests pass"
    assert goal.session_id == "s1"
    assert goal.turn_count == 0


def test_goal_stop_hook_updates_store_reason_and_satisfaction(tmp_path):
    store = GoalStore(tmp_path / "goal.json")
    store.set(GoalState("tests pass"))
    backend = JudgeBackend('{"satisfied": true, "reason": "all checks passed"}')

    result = goal_stop_hook(store, [], backend_factory=lambda model: backend)

    goal = store.get()
    assert result is None
    assert goal is not None
    assert goal.active is False
    assert goal.satisfied is True
    assert goal.last_judge_reason == "all checks passed"

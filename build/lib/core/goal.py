"""Goal evaluation support built on Stop Hook semantics."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from llm.base import LLMBackend, LLMMessage

MAX_GOAL_CONDITION_CHARS = 4_000


@dataclass
class GoalState:
    """Session-level completion condition judged by an independent model."""

    condition: str
    session_id: str = ""
    active: bool = True
    judge_model: str = "haiku"
    max_turns: int = 20
    turn_count: int = 0
    satisfied: bool = False
    last_judge_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    def __post_init__(self) -> None:
        if len(self.condition) > MAX_GOAL_CONDITION_CHARS:
            raise ValueError(f"goal condition exceeds {MAX_GOAL_CONDITION_CHARS} characters")


class GoalStore:
    """Session-scoped store: at most one active goal."""

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._goal: GoalState | None = None
        self._persist_path = Path(persist_path) if persist_path is not None else None

    def set(self, goal: GoalState) -> None:
        if self._goal and self._goal.active:
            self._goal.active = False
            self._goal.completed_at = time.time()
        self._goal = goal
        self._persist()

    def get(self) -> GoalState | None:
        return self._goal

    def clear(self) -> None:
        if self._goal:
            self._goal.active = False
            self._goal.completed_at = time.time()
        self._goal = None
        self._persist()

    def mark_satisfied(self, reason: str) -> None:
        if self._goal:
            self._goal.satisfied = True
            self._goal.active = False
            self._goal.last_judge_reason = reason
            self._goal.completed_at = time.time()
            self._persist()

    def record_turn(self, reason: str) -> None:
        if self._goal:
            self._goal.turn_count += 1
            self._goal.last_judge_reason = reason
            self._persist()

    def restore(self) -> GoalState | None:
        if self._persist_path is None or not self._persist_path.exists():
            return None
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not data or not data.get("active"):
            return None
        goal = GoalState(**data)
        goal.turn_count = 0
        self._goal = goal
        self._persist()
        return goal

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self._goal) if self._goal else None
            self._persist_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            return


@dataclass(frozen=True)
class JudgeResult:
    """Result returned by the independent goal judge."""

    satisfied: bool
    reason: str


JudgeBackendFactory = Callable[[str], LLMBackend]


def judge_goal(
    goal: GoalState,
    messages: list[Any],
    *,
    backend_factory: JudgeBackendFactory,
) -> JudgeResult:
    """Evaluate a goal with an independent model and no tools."""
    backend = backend_factory(goal.judge_model)
    prompt = _build_goal_judge_prompt(goal, messages)
    response = backend.complete([LLMMessage(role="user", content=prompt)], tools=[])
    return _parse_judge_response(response.raw_content)


def goal_stop_hook(
    goal_or_store: GoalState | GoalStore | None,
    messages: list[Any],
    *,
    backend_factory: JudgeBackendFactory,
) -> list[dict[str, str]] | None:
    """Stop Hook adapter: block completion until the goal judge is satisfied."""
    store = goal_or_store if isinstance(goal_or_store, GoalStore) else None
    goal = store.get() if store is not None else goal_or_store
    if goal is None or not goal.active:
        return None

    result = judge_goal(goal, messages, backend_factory=backend_factory)
    if store is not None:
        store.record_turn(result.reason)
        goal = store.get() or goal
    else:
        goal.turn_count += 1
        goal.last_judge_reason = result.reason

    if result.satisfied:
        if store is not None:
            store.mark_satisfied(result.reason)
        else:
            goal.satisfied = True
            goal.active = False
            goal.completed_at = time.time()
        return None

    if goal.turn_count >= goal.max_turns:
        reason = f"Goal max turns reached: {result.reason}"
        if store is not None:
            store.mark_satisfied(reason)
        else:
            goal.active = False
            goal.completed_at = time.time()
            goal.last_judge_reason = reason
        return None

    return [{
        "role": "user",
        "content": (
            "[Goal not yet met]\n"
            f"Condition: {goal.condition}\n"
            f"Evaluation: {result.reason}\n"
            f"Turn: {goal.turn_count}/{goal.max_turns}\n"
            "Continue working until the completion condition is satisfied."
        ),
    }]


def _build_goal_judge_prompt(goal: GoalState, messages: list[Any]) -> str:
    transcript = _format_transcript(messages)
    return (
        "You are a goal evaluator. Given the completion condition and conversation transcript, "
        "determine whether the condition has been met.\n\n"
        "Rules:\n"
        "- Do not call tools. Judge only from information already present in the transcript.\n"
        "- If evidence is insufficient, return satisfied=false.\n"
        "- Respond only as JSON with keys: satisfied (boolean), reason (string).\n\n"
        f"Completion condition:\n{goal.condition}\n\n"
        f"Conversation transcript:\n{transcript}\n"
    )


def _format_transcript(messages: list[Any], max_chars: int = 30_000) -> str:
    parts: list[str] = []
    total = 0
    for message in reversed(messages):
        if isinstance(message, dict):
            role = message.get("role", "unknown")
            content = message.get("content", "")
        else:
            role = getattr(message, "role", "unknown")
            content = getattr(message, "content", "")
        line = f"[{role}] {str(content)[:500]}"
        if total + len(line) > max_chars:
            break
        parts.append(line)
        total += len(line)
    return "\n".join(reversed(parts))


def _parse_judge_response(text: str) -> JudgeResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return JudgeResult(satisfied=False, reason=f"Judge returned non-JSON response: {text[:500]}")

    satisfied = bool(data.get("satisfied", False))
    reason = str(data.get("reason", "")) or ("Goal satisfied." if satisfied else "Goal not satisfied.")
    return JudgeResult(satisfied=satisfied, reason=reason)

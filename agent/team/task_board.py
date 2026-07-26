"""Dependency-aware shared task board with lease-based claiming."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import threading

from agent.team.lease_manager import Lease, LeaseManager


class BoardTaskState(str, Enum):
    PROPOSED = "proposed"
    READY = "ready"
    CLAIMED = "claimed"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


@dataclass(frozen=True)
class BoardTask:
    id: str
    goal: str
    dependencies: tuple[str, ...] = ()
    state: BoardTaskState = BoardTaskState.PROPOSED
    assignee_id: str = ""
    lease_token: str = ""
    result_summary: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.goal.strip():
            raise ValueError("task id and goal are required")
        object.__setattr__(self, "state", BoardTaskState(self.state))
        if self.id in self.dependencies:
            raise ValueError("a board task cannot depend on itself")


class TaskBoard:
    def __init__(
        self,
        lease_manager: LeaseManager,
        *,
        lease_ttl_seconds: float = 120.0,
        max_tasks: int = 32,
    ) -> None:
        self._leases = lease_manager
        self._lease_ttl = lease_ttl_seconds
        self._max_tasks = max_tasks
        self._tasks: dict[str, BoardTask] = {}
        self._lock = threading.RLock()

    def add(self, task: BoardTask) -> BoardTask:
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"task {task.id!r} already exists")
            if len(self._tasks) >= self._max_tasks:
                raise OverflowError("team task board is full")
            unknown = set(task.dependencies) - self._tasks.keys()
            if unknown:
                raise ValueError("task dependencies must be added first")
            state = BoardTaskState.READY if not task.dependencies else BoardTaskState.PROPOSED
            stored = replace(task, state=state)
            self._tasks[task.id] = stored
            return stored

    def get(self, task_id: str) -> BoardTask:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                raise KeyError(f"unknown team task {task_id!r}") from exc

    def list(self) -> tuple[BoardTask, ...]:
        with self._lock:
            self._refresh_ready()
            return tuple(self._tasks.values())

    def claim(self, task_id: str, member_id: str) -> tuple[BoardTask, Lease] | None:
        with self._lock:
            self._refresh_ready()
            task = self.get(task_id)
            if task.state is not BoardTaskState.READY:
                return None
            lease = self._leases.acquire(task_id, member_id, self._lease_ttl)
            if lease is None:
                return None
            claimed = replace(
                task,
                state=BoardTaskState.CLAIMED,
                assignee_id=member_id,
                lease_token=lease.token,
            )
            self._tasks[task_id] = claimed
            return claimed, lease

    def complete(self, task_id: str, member_id: str, lease_token: str, summary: str) -> BoardTask:
        return self._finish(
            task_id, member_id, lease_token, BoardTaskState.COMPLETED, summary
        )

    def fail(self, task_id: str, member_id: str, lease_token: str, summary: str) -> BoardTask:
        return self._finish(
            task_id, member_id, lease_token, BoardTaskState.FAILED, summary
        )

    def await_review(
        self, task_id: str, member_id: str, lease_token: str, summary: str,
    ) -> BoardTask:
        return self._finish(
            task_id,
            member_id,
            lease_token,
            BoardTaskState.AWAITING_REVIEW,
            summary,
        )

    def resolve_review(
        self, task_id: str, *, accepted: bool, summary: str,
    ) -> BoardTask:
        with self._lock:
            task = self.get(task_id)
            if task.state is not BoardTaskState.AWAITING_REVIEW:
                raise RuntimeError("task is not awaiting lead review")
            resolved = replace(
                task,
                state=(
                    BoardTaskState.COMPLETED
                    if accepted else BoardTaskState.FAILED
                ),
                result_summary=summary.strip() or task.result_summary,
            )
            self._tasks[task_id] = resolved
            self._refresh_ready()
            return resolved

    def _finish(
        self,
        task_id: str,
        member_id: str,
        lease_token: str,
        state: BoardTaskState,
        summary: str,
    ) -> BoardTask:
        with self._lock:
            task = self.get(task_id)
            lease = self._leases.get(task_id)
            if (
                task.state is not BoardTaskState.CLAIMED
                or lease is None
                or lease.owner_id != member_id
                or lease.token != lease_token
            ):
                raise PermissionError("a valid task lease is required")
            finished = replace(task, state=state, result_summary=summary.strip())
            self._tasks[task_id] = finished
            self._leases.release(lease_token)
            self._refresh_ready()
            return finished

    def _refresh_ready(self) -> None:
        for task_id, task in tuple(self._tasks.items()):
            if (
                task.state is BoardTaskState.CLAIMED
                and self._leases.get(task_id) is None
            ):
                self._tasks[task_id] = replace(
                    task,
                    state=BoardTaskState.READY,
                    assignee_id="",
                    lease_token="",
                )
        completed = {
            task_id
            for task_id, task in self._tasks.items()
            if task.state is BoardTaskState.COMPLETED
        }
        for task_id, task in tuple(self._tasks.items()):
            if task.state is BoardTaskState.PROPOSED and set(task.dependencies) <= completed:
                self._tasks[task_id] = replace(task, state=BoardTaskState.READY)

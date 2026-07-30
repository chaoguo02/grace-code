from __future__ import annotations

import threading

import pytest

from agent.session.execution_budget import (
    BudgetExhausted,
    ExecutionBudget,
    ExecutionBudgetConfig,
)


def _budget(limit: int = 10_000) -> ExecutionBudget:
    budget = ExecutionBudget(ExecutionBudgetConfig(
        token_limit=limit,
        step_limit=10,
    ))
    budget.start()
    return budget


def test_child_reservation_reduces_parent_remaining_and_settles_actual() -> None:
    budget = _budget()
    lease = budget.reserve_tokens(6_000)

    assert budget.token_remaining == 4_000
    assert budget.token_reserved == 6_000

    lease.settle(2_500)

    assert budget.token_used == 2_500
    assert budget.token_reserved == 0
    assert budget.token_remaining == 7_500
    assert budget.get_usage_report()["subagent_tokens"] == 2_500


def test_child_reservation_is_idempotent_and_cannot_oversubscribe() -> None:
    budget = _budget()
    lease = budget.reserve_tokens(8_000)

    with pytest.raises(BudgetExhausted):
        budget.reserve_tokens(2_001)

    lease.release()
    lease.settle(7_000)

    assert budget.token_used == 0
    assert budget.token_remaining == 10_000


def test_parallel_child_reservations_are_atomic() -> None:
    budget = _budget()
    barrier = threading.Barrier(8)
    granted = []
    lock = threading.Lock()

    def reserve() -> None:
        barrier.wait()
        try:
            lease = budget.reserve_tokens(2_000)
        except BudgetExhausted:
            return
        with lock:
            granted.append(lease)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert len(granted) == 5
    assert budget.token_remaining == 0
    for lease in granted:
        lease.release()
    assert budget.token_remaining == 10_000

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from config.schema import (
    ResourceGovernanceConfig,
    ResourceGovernanceQueueConfig,
    ResourceGovernanceWorkerConfig,
)
from core.resource_governor import (
    AdmissionOutcome,
    ResourceGovernor,
    ResourceKind,
    ResourceRequest,
)
from server.services.event_bus import SessionSubscriber


def _governor(*, workers: int = 1, timeout: float = 1.0) -> ResourceGovernor:
    return ResourceGovernor(ResourceGovernanceConfig(
        mode="enforce",
        worker=ResourceGovernanceWorkerConfig(
            global_max=workers,
            per_root_max=workers,
        ),
        queue=ResourceGovernanceQueueConfig(
            max_size=64,
            timeout_seconds=timeout,
        ),
    ))


def test_waiter_is_granted_after_renewable_slot_release() -> None:
    governor = _governor()
    first = governor.admit(ResourceRequest(
        "first", "root", "first",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    holder: list = []

    def wait_for_capacity() -> None:
        holder.append(governor.admit_wait(ResourceRequest(
            "second", "root", "second",
            resources={ResourceKind.WORKER_SLOT: 1},
            timeout_s=1,
        )))

    waiter = threading.Thread(target=wait_for_capacity)
    waiter.start()
    for _ in range(100):
        if governor.queue_depth == 1:
            break
        threading.Event().wait(0.005)
    assert governor.queue_depth == 1
    first.lease.release(actual_used=50_000)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert holder[0].outcome is AdmissionOutcome.GRANTED
    holder[0].lease.release()
    worker = governor.snapshot().snapshots[ResourceKind.WORKER_SLOT]
    assert worker.reserved == 0
    assert worker.consumed == 0
    assert worker.available == 1


def test_composite_lease_reconciles_tokens_without_consuming_worker() -> None:
    governor = _governor()
    result = governor.admit(ResourceRequest(
        "composite", "root", "child",
        resources={
            ResourceKind.WORKER_SLOT: 1,
            ResourceKind.TOKEN_BUDGET: 10_000,
        },
    ))
    result.lease.release(actual_used={
        ResourceKind.TOKEN_BUDGET: 7_500,
    })
    snapshot = governor.snapshot().snapshots
    assert snapshot[ResourceKind.WORKER_SLOT].available == 1
    assert snapshot[ResourceKind.WORKER_SLOT].consumed == 0
    assert snapshot[ResourceKind.TOKEN_BUDGET].reserved == 0
    assert snapshot[ResourceKind.TOKEN_BUDGET].consumed == 7_500


def test_atomic_admission_never_oversubscribes_worker_limit() -> None:
    governor = _governor()
    barrier = threading.Barrier(16)
    results = []
    lock = threading.Lock()

    def admit(index: int) -> None:
        barrier.wait()
        result = governor.admit(ResourceRequest(
            f"request-{index}", f"root-{index}", f"session-{index}",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        with lock:
            results.append(result)

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert sum(
        result.outcome is AdmissionOutcome.GRANTED for result in results
    ) == 1
    assert governor.snapshot().snapshots[ResourceKind.WORKER_SLOT].reserved == 1
    for result in results:
        if result.outcome is AdmissionOutcome.QUEUED:
            governor.cancel_request(
                next(
                    request_id for request_id, entry
                    in governor._queued_by_id.items()
                    if entry.result is result
                )
            )
        elif result.lease is not None:
            result.lease.release()


def test_capacity_timeout_removes_queue_entry() -> None:
    governor = _governor(timeout=0.05)
    first = governor.admit(ResourceRequest(
        "first", "root", "first",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    timed_out = governor.admit_wait(ResourceRequest(
        "timeout", "root", "timeout",
        resources={ResourceKind.WORKER_SLOT: 1},
        timeout_s=0.05,
    ))
    assert timed_out.outcome is AdmissionOutcome.CAPACITY_TIMEOUT
    assert governor.queue_depth == 0
    first.lease.release()


def test_queue_rotates_grants_across_roots_without_breaking_root_fifo() -> None:
    governor = ResourceGovernor(ResourceGovernanceConfig(
        mode="enforce",
        worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        queue=ResourceGovernanceQueueConfig(max_size=8, timeout_seconds=1),
    ))
    active = governor.admit(ResourceRequest(
        "active-a", "root-a", "active-a",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    a_first = governor.admit(ResourceRequest(
        "queued-a-1", "root-a", "queued-a-1",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    a_second = governor.admit(ResourceRequest(
        "queued-a-2", "root-a", "queued-a-2",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    b_first = governor.admit(ResourceRequest(
        "queued-b-1", "root-b", "queued-b-1",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))

    active.lease.release()
    assert b_first.outcome is AdmissionOutcome.GRANTED
    assert a_first.outcome is AdmissionOutcome.QUEUED
    b_first.lease.release()
    assert a_first.outcome is AdmissionOutcome.GRANTED
    assert a_second.outcome is AdmissionOutcome.QUEUED
    a_first.lease.release()
    assert a_second.outcome is AdmissionOutcome.GRANTED
    a_second.lease.release()


def test_pressure_change_is_emitted_with_stable_request_identity() -> None:
    governor = _governor(workers=2)
    events: list[dict] = []
    governor.on_event(events.append)
    first = governor.admit(ResourceRequest(
        "pressure-1", "root", "session", run_id="run", task_id="task",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))
    second = governor.admit(ResourceRequest(
        "pressure-2", "root", "session", run_id="run", task_id="task",
        resources={ResourceKind.WORKER_SLOT: 1},
    ))

    pressure = [
        event for event in events
        if event["type"] == "resource_pressure_changed"
    ]
    assert pressure
    assert pressure[-1]["root_session_id"] == "root"
    assert pressure[-1]["session_id"] == "session"
    assert pressure[-1]["run_id"] == "run"
    assert pressure[-1]["task_id"] == "task"
    assert pressure[-1]["resource_kind"] == ResourceKind.WORKER_SLOT.value
    assert pressure[-1]["pressure"] == "critical"
    second.lease.release()
    first.lease.release()


def test_budget_accounting_events_do_not_create_second_token_authority() -> None:
    governor = _governor()
    events: list[dict] = []
    governor.on_event(events.append)
    request = ResourceRequest(
        "budget-facts", "root", "session", run_id="run", task_id="task",
    )
    governor.publish_accounting_event(
        "granted",
        request,
        {ResourceKind.TOKEN_BUDGET: 10_000},
    )
    governor.publish_accounting_event(
        "reconciled",
        request,
        {ResourceKind.TOKEN_BUDGET: 10_000},
        actual={ResourceKind.TOKEN_BUDGET: 7_500},
    )

    assert [event["type"] for event in events] == [
        "delegation_resource_granted",
        "delegation_resource_reconciled",
    ]
    token = governor.snapshot().snapshots[ResourceKind.TOKEN_BUDGET]
    assert token.reserved == 0
    assert token.consumed == 0


def test_hundred_concurrent_requests_never_exceed_worker_limit() -> None:
    governor = ResourceGovernor(ResourceGovernanceConfig(
        mode="enforce",
        worker=ResourceGovernanceWorkerConfig(global_max=2, per_root_max=2),
        queue=ResourceGovernanceQueueConfig(max_size=128, timeout_seconds=5),
    ))
    active = 0
    peak = 0
    lock = threading.Lock()

    def run(index: int) -> None:
        nonlocal active, peak
        result = governor.admit_wait(ResourceRequest(
            f"stress-{index}", f"root-{index % 5}", f"session-{index}",
            resources={ResourceKind.WORKER_SLOT: 1},
            timeout_s=5,
        ))
        assert result.outcome is AdmissionOutcome.GRANTED
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.001)
        with lock:
            active -= 1
        result.lease.release()

    with ThreadPoolExecutor(max_workers=100) as pool:
        list(pool.map(run, range(100)))

    assert peak <= 2
    worker = governor.snapshot().snapshots[ResourceKind.WORKER_SLOT]
    assert worker.reserved == 0
    assert governor.queue_depth == 0


@pytest.mark.asyncio
async def test_event_subscriber_delivers_each_delta_without_stale_merge() -> None:
    loop = asyncio.get_running_loop()
    subscriber = SessionSubscriber("session", loop, queue_max_size=8)
    subscriber.publish({
        "type": "assistant_text_delta",
        "block_id": "answer",
        "text": "first",
    })
    subscriber.publish({
        "type": "assistant_text_delta",
        "block_id": "answer",
        "text": " second",
    })
    await asyncio.sleep(0)
    first = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
    second = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
    assert first["text"] == "first"
    assert second["text"] == " second"

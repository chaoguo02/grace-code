"""G34: Workspace/Evidence — lease lifecycle, conflict detection, immutable evidence.

AC: Write lease conflict → acquire returns None (serialized)
AC: Read leases can share (multiple readers)
AC: Write blocks read + read blocks write upgrade
AC: Lease expiry → stale leases cleaned, new lease can acquire
AC: release_all() cleans up all leases for a terminated run
AC: Evidence snapshot is immutable (independent copies)
AC: EvidenceCollector records tools, workspace facts, files, hook blocks, errors
AC: WorkspaceFact is frozen value object
"""

from __future__ import annotations

import time as _time

import pytest

from application.workspaces.workspace_lease_service import (
    WorkspaceLeaseService, WorkspaceLease, LeaseMode,
)
from application.evidence.evidence_collector import EvidenceCollector, WorkspaceFact
from runtime_core.outcome import RunEvidence


# ═══════════════════════════════════════════════════════════════════════════════
# G34.1 — WorkspaceLease: acquire/release/conflict
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceLeaseAcquire:
    """G34: Lease acquire with conflict detection."""

    def test_write_conflict_blocks_second_write(self):
        svc = WorkspaceLeaseService()
        assert svc.acquire("file.txt", "r1", "write") is not None
        assert svc.acquire("file.txt", "r2", "write") is None, (
            "G34: second write on same key must return None (conflict)"
        )

    def test_write_blocks_read(self):
        svc = WorkspaceLeaseService()
        svc.acquire("file.txt", "r1", "write")
        assert svc.acquire("file.txt", "r2", "read") is None, (
            "G34: write lease must block readers"
        )

    def test_read_blocks_write_upgrade(self):
        svc = WorkspaceLeaseService()
        svc.acquire("file.txt", "r1", "read")
        assert svc.acquire("file.txt", "r2", "write") is None, (
            "G34: cannot upgrade to write while readers hold lease"
        )

    def test_read_shared_multiple(self):
        svc = WorkspaceLeaseService()
        assert svc.acquire("file.txt", "r1", "read") is not None
        assert svc.acquire("file.txt", "r2", "read") is not None
        assert svc.acquire("file.txt", "r3", "read") is not None
        assert svc.active_lease_count == 3  # all three share

    def test_different_keys_no_conflict(self):
        svc = WorkspaceLeaseService()
        assert svc.acquire("a.txt", "r1", "write") is not None
        assert svc.acquire("b.txt", "r2", "write") is not None
        assert svc.active_lease_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# G34.2 — WorkspaceLease: release / expiry
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceLeaseRelease:
    """G34: Lease release and expiry."""

    def test_release_specific_key(self):
        svc = WorkspaceLeaseService()
        svc.acquire("a.txt", "r1", "write")
        svc.acquire("b.txt", "r1", "read")

        assert svc.release("a.txt", "r1") is True
        assert svc.release("a.txt", "r1") is False  # already released
        assert svc.active_lease_count == 1  # only b.txt remains

    def test_release_all_for_run(self):
        svc = WorkspaceLeaseService()
        svc.acquire("a.txt", "r1", "write")
        svc.acquire("b.txt", "r1", "read")
        svc.acquire("c.txt", "r2", "read")

        count = svc.release_all("r1")
        assert count == 2, f"release_all('r1') should release 2, got {count}"
        assert svc.active_lease_count == 1  # only r2's lease remains

    def test_release_wrong_owner_fails(self):
        svc = WorkspaceLeaseService()
        svc.acquire("file.txt", "r1", "write")
        assert svc.release("file.txt", "r2") is False, (
            "G34: cannot release lease owned by different run"
        )

    def test_lease_expiry_allows_takeover(self):
        svc = WorkspaceLeaseService()
        # Acquire with very short lease
        svc.acquire("file.txt", "r1", "write", lease_s=0.01)
        _time.sleep(0.05)  # let it expire

        # Expired lease should allow takeover
        assert svc.acquire("file.txt", "r2", "write") is not None, (
            "G34: expired lease must allow takeover"
        )

    def test_expire_stale_cleans_up(self):
        svc = WorkspaceLeaseService()
        svc.acquire("a.txt", "r1", "read", lease_s=0.01)
        svc.acquire("b.txt", "r1", "read", lease_s=300.0)
        _time.sleep(0.05)

        count = svc.expire_stale()
        assert count == 1  # only a.txt expired
        assert svc.active_lease_count == 1  # b.txt still active

    def test_check_conflict_helper(self):
        svc = WorkspaceLeaseService()
        assert not svc.check_conflict("file.txt", "write")

        svc.acquire("file.txt", "r1", "write")
        assert svc.check_conflict("file.txt", "write")
        assert svc.check_conflict("file.txt", "read")


# ═══════════════════════════════════════════════════════════════════════════════
# G34.3 — WorkspaceLease: list / query
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceLeaseQuery:
    """G34: Lease query methods."""

    def test_list_for_run(self):
        svc = WorkspaceLeaseService()
        svc.acquire("a.txt", "r1", "read")
        svc.acquire("b.txt", "r1", "write")
        svc.acquire("c.txt", "r2", "read")

        r1_leases = svc.list_for_run("r1")
        assert len(r1_leases) == 2

    def test_acquire_many(self):
        svc = WorkspaceLeaseService()
        leases = svc.acquire_many(["a.txt", "b.txt", "c.txt"], "r1", "read")
        assert len(leases) == 3
        assert svc.active_lease_count == 3

    def test_workspace_lease_is_frozen(self):
        lease = WorkspaceLease(key="f", owner_run_id="r1", mode=LeaseMode.WRITE)
        with pytest.raises(Exception):
            lease.key = "other"  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# G34.4 — EvidenceCollector
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvidenceCollector:
    """G34: Evidence collector — tools, workspace facts, immutable snapshots."""

    def test_collect_tools_and_snapshot(self):
        ec = EvidenceCollector()
        ec.record_tool("read", True, duration_ms=5.0)
        ec.record_tool("write", True, duration_ms=12.0)
        ec.record_tool("test", False, duration_ms=300.0)

        snap = ec.snapshot()
        assert isinstance(snap, RunEvidence)
        assert len(snap.tool_calls) == 3
        assert snap.tool_calls[0].tool_name == "read"
        assert snap.tool_calls[2].success is False

    def test_workspace_facts_collection(self):
        ec = EvidenceCollector()
        ec.record_workspace_fact("src/main.py", "modified", 100, 150)
        ec.record_workspace_fact("src/new.py", "created", 0, 80)

        facts = ec.workspace_snapshot()
        assert len(facts) == 2
        assert facts[0].path == "src/main.py"
        assert facts[0].action == "modified"
        assert isinstance(facts[0], WorkspaceFact)

    def test_files_touched_deduplicated(self):
        ec = EvidenceCollector()
        ec.record_file_touched("a.txt")
        ec.record_file_touched("a.txt")  # duplicate
        ec.record_file_touched("b.txt")

        snap = ec.snapshot()
        assert len(snap.files_touched) == 2  # deduplicated

    def test_hook_blocks_recorded(self):
        ec = EvidenceCollector()
        ec.record_hook_block("security_check")
        ec.record_hook_block("rate_limit")

        snap = ec.snapshot()
        assert len(snap.hook_blocks) == 2

    def test_errors_recorded(self):
        ec = EvidenceCollector()
        ec.record_error("tool timeout")
        ec.record_error("partial failure")

        assert ec.error_count == 2
        assert ec.has_errors

    def test_snapshot_is_independent_copy(self):
        """G34: Each snapshot() returns a new independent value object."""
        ec = EvidenceCollector()
        ec.record_tool("read", True)

        snap1 = ec.snapshot()
        ec.record_tool("write", True)
        snap2 = ec.snapshot()

        # snap1 unchanged despite subsequent recordings
        assert len(snap1.tool_calls) == 1
        assert len(snap2.tool_calls) == 2
        # snap1 and snap2 are independent tuples
        assert snap1.tool_calls != snap2.tool_calls

    def test_counts_accurate(self):
        ec = EvidenceCollector()
        assert ec.tool_count == 0
        assert ec.files_touched_count == 0

        ec.record_tool("read", True)
        ec.record_file_touched("f.txt")

        assert ec.tool_count == 1
        assert ec.files_touched_count == 1

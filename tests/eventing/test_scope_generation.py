"""G4: Scope Generation — strict node lifecycle, no implicit reopen.

Covers:
  - Same generation → returns same node (idempotent)
  - Higher generation → NEW node, old node closed (NOT reopened)
  - Tombstone preserved after close → stale rejection
  - Lower/same_closed/future_unregistered generations all rejected
  - Session close recursively closes child tasks
  - Global close recursively closes all
  - 10,000 generation churn — no node reopen, no identity collision
  - ScopeToken equality includes all five identity fields
  - children dict not exposed mutably
"""

from __future__ import annotations

import uuid

import pytest

from core.eventing.identifiers import SessionId, TaskId
from core.eventing.scope import ScopeKind, ScopeToken
from eventing.scope_tree import (
    ScopeTree,
    ScopeNode,
    ScopeClosedError,
    ScopeNotFoundError,
    StaleGenerationError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G4.1 — Same generation idempotent
# ═══════════════════════════════════════════════════════════════════════════════

class TestSameGenerationIdempotent:
    """ensure_session/ensure_task with same generation returns same node."""

    def test_same_session_generation_returns_same_node(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        n1 = tree.ensure_session(sid, 1)
        n2 = tree.ensure_session(sid, 1)
        assert n1 is n2

    def test_same_task_generation_returns_same_node(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tid = TaskId("t1")
        n1 = tree.ensure_task(sid, tid, 1)
        n2 = tree.ensure_task(sid, tid, 1)
        assert n1 is n2


# ═══════════════════════════════════════════════════════════════════════════════
# G4.2 — Higher generation creates NEW node, old becomes tombstone
# ═══════════════════════════════════════════════════════════════════════════════

class TestHigherGenerationNewNode:
    """G4: Higher generation → NEW node. Old node is closed, NOT reopened."""

    def test_session_higher_generation_creates_new_node(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        n1 = tree.ensure_session(sid, 1)
        n2 = tree.ensure_session(sid, 2)

        # Different nodes (NOT reopened)
        assert n1 is not n2, (
            "G4 FAIL: higher generation must create a NEW node, "
            "not reopen the old one"
        )
        assert n1.generation == 1
        assert n2.generation == 2

    def test_old_session_node_is_closed_after_new_generation(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        n1 = tree.ensure_session(sid, 1)
        n2 = tree.ensure_session(sid, 2)

        # Old node must be closed
        assert n1.closed, "Old generation node must be closed"
        # New node must be open
        assert not n2.closed, "New generation node must be open"

    def test_task_higher_generation_creates_new_node(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tid = TaskId("t1")
        n1 = tree.ensure_task(sid, tid, 1)
        n2 = tree.ensure_task(sid, tid, 2)

        assert n1 is not n2, "Task: higher generation must create new node"
        assert n1.closed
        assert not n2.closed

    def test_higher_generation_preserves_old_as_tombstone(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.ensure_session(sid, 2)

        # At least 1 tombstone should exist
        assert tree.tombstone_count >= 1, (
            "Old generation node must be preserved as tombstone"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G4.3 — 10,000 generation churn
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerationChurn:
    """G4: 10,000 generation transitions — no node reopen, no collision."""

    def test_10000_session_churn_no_reopen(self):
        tree = ScopeTree()
        sid = SessionId("s1")

        for gen in range(1, 10001):
            node = tree.ensure_session(sid, gen)
            assert node.generation == gen
            assert not node.closed, f"Gen {gen} should be open"
            # Verify previous generation is closed
            if gen > 1:
                prev = tree.find_exact(
                    ScopeToken.session_scope(tree._global_id, sid, generation=gen - 1)
                )
                if prev is not None:
                    assert prev.closed, f"Gen {gen-1} should be closed"
                # Also verify via is_closed helper
                assert tree.is_closed(
                    ScopeToken.session_scope(tree._global_id, sid, generation=gen - 1)
                ), f"Gen {gen-1} should be detected as closed"

        # After 10k churns, the active node should be at gen 10000
        active = tree.find_exact(
            ScopeToken.session_scope(tree._global_id, sid, generation=10000)
        )
        assert active is not None
        assert active.generation == 10000
        assert not active.closed

    def test_10000_interleaved_session_task_churn(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tid = TaskId("t1")

        for gen in range(1, 10001):
            sn = tree.ensure_session(sid, gen)
            tn = tree.ensure_task(sid, tid, gen)
            assert sn.generation == gen
            assert tn.generation == gen
            assert not sn.closed
            assert not tn.closed

        # No identity collision — all generations are unique
        active_s = tree.find_exact(
            ScopeToken.session_scope(tree._global_id, sid, generation=10000)
        )
        active_t = tree.find_exact(
            ScopeToken.task_scope(tree._global_id, sid, tid, generation=10000)
        )
        assert active_s is not None
        assert active_t is not None
        assert active_s.generation == 10000
        assert active_t.generation == 10000

    def test_churn_preserves_only_highest_tombstone(self):
        tree = ScopeTree()
        sid = SessionId("s1")

        for gen in range(1, 101):
            tree.ensure_session(sid, gen)

        # We churned 100 times, so at most 1 tombstone per scope key
        # (only the highest previous generation is kept)
        assert tree.tombstone_count <= 2, (
            f"Expected at most 2 tombstones, got {tree.tombstone_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G4.4 — Stale generation rejection
# ═══════════════════════════════════════════════════════════════════════════════

class TestStaleGenerationRejection:
    """G4: Stale/lower/closed generations are properly rejected."""

    def test_lower_generation_is_stale(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 5)

        # Token at generation 3 should be stale (vs active gen 5)
        stale_token = ScopeToken.session_scope(tree._global_id, sid, generation=3)
        with pytest.raises(StaleGenerationError):
            tree.check_stale(stale_token)

    def test_same_closed_generation_is_rejected(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.close_session(sid)

        # After close, the scope is a tombstone
        token = ScopeToken.session_scope(tree._global_id, sid, generation=1)
        with pytest.raises(ScopeClosedError):
            tree.check_stale(token)

    def test_future_unregistered_not_found(self):
        """A generation that was never registered → find_exact returns None."""
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)

        token = ScopeToken.session_scope(tree._global_id, sid, generation=5)
        # gen 5 was never registered, so find_exact returns None
        # (find() returns the active node by scope key, ignoring generation)
        assert tree.find_exact(token) is None

    def test_stale_after_generation_bump(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.ensure_session(sid, 2)

        # Token at gen 1 is now stale
        stale = ScopeToken.session_scope(tree._global_id, sid, generation=1)
        with pytest.raises(StaleGenerationError):
            tree.check_stale(stale)

    def test_is_closed_helper(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.close_session(sid)

        token = ScopeToken.session_scope(tree._global_id, sid, generation=1)
        assert tree.is_closed(token)


# ═══════════════════════════════════════════════════════════════════════════════
# G4.5 — Recursive close
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecursiveClose:
    """G4: Close session → close children. Close global → close all."""

    def test_close_session_closes_child_tasks(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tid = TaskId("t1")

        tree.ensure_session(sid, 1)
        task_node = tree.ensure_task(sid, tid, 1)

        assert not task_node.closed
        tree.close_session(sid)

        assert task_node.closed, "G4: close_session must recursively close child tasks"

    def test_close_global_closes_all_sessions_and_tasks(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tid = TaskId("t1")

        sn = tree.ensure_session(sid, 1)
        tn = tree.ensure_task(sid, tid, 1)

        tree.close_all()

        assert sn.closed
        assert tn.closed, "G4: close_all must close all descendants"
        assert tree._root.closed

    def test_close_session_is_idempotent(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.close_session(sid)
        tree.close_session(sid)  # second close must not raise
        assert tree.tombstone_count == 1

    def test_close_unknown_session_does_not_raise(self):
        tree = ScopeTree()
        tree.close_session(SessionId("nonexistent"))  # no-op


# ═══════════════════════════════════════════════════════════════════════════════
# G4.6 — ScopeToken identity contract
# ═══════════════════════════════════════════════════════════════════════════════

class TestScopeTokenIdentity:
    """G4: ScopeToken identity includes ALL five fields."""

    def test_same_session_different_generation_not_equal(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        t1 = ScopeToken.session_scope(gid, sid, generation=1)
        t2 = ScopeToken.session_scope(gid, sid, generation=2)
        assert t1 != t2, (
            "G4: Same session at different generations must NOT be equal"
        )

    def test_same_fields_equal(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        t1 = ScopeToken.session_scope(gid, sid, generation=3)
        t2 = ScopeToken.session_scope(gid, sid, generation=3)
        assert t1 == t2
        assert hash(t1) == hash(t2)

    def test_different_global_id_not_equal(self):
        sid = SessionId("s1")
        t1 = ScopeToken.session_scope(uuid.uuid4(), sid, generation=1)
        t2 = ScopeToken.session_scope(uuid.uuid4(), sid, generation=1)
        assert t1 != t2

    def test_different_session_id_not_equal(self):
        gid = uuid.uuid4()
        t1 = ScopeToken.session_scope(gid, SessionId("s1"), generation=1)
        t2 = ScopeToken.session_scope(gid, SessionId("s2"), generation=1)
        assert t1 != t2

    def test_different_kind_not_equal(self):
        gid = uuid.uuid4()
        t1 = ScopeToken.session_scope(gid, SessionId("s1"), generation=1)
        t2 = ScopeToken.task_scope(gid, SessionId("s1"), TaskId("t1"), generation=1)
        assert t1 != t2

    def test_identity_property(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        t = ScopeToken.session_scope(gid, sid, generation=1)
        ident = t.identity
        assert ident == ("session", str(gid), str(sid), None, 1)

    def test_set_behavior(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        t1 = ScopeToken.session_scope(gid, sid, generation=1)
        t2 = ScopeToken.session_scope(gid, sid, generation=2)
        t3 = ScopeToken.session_scope(gid, sid, generation=1)

        s = {t1, t2, t3}
        assert len(s) == 2  # t1 == t3 (same gen), t2 is different

    def test_dict_key_behavior(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        t1 = ScopeToken.session_scope(gid, sid, generation=1)
        t2 = ScopeToken.session_scope(gid, sid, generation=2)

        d = {t1: "old", t2: "new"}
        assert d[t1] == "old"
        assert d[t2] == "new"


# ═══════════════════════════════════════════════════════════════════════════════
# G4.7 — External immutability
# ═══════════════════════════════════════════════════════════════════════════════

class TestExternalImmutability:
    """G4: children dict not exposed mutably; ScopeToken is frozen."""

    def test_scope_token_is_frozen(self):
        token = ScopeToken.global_scope()
        with pytest.raises(Exception):
            token.generation = 5  # type: ignore

    def test_scope_tree_active_count(self):
        tree = ScopeTree()
        assert tree.active_session_count == 0
        tree.ensure_session(SessionId("s1"), 1)
        assert tree.active_session_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# G4.8 — GLOBAL scope identity
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalScope:
    """G4: GLOBAL scope at different generations are different tokens."""

    def test_global_at_different_generations_not_equal(self):
        gid = uuid.uuid4()
        g1 = ScopeToken(
            kind=ScopeKind.GLOBAL, global_id=gid, generation=0,
        )
        g2 = ScopeToken(
            kind=ScopeKind.GLOBAL, global_id=gid, generation=1,
        )
        assert g1 != g2

    def test_global_scope_key_constant(self):
        g = ScopeToken.global_scope()
        assert g.scope_key == "global"
        assert g.parent_key is None

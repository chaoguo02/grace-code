"""P1: Value objects + ScopeToken — acceptance tests.

AC: All IDs validated at construction (non-empty, non-negative).
AC: ScopeToken invariants enforced per kind.
AC: is_stale rejects old generation events.
AC: No import from server/agent/runtime/hooks.
"""

from __future__ import annotations

import uuid

import pytest

from core.eventing.identifiers import (
    SessionId, TaskId, RunId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeKind, ScopeToken


# ── ID value objects ────────────────────────────────────────────────────────

class TestIdentifiers:

    def test_session_id_rejects_empty(self):
        with pytest.raises(ValueError):
            SessionId("")
        with pytest.raises(ValueError):
            SessionId("   ")

    def test_session_id_accepts_valid(self):
        sid = SessionId("abc-123")
        assert str(sid) == "abc-123"

    def test_task_id_rejects_empty(self):
        with pytest.raises(ValueError):
            TaskId("")

    def test_run_id_rejects_empty(self):
        with pytest.raises(ValueError):
            RunId("")

    def test_event_id_generate(self):
        eid = EventId.generate()
        assert isinstance(eid.value, uuid.UUID)

    def test_aggregate_version_rejects_negative(self):
        with pytest.raises(ValueError):
            AggregateVersion(-1)

    def test_aggregate_version_next(self):
        v1 = AggregateVersion(3)
        v2 = v1.next()
        assert v2.value == 4
        assert v1.value == 3  # immutable

    def test_all_ids_are_frozen(self):
        sid = SessionId("x")
        with pytest.raises(Exception):
            sid.value = "y"  # type: ignore


# ── ScopeToken ──────────────────────────────────────────────────────────────

class TestScopeToken:

    def test_global_scope_rejects_session_id(self):
        with pytest.raises(ValueError, match="GLOBAL"):
            ScopeToken(
                kind=ScopeKind.GLOBAL,
                global_id=uuid.uuid4(),
                generation=0,
                session_id=SessionId("s1"),
            )

    def test_session_scope_requires_session_id(self):
        with pytest.raises(ValueError, match="SESSION"):
            ScopeToken(
                kind=ScopeKind.SESSION,
                global_id=uuid.uuid4(),
                generation=0,
            )

    def test_session_scope_rejects_task_id(self):
        with pytest.raises(ValueError, match="SESSION"):
            ScopeToken(
                kind=ScopeKind.SESSION,
                global_id=uuid.uuid4(),
                generation=0,
                session_id=SessionId("s1"),
                task_id=TaskId("t1"),
            )

    def test_task_scope_requires_both(self):
        with pytest.raises(ValueError, match="TASK"):
            ScopeToken(
                kind=ScopeKind.TASK,
                global_id=uuid.uuid4(),
                generation=0,
                session_id=SessionId("s1"),
            )

    def test_task_scope_valid(self):
        token = ScopeToken(
            kind=ScopeKind.TASK,
            global_id=uuid.uuid4(),
            generation=1,
            session_id=SessionId("s1"),
            task_id=TaskId("t1"),
        )
        assert token.kind == ScopeKind.TASK
        assert token.generation == 1

    def test_generation_rejects_negative(self):
        with pytest.raises(ValueError, match="generation"):
            ScopeToken(
                kind=ScopeKind.GLOBAL,
                global_id=uuid.uuid4(),
                generation=-1,
            )

    def test_is_stale(self):
        token = ScopeToken(
            kind=ScopeKind.SESSION,
            global_id=uuid.uuid4(),
            generation=3,
            session_id=SessionId("s1"),
        )
        assert token.is_stale(5) is True   # 3 < 5
        assert token.is_stale(3) is False  # 3 == 3
        assert token.is_stale(2) is False  # 3 > 2

    def test_factory_helpers(self):
        gid = uuid.uuid4()
        sid = SessionId("s1")
        tid = TaskId("t1")

        g = ScopeToken.global_scope()
        assert g.kind == ScopeKind.GLOBAL

        s = ScopeToken.session_scope(gid, sid, generation=2)
        assert s.kind == ScopeKind.SESSION
        assert s.generation == 2

        t = ScopeToken.task_scope(gid, sid, tid, generation=3)
        assert t.kind == ScopeKind.TASK
        assert str(t.session_id) == "s1"
        assert str(t.task_id) == "t1"


# ── Import boundary ─────────────────────────────────────────────────────────

class TestImportBoundary:
    """P1 constraint: no server/agent/runtime/hooks imports."""

    def test_identifiers_no_forbidden_imports(self):
        import ast, core.eventing.identifiers as mod
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in getattr(node, 'names', [])]
                forbidden = ('server', 'agent', 'runtime', 'hooks')
                if module and any(module.startswith(f) for f in forbidden):
                    pytest.fail(f"{mod.__file__} imports {module}")
                for name in names:
                    if any(name.startswith(f) for f in forbidden):
                        pytest.fail(f"{mod.__file__} imports {name}")

    def test_scope_no_forbidden_imports(self):
        import ast, core.eventing.scope as mod
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                names = [n.name for n in getattr(node, 'names', [])]
                forbidden = ('server', 'agent', 'runtime', 'hooks')
                if module and any(module.startswith(f) for f in forbidden):
                    pytest.fail(f"{mod.__file__} imports {module}")
                for name in names:
                    if any(name.startswith(f) for f in forbidden):
                        pytest.fail(f"{mod.__file__} imports {name}")

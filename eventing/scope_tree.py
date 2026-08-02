"""
G4: Scope tree — Strict generation, tombstone preservation, no node reuse.

Key rules:
  - Higher generation creates a NEW node; the old node stays as tombstone.
  - Closed nodes are NEVER reopened (no bump-generation-on-closed).
  - find() rejects: lower generation, same closed node, unknown scope.
  - Session close recursively closes child tasks.
  - Global close recursively closes all sessions and tasks.
  - children dict is never exposed mutably to external code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.eventing.identifiers import SessionId, TaskId
from core.eventing.scope import ScopeKind, ScopeToken


# ── Errors ──────────────────────────────────────────────────────────────────

class ScopeClosedError(RuntimeError):
    """Scope has been closed — no more events or children accepted."""


class ScopeNotFoundError(RuntimeError):
    """No scope matches the given token."""


class StaleGenerationError(ScopeClosedError):
    """The requested generation is stale (lower than current)."""


class FutureGenerationError(RuntimeError):
    """The requested generation has not been registered yet."""


# ── ScopeNode ───────────────────────────────────────────────────────────────

@dataclass
class ScopeNode:
    """A scope node in the tree.  Immutable token; mutable closed flag.

    Once closed, a node stays closed.  New generations create new nodes;
    old nodes are preserved as tombstones.
    """

    token: ScopeToken
    parent: ScopeNode | None = None
    children: dict[str, ScopeNode] = field(default_factory=dict)
    _closed: bool = field(default=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def generation(self) -> int:
        return self.token.generation

    def close(self) -> None:
        """Close this node and all descendant children recursively."""
        if self._closed:
            return
        self._closed = True
        for child in self.children.values():
            child.close()

    def _child_keys(self) -> tuple[str, ...]:
        """Immutable snapshot of child keys (for external read access)."""
        return tuple(self.children.keys())

    def __repr__(self) -> str:
        status = "closed" if self._closed else "open"
        return (
            f"ScopeNode({self.token.kind.value}, gen={self.generation}, "
            f"{status}, children={len(self.children)})"
        )


# ── ScopeTree ───────────────────────────────────────────────────────────────

class ScopeTree:
    """Global → Session → Task hierarchy with strict generation semantics.

    - Each (scope_key, generation) pair gets a unique node.
    - Closing a session closes all its tasks.
    - Closed nodes stay as tombstones for stale rejection.
    - The root (GLOBAL) is created once and closed when the tree is done.
    """

    def __init__(self, global_id: uuid.UUID | None = None) -> None:
        self._global_id = global_id or uuid.uuid4()
        gtoken = ScopeToken(
            kind=ScopeKind.GLOBAL,
            global_id=self._global_id,
            generation=0,
        )
        self._root = ScopeNode(token=gtoken)
        # Tombstones: old generation nodes kept for stale rejection.
        # Keyed by scope_key (e.g. "session:s1") → list of tombstone nodes.
        self._tombstones: dict[str, list[ScopeNode]] = {}

    # ── Read ──────────────────────────────────────────────────────────────

    @property
    def root(self) -> ScopeNode:
        return self._root

    def find(self, token: ScopeToken) -> ScopeNode | None:
        """Find the node matching *token* by scope key (kind + IDs).

        Does NOT verify generation match — callers must check
        node.generation vs token.generation themselves for exact routing.
        Returns None only if the scope key has never been registered.
        """
        if token.kind == ScopeKind.GLOBAL:
            return self._root
        if token.kind == ScopeKind.SESSION:
            sid = str(token.session_id) if token.session_id else ""
            return self._root.children.get(sid)
        if token.kind == ScopeKind.TASK:
            sid = str(token.session_id) if token.session_id else ""
            tid = str(token.task_id) if token.task_id else ""
            session_node = self._root.children.get(sid)
            if session_node is None:
                return None
            return session_node.children.get(tid)
        return None

    def find_exact(self, token: ScopeToken) -> ScopeNode | None:
        """Find the node matching *token* by scope key AND generation.

        Returns None if the scope key is unknown OR the generation differs.
        """
        node = self.find(token)
        if node is None:
            return None
        if node.generation != token.generation:
            return None
        return node

    def check_stale(self, token: ScopeToken) -> None:
        """Raise StaleGenerationError if *token* is from an old generation.

        Checks:
          1. Tombstones: any preserved closed node with higher generation
          2. Active node: if active node has higher generation, token is stale
          3. Active node: if same key but closed, this specific gen is closed
        """
        scope_key = token.scope_key

        # Check active node first (most common case)
        active = self.find(token)
        if active is not None:
            if active.generation > token.generation:
                raise StaleGenerationError(
                    f"Scope {scope_key} generation {token.generation} is stale "
                    f"(active generation is {active.generation})"
                )
            if active.closed:
                raise ScopeClosedError(
                    f"Scope {scope_key} generation {token.generation} is closed"
                )
            # Active at same generation and open → OK
            return

        # Check tombstones
        for tombstone in self._tombstones.get(scope_key, []):
            if tombstone.generation > token.generation:
                raise StaleGenerationError(
                    f"Scope {scope_key} generation {token.generation} is stale "
                    f"(tombstone at gen {tombstone.generation})"
                )

    # ── Mutation ───────────────────────────────────────────────────────────

    def ensure_session(
        self, session_id: SessionId, generation: int,
    ) -> ScopeNode:
        """Get or create a SESSION scope node at *generation*.

        If a node exists at a DIFFERENT generation:
          - Close existing node → move to tombstone
          - Create a NEW node (never reopen a closed node)
        """
        sid = str(session_id)
        scope_key = f"session:{sid}"
        existing = self._root.children.get(sid)

        if existing is not None:
            if existing.generation == generation:
                return existing
            # Different generation: close old, create new
            if not existing.closed:
                existing.close()
            self._add_tombstone(scope_key, existing)

        token = ScopeToken.session_scope(
            self._global_id, session_id, generation=generation,
        )
        node = ScopeNode(token=token, parent=self._root)
        self._root.children[sid] = node
        return node

    def ensure_task(
        self, session_id: SessionId, task_id: TaskId, generation: int,
    ) -> ScopeNode:
        """Get or create a TASK scope node.

        Ensures the parent session exists first (reuses ensure_session logic).
        """
        session_node = self.ensure_session(session_id, generation)
        tid = str(task_id)
        scope_key = f"task:{session_id}:{tid}"
        existing = session_node.children.get(tid)

        if existing is not None:
            if existing.generation == generation:
                return existing
            # Different generation: close old, create new
            if not existing.closed:
                existing.close()
            self._add_tombstone(scope_key, existing)

        token = ScopeToken.task_scope(
            self._global_id, session_id, task_id, generation=generation,
        )
        node = ScopeNode(token=token, parent=session_node)
        session_node.children[tid] = node
        return node

    # ── Close ──────────────────────────────────────────────────────────────

    def close_session(self, session_id: SessionId) -> None:
        """Close a session and all its child tasks.  Preserves tombstone."""
        sid = str(session_id)
        node = self._root.children.get(sid)
        if node is None or node.closed:
            return
        node.close()
        scope_key = f"session:{sid}"
        self._add_tombstone(scope_key, node)

    def close_task(self, session_id: SessionId, task_id: TaskId) -> None:
        """Close a single task.  Preserves tombstone."""
        sid = str(session_id)
        session_node = self._root.children.get(sid)
        if session_node is None:
            return
        tid = str(task_id)
        node = session_node.children.get(tid)
        if node is None or node.closed:
            return
        node.close()
        scope_key = f"task:{sid}:{tid}"
        self._add_tombstone(scope_key, node)

    def close_all(self) -> None:
        """Close root (and all descendants).  Clears children."""
        self._root.close()
        for child in list(self._root.children.values()):
            scope_key = child.token.scope_key
            self._add_tombstone(scope_key, child)
        self._root.children.clear()

    # ── Tombstone management ───────────────────────────────────────────────

    def _add_tombstone(self, scope_key: str, node: ScopeNode) -> None:
        """Archive a closed node as a tombstone for stale rejection."""
        if scope_key not in self._tombstones:
            self._tombstones[scope_key] = []
        # Keep only the highest-generation tombstone per scope key
        existing = self._tombstones[scope_key]
        for i, t in enumerate(existing):
            if t.generation == node.generation:
                return  # already recorded
            if t.generation < node.generation:
                existing[i] = node
                return
        existing.append(node)

    @property
    def tombstone_count(self) -> int:
        return sum(len(v) for v in self._tombstones.values())

    @property
    def active_session_count(self) -> int:
        return len(self._root.children)

    def is_closed(self, token: ScopeToken) -> bool:
        """Check if *token*'s exact scope generation is closed.

        Uses exact generation matching.  A scope at gen=1 is closed even
        when a newer gen=2 node is active.
        """
        # Check tombstones first (old generations preserved after bump)
        for tombstone in self._tombstones.get(token.scope_key, []):
            if tombstone.generation == token.generation:
                return True
            if tombstone.generation > token.generation:
                return True  # stale

        # Check active node by exact generation
        node = self.find_exact(token)
        if node is not None:
            return node.closed

        # Not registered at all — not closed, just unknown
        return False

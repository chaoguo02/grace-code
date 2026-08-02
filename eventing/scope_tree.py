"""
P5: Scope tree — Global → Session → Task hierarchy.

Generation tracking rejects stale events from old scopes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.eventing.identifiers import SessionId, TaskId
from core.eventing.scope import ScopeKind, ScopeToken


class ScopeClosedError(RuntimeError):
    """Scope has been closed — no more events or children accepted."""


class ScopeNotFoundError(RuntimeError):
    """No scope matches the given token."""


@dataclass
class ScopeNode:
    token: ScopeToken
    parent: ScopeNode | None = None
    children: dict[str, ScopeNode] = field(default_factory=dict)
    closed: bool = False
    generation: int = 0

    def close(self) -> None:
        self.closed = True
        for child in self.children.values():
            child.close()


class ScopeTree:
    """Global → Session → Task hierarchy.

    Routes events by exact scope match (no bubbling).
    Each scope has a generation — stale events rejected.
    """

    def __init__(self, global_id: uuid.UUID | None = None) -> None:
        self._global_id = global_id or uuid.uuid4()
        self._root = ScopeNode(
            token=ScopeToken(
                kind=ScopeKind.GLOBAL,
                global_id=self._global_id,
                generation=0,
            ),
        )

    @property
    def root(self) -> ScopeNode:
        return self._root

    def find(self, token: ScopeToken) -> ScopeNode | None:
        """Find the node matching *token*. Exact scope match."""
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

    def ensure_session(
        self, session_id: SessionId, generation: int,
    ) -> ScopeNode:
        sid = str(session_id)
        if sid in self._root.children:
            node = self._root.children[sid]
            if node.generation < generation:
                node.close()
                node.generation = generation
            return node
        token = ScopeToken.session_scope(
            self._global_id, session_id, generation=generation,
        )
        node = ScopeNode(token=token, parent=self._root, generation=generation)
        self._root.children[sid] = node
        return node

    def ensure_task(
        self, session_id: SessionId, task_id: TaskId, generation: int,
    ) -> ScopeNode:
        session_node = self.ensure_session(session_id, generation)
        tid = str(task_id)
        if tid in session_node.children:
            return session_node.children[tid]
        token = ScopeToken.task_scope(
            self._global_id, session_id, task_id, generation=generation,
        )
        node = ScopeNode(
            token=token, parent=session_node, generation=generation,
        )
        session_node.children[tid] = node
        return node

    def close_session(self, session_id: SessionId) -> None:
        sid = str(session_id)
        node = self._root.children.get(sid)
        if node is not None:
            node.close()

    def close_task(self, session_id: SessionId, task_id: TaskId) -> None:
        sid = str(session_id)
        session_node = self._root.children.get(sid)
        if session_node is None:
            return
        tid = str(task_id)
        node = session_node.children.get(tid)
        if node is not None:
            node.close()

    def close_all(self) -> None:
        self._root.close()
        self._root.children.clear()

"""
memory/context.py

MemoryContext — 管理记忆在 LLM 上下文中的注入。

记忆索引以独立的 user message 注入（不影响 system prompt 的 prompt cache），
在 compaction 后从 MemoryStore 重新读取以确保长对话不丢失长期记忆上下文。

支持相关性过滤：根据当前任务描述的关键词，优先展示相关记忆。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from memory.models import MemoryStatus, MemoryType
from memory.recall import MemoryRecallQuery, MemoryRecallService
from memory.store import MemoryStore

if TYPE_CHECKING:
    from llm.base import LLMBackend
    from memory.retriever import ProactiveRetriever

logger = logging.getLogger(__name__)


class MemoryContext:
    """
    管理记忆在 agent 上下文中的注入。

    职责：
    - 构建 Memory Section 文本（注入独立的 project context user message）
    - 每次构建时从 MemoryStore 重新读取（确保 compaction 后不丢失）
    - 按任务相关性过滤和排序记忆条目
    """

    def __init__(
        self,
        store: MemoryStore,
        max_lines: int = 50,
        enabled: bool = True,
        retriever: ProactiveRetriever | None = None,
        selector_backend: "LLMBackend | None" = None,
        recall_service: MemoryRecallService | None = None,
    ) -> None:
        self._store = store
        self._max_lines = max_lines
        self._enabled = enabled
        self._task_context: str = ""
        self._retriever = retriever
        self._selector_backend = selector_backend
        self._user_message: str = ""
        self._cached_section_by_session: dict[str, str] = {}
        self._recent_tools_by_session: dict[str, list[str]] = {}
        self._active_files_by_session: dict[str, set[str]] = {}
        self._turn_id: str = ""
        self._run_id: str = ""
        # Backward-compatible fields for deprecated selector/precision helpers.
        # Active recall uses session-scoped state above.
        self._already_surfaced: set[str] = set()
        self._recent_tools: list[str] = []
        self._session_id: str = ""
        self._agent_name: str = ""
        self._mode: str = ""
        self._repo_path: str = "."
        self._session_title: str = ""
        self._recall_service = recall_service or MemoryRecallService(store, retriever)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def store(self) -> MemoryStore:
        """访问底层 MemoryStore（用于 compaction 后重新加载）。"""
        return self._store

    def set_task_context(self, task_description: str) -> None:
        """设置当前任务描述，用于记忆相关性过滤。"""
        self._task_context = task_description
        self._invalidate_session_cache()

    def set_session_context(
        self,
        *,
        session_id: str = "",
        agent_name: str = "",
        mode: str = "",
        repo_path: str = ".",
        session_title: str = "",
    ) -> None:
        """Set session metadata used by active recall without sharing cache across sessions."""
        changed = session_id != self._session_id
        self._session_id = session_id
        self._agent_name = agent_name
        self._mode = mode
        self._repo_path = repo_path
        self._session_title = session_title
        if changed:
            self._invalidate_session_cache(session_id)

    def set_user_message(self, message: str) -> None:
        """设置当前轮用户消息，用于 RAG 主动检索。"""
        if message != self._user_message:
            self._user_message = message
            self._invalidate_session_cache()

    def add_recent_tool(self, tool_name: str) -> None:
        """Track recently used tools (for selector context hint)."""
        sid = self._session_id or f"__default__{id(self)}"
        tools = self._recent_tools_by_session.setdefault(sid, [])
        if tool_name not in tools:
            tools.append(tool_name)
            if len(tools) > 10:
                self._recent_tools_by_session[sid] = tools[-10:]

    def set_active_files(self, files: set[str]) -> None:
        """Track files accessed during the agent run (for recall scoring)."""
        sid = self._session_id or f"__default__{id(self)}"
        self._active_files_by_session[sid] = set(files)
        self._invalidate_session_cache(sid)

    def set_turn_id(self, turn_id: str) -> None:
        """Set the current turn ID for fine-grained recall tracing."""
        self._turn_id = turn_id
        self._invalidate_session_cache()

    def set_run_id(self, run_id: str) -> None:
        """Set the current run ID for memory source attribution."""
        self._run_id = run_id

    def _invalidate_session_cache(self, session_id: str | None = None) -> None:
        sid = session_id or self._session_id or f"__default__{id(self)}"
        self._cached_section_by_session.pop(sid, None)

    def invalidate_cache(self, session_id: str | None = None) -> None:
        """Public invalidation hook used by web CRUD/override endpoints."""
        if session_id:
            self._cached_section_by_session.pop(session_id, None)
        else:
            self._cached_section_by_session.clear()

    @property
    def recall_service(self) -> MemoryRecallService:
        return self._recall_service

    @property
    def run_id(self) -> str:
        return self._run_id

    def build_memory_section(self) -> str:
        """
        构建 Memory Section 文本。

        Phase 4 injection strategy (data-driven, not LLM-mediated):
        - user/feedback types: always inject full content (short rules)
        - project/reference types: scope+confidence precision injection (max 5)

        没有记忆时返回空字符串。
        """
        if not self._enabled:
            return ""

        sid = self._session_id or f"__default__{id(self)}"
        cached = self._cached_section_by_session.get(sid)
        if cached is not None:
            return cached

        parts: list[str] = []

        recall = self._recall_service.recall(MemoryRecallQuery(
            session_id=sid,
            user_message=self._user_message,
            task_description=self._task_context,
            agent_name=self._agent_name,
            mode=self._mode,
            repo_path=self._repo_path,
            session_title=self._session_title,
            active_files=tuple(sorted(self._active_files_by_session.get(sid, set()))),
            recent_tools=tuple(self._recent_tools_by_session.get(sid, [])),
            turn_id=self._turn_id,
        ))
        if recall.injection_text:
            parts.append(recall.injection_text)

        # Index listing (on-demand lookup — LLM can memory_read as needed)
        index_content = self._store.get_index_content(max_lines=self._max_lines)
        if index_content.strip():
            index_section = "\n".join([
                "## Available Memories",
                index_content,
                "",
                "Use memory_read to read a specific memory, memory_write to",
                "save new information you want to remember across sessions.",
            ])
            parts.append(index_section)

        section = "\n\n".join(parts)
        self._cached_section_by_session[sid] = section
        return section

    def _validate_anchors_stale(self, mem: Memory) -> bool:
        """Return True if any anchor's content_hash mismatches the current file.

        Code is Truth: when the anchored file has been edited since the memory
        was written, the memory is considered stale and should be deprecated.
        Anchors store repo-relative paths; resolve against repo_path.
        """
        import hashlib
        for anchor in mem.anchors:
            if not anchor.path or not anchor.content_hash:
                continue
            try:
                full_path = Path(self._repo_path) / anchor.path
                current_hash = hashlib.sha256(
                    full_path.read_bytes()
                ).hexdigest()
                if current_hash != anchor.content_hash:
                    logger.debug(
                        "Memory %s anchor %s hash mismatch — marking stale",
                        mem.name, anchor.path,
                    )
                    return True
            except (OSError, IOError):
                logger.debug(
                    "Memory %s anchor %s no longer readable — marking stale",
                    mem.name, anchor.path,
                )
                return True
        return False

    def _build_always_inject_section(self) -> str:
        """Load full content of all user/feedback memories (always injected)."""
        from memory.models import ALWAYS_INJECT_TYPES
        summaries = self._store.list_memories()
        always_mems = [s for s in summaries if s.type in ALWAYS_INJECT_TYPES]
        if not always_mems:
            return ""

        lines: list[str] = ["## Active Rules & Preferences"]
        for s in always_mems:
            try:
                mem = self._store.read_memory(s.name)
                if not mem or not mem.content.strip():
                    continue
                # P1: skip deprecated memories — Code is Truth
                if mem.metadata.status is not MemoryStatus.ACTIVE:
                    continue
                # P1: anchor content_hash validation — if the anchored file
                # has changed, the memory is stale. Auto-deprecate and skip.
                if self._validate_anchors_stale(mem):
                    try:
                        mem.metadata.status = MemoryStatus.DEPRECATED
                        self._store.write_memory(mem, source="anchor-stale-check")
                    except Exception:
                        pass
                    continue
                lines.append(f"### {s.name} ({s.type})")
                lines.append(mem.content.strip())
                lines.append("")
            except Exception:
                continue

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _build_precision_section(self) -> str:
        """Phase 4 precision injection: scope + confidence filtering.

        Replaces the old three-pipeline approach (Sonnet Selector + keyword
        scoring + RAG retrieval) with a single deterministic pipeline:
        1. list_by_scope("project", min_confidence=0.5)
        2. Sort by confidence DESC, updated_at DESC
        3. Take top-5, verify freshness (content_hash), format and inject

        Zero extra LLM calls. Zero vector scans. Deterministic and fast.
        """
        try:
            memories = self._store.list_by_scope("project", min_confidence=0.5)
        except AttributeError:
            # list_by_scope not available (old store) — fallback to empty
            return ""

        if not memories:
            return ""

        # Also include global-scoped memories (user preferences apply everywhere)
        try:
            global_mems = self._store.list_by_scope("global", min_confidence=0.5)
        except AttributeError:
            global_mems = []

        # Sort: confidence DESC, then access_count DESC, then updated_at DESC
        all_mems = memories + global_mems
        all_mems.sort(
            key=lambda m: (
                -getattr(m.metadata, "confidence", 0.5),
                -getattr(m.metadata, "access_count", 0),
            )
        )

        # Take top-5 (already sorted, but deduplicate by name)
        seen: set[str] = set()
        top: list = []
        for m in all_mems:
            if m.name in seen:
                continue
            if getattr(m.metadata, "status", MemoryStatus.ACTIVE) is not MemoryStatus.ACTIVE:
                continue
            if m.name in self._already_surfaced:
                continue
            seen.add(m.name)
            top.append(m)
            if len(top) >= 5:
                break

        if not top:
            return ""

        lines: list[str] = ["## Relevant Project Knowledge"]
        for mem in top:
            # Phase 6: cache returns Memory without content — load from file on demand
            if not mem.content.strip():
                full = self._store.read_memory(mem.name)
                if full:
                    mem = full
            freshness = self._verify_memory_freshness(mem)
            lines.append(f"### {mem.name}")
            lines.append(mem.content.strip())
            if freshness:
                lines.append(f"\n> {freshness}")
            lines.append("")
            self._already_surfaced.add(mem.name)

        return "\n".join(lines)

    def _verify_memory_freshness(self, memory) -> str:
        """Phase 4 Step 3: Content hash verification before injection.

        Checks all file anchors with content_hash. Returns a freshness
        warning string, or empty string if memory is still fresh.

        - Hash matches → confidence boost (no warning)
        - Hash mismatch → confidence *= 0.5, warning injected
        - File deleted → memory deprecated, returns "DEPRECATED" signal
        """
        import hashlib
        from pathlib import Path

        anchors = getattr(memory, "anchors", []) or []
        hash_anchors = [
            a for a in anchors
            if getattr(a, "kind", "") == "file" and getattr(a, "content_hash", "")
        ]
        if not hash_anchors:
            return ""  # No hash binding — cannot verify

        all_match = True
        for anchor in hash_anchors:
            try:
                p = Path(anchor.path)
                if not p.exists():
                    # File deleted — memory is orphaned
                    memory.metadata.status = MemoryStatus.DEPRECATED
                    try:
                        self._store.write_memory(memory)
                    except Exception:
                        pass
                    return "DEPRECATED"
                raw = p.read_bytes()
                # Normalize line endings before hashing (P2-44: git autocrlf defense)
                current_hash = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
                if current_hash != anchor.content_hash:
                    all_match = False
            except (OSError, IOError):
                continue

        if not all_match:
            # Partial mismatch — degrade confidence, don't discard
            old_conf = getattr(memory.metadata, "confidence", 0.7)
            memory.metadata.confidence = max(0.1, old_conf * 0.5)
            try:
                self._store.write_memory(memory)
            except Exception:
                pass
            return "[FILE CHANGED] Associated files have changed since this memory was created. Confidence degraded. Verify before relying on this information."

        return ""  # All hashes match — memory is fresh

    def _build_selected_section(self) -> str:
        """Select and load on-demand (project/reference) memories via Sonnet selector.

        Deprecated by Phase 4 _build_precision_section(). Kept for backward compat.
        """
        query = self._user_message or self._task_context
        if not query:
            return ""

        # Try Sonnet selector first
        if self._selector_backend:
            from memory.selector import select_memories
            selected_names = select_memories(
                query=query,
                memory_dir=self._store.store_dir,
                selector_backend=self._selector_backend,
                already_surfaced=self._already_surfaced,
                recent_tools=self._recent_tools,
            )
            if selected_names:
                return self._load_selected_memories(selected_names)

        # Fallback: no selector configured or selector returned nothing
        return ""

    # _load_selected_memories removed in Phase 4 — replaced by _build_precision_section.
    # The old Sonnet selector path had a constitution violation (import agent.session.runtime).

    def _build_rag_section(self) -> str:
        """用 ProactiveRetriever 检索相关 chunks 并格式化。

        按类型分配检索配额：
        - project: top-5（稳定项目知识）
        - reference: top-3（外部资源指引）
        - feedback 不在此注入（通过 task anchor 按文件触发）
        """
        if not self._retriever:
            return ""
        query = self._user_message or self._task_context
        if not query:
            return ""
        try:
            chunks = self._retriever.retrieve(
                user_message=query,
                task_description=self._task_context,
            )
            project_chunks: list[dict] = []
            reference_chunks: list[dict] = []
            other_chunks: list[dict] = []
            for chunk in chunks:
                mem_type = (chunk.get("metadata") or {}).get("type", "")
                if mem_type == "project" and len(project_chunks) < 5:
                    project_chunks.append(chunk)
                elif mem_type == "reference" and len(reference_chunks) < 3:
                    reference_chunks.append(chunk)
                elif mem_type not in ("project", "reference", "feedback"):
                    other_chunks.append(chunk)
            filtered = project_chunks + reference_chunks + other_chunks
            return self._retriever.format_for_injection(filtered)
        except Exception as exc:
            logger.debug("RAG retrieval failed: %s", exc)
            return ""

    def get_feedback_for_files(
        self, accessed_files: set[str], *, record_access: bool = False,
    ) -> str:
        """
        根据已访问文件的锚点匹配，返回相关 feedback 记忆内容。

        feedback 规则嵌入 task anchor 每步注入，不会被 compaction 丢失。

        Args:
            accessed_files: 已访问的文件路径集合（相对路径）
            record_access: 是否递增匹配到的记忆的 access_count

        Returns:
            格式化的 feedback 记忆文本；无匹配时返回空字符串。
        """
        if not self._enabled or not accessed_files:
            return ""

        summaries = self._store.list_memories()
        feedback_mems = [s for s in summaries if s.type == "feedback"]
        if not feedback_mems:
            return ""

        normalized_files = {
            p.replace("\\", "/").lstrip("./") for p in accessed_files
        }

        matched_memories: list[str] = []
        matched_names: list[str] = []
        for mem_summary in feedback_mems:
            mem = self._store.read_memory(mem_summary.name)
            if mem is None:
                continue
            for anchor in mem.anchors:
                if anchor.kind != "file" or not anchor.path:
                    continue
                anchor_path = anchor.path.replace("\\", "/").lstrip("./")
                for f in normalized_files:
                    if f == anchor_path or f.startswith(anchor_path + "/"):
                        # ── P1: deprecated memories are NOT injected ──
                        if mem.metadata.status is not MemoryStatus.ACTIVE:
                            logger.debug(
                                "Skipping %s feedback memory '%s'",
                                mem.metadata.status.value, mem.name,
                            )
                            break

                        # ── P1-a: Content hash verification — Code is Truth ──
                        # If the memory was bound to a specific file version (hash),
                        # verify the current file matches. If not, physically discard.
                        bound_hash = anchor.content_hash
                        if bound_hash:
                            try:
                                _path = Path(anchor_path)
                                if _path.exists():
                                    _current = hashlib.sha256(
                                        _path.read_bytes()
                                    ).hexdigest()
                                    if _current != bound_hash:
                                        logger.info(
                                            "Memory '%s' physically discarded: "
                                            "content hash mismatch for %s",
                                            mem.name, anchor_path,
                                        )
                                        mem.metadata.status = MemoryStatus.DEPRECATED
                                        self._store.write_memory(mem)
                                        break
                            except (OSError, ImportError):
                                pass  # can't verify — let it through
                        matched_memories.append(
                            f"### {mem.name}\n{mem.content.strip()}"
                        )
                        matched_names.append(mem.name)
                        break
                else:
                    continue
                break

        if not matched_memories:
            return ""

        if record_access:
            for name in matched_names:
                self._store.record_access(name)

        return "\n\n".join([
            "## Feedback Rules (triggered by file access)",
            *matched_memories,
        ])

    def get_procedural_for_files(
        self, accessed_files: set[str], *, record_access: bool = False,
    ) -> str:
        """Backward-compatible alias for get_feedback_for_files()."""
        return self.get_feedback_for_files(accessed_files, record_access=record_access)


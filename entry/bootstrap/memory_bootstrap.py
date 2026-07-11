"""Memory system bootstrap — initializes the two-tier memory system.

Constitution: memory initialization belongs in entry/bootstrap/ — it's
assembly logic, not CLI logic.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def init_memory(repo_path: str, config: Any) -> tuple:
    """Initialize the two-tier memory system. Returns (store, context, external_store).

    Gracefully degrades when fastembed is not available: disables semantic
    search but keeps file-based indexing.
    """
    from memory.store import TwoTierMemoryStore
    from memory.context import MemoryContext
    from llm.router import create_selector_backend

    retriever = None
    external_store = None
    indexer = None

    try:
        import fastembed  # noqa: F401
        from memory.external_store import ExternalMemoryStore
        from memory.indexer import MemoryIndexer
        from memory.retriever import ProactiveRetriever

        external_store = ExternalMemoryStore()
        indexer = MemoryIndexer(external_store)
        retriever = ProactiveRetriever(external_store, max_chunks=5, max_tokens=2000)
    except ImportError:
        logger.info(
            "fastembed not installed — semantic memory search disabled. "
            "Install: pip install 'coding-agent[rag]'"
        )

    memory_store = TwoTierMemoryStore(
        repo_path=repo_path,
        memory_dir=config.memory.directory or None,
        max_index_lines=config.memory.max_index_lines,
        indexer=indexer,
    )
    selector_backend = create_selector_backend({
        "memory": {
            "selector_enabled": config.memory.selector_enabled,
            "selector_model": config.memory.selector_model,
        },
        "llm": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key,
            "base_url": config.llm.base_url,
        },
    })
    memory_context = MemoryContext(
        store=memory_store,
        max_lines=config.memory.max_index_lines,
        enabled=config.memory.enabled,
        retriever=retriever,
        selector_backend=selector_backend,
    )
    return memory_store, memory_context, external_store

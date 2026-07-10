"""
Backward-compatible re-export layer.

All logic has moved to ``runtime/streaming_executor.py``. This module re-exports
the same symbols so existing imports and tests continue to work.

Deprecated — new code should import from ``runtime.streaming_executor``.
"""

from __future__ import annotations

from runtime.streaming_executor import (
    ExecuteFn,
    SiblingAbortController,
    StreamingToolExecutor,
    ToolStatus,
    TrackedTool,
)

SiblingStreamingToolExecutor = StreamingToolExecutor

__all__ = [
    "ExecuteFn",
    "SiblingAbortController",
    "SiblingStreamingToolExecutor",
    "StreamingToolExecutor",
    "ToolStatus",
    "TrackedTool",
]

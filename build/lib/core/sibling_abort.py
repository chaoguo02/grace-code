"""
Backward-compatible re-export layer.

Logic has moved to ``core/streaming_executor.py``.
Deprecated — new code should import from ``core.streaming_executor`` directly.
"""

from __future__ import annotations

from core.streaming_executor import (
    SiblingAbortController,
    StreamingToolExecutor,
    TrackedStatus as ToolStatus,
    TrackedTool,
)

SiblingStreamingToolExecutor = StreamingToolExecutor

__all__ = [
    "SiblingAbortController",
    "SiblingStreamingToolExecutor",
    "StreamingToolExecutor",
    "ToolStatus",
    "TrackedTool",
]

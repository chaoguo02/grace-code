"""Provider-neutral LLM response extraction."""

from __future__ import annotations


def response_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    action = getattr(response, "action", None)
    if action is not None:
        message = getattr(action, "message", None)
        if isinstance(message, str) and message:
            return message
        thought = getattr(action, "thought", None)
        if isinstance(thought, str):
            return thought
    raw_content = getattr(response, "raw_content", None)
    return raw_content if isinstance(raw_content, str) else ""

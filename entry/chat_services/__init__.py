"""entry/chat_services/ — services extracted from ChatSession.

Each module handles one aspect of chat session management:
  agent_session_factory — agent creation, rebuild, model switching
"""

from entry.chat_services.agent_session_factory import (
    create_chat_agent,
    rebuild_backend_for_model,
)

__all__ = ["create_chat_agent", "rebuild_backend_for_model"]

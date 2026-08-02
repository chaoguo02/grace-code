from __future__ import annotations

import logging
import os
from typing import Any

from config.schema import ObservabilityConfig

logger = logging.getLogger(__name__)


def create_langfuse_client(config: ObservabilityConfig) -> tuple[Any | None, Any | None]:
    if not config.enabled or config.provider != "langfuse":
        return None, None

    public_key = config.langfuse.public_key.strip()
    secret_key = config.langfuse.secret_key.strip()
    base_url = config.langfuse.base_url.strip()

    if not public_key or not secret_key:
        logger.info("Langfuse observability disabled: missing credentials")
        return None, None

    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url

    try:
        from langfuse import get_client, propagate_attributes
    except ImportError:
        logger.warning("Langfuse package is not installed; observability will stay disabled")
        return None, None

    try:
        client = get_client()
    except Exception as exc:
        logger.warning("Failed to initialize Langfuse client: %s", exc)
        return None, None

    return client, propagate_attributes

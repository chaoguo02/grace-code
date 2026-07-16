"""LLMInvoker — encapsulates LLM call + retry + token tracking.

Constitution: llm/ owns "provider adapter, request/response normalization,
streaming, token counting." LLMInvoker is a pure function of (backend, config,
messages, tools, prompt_metadata) → InvokeResult — it depends on nothing in
agent/ or above.

Extracted from ReActAgent._call_with_retry().
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llm.base import LLMBackend, LLMMessage, LLMToolSchema, LLMResponse, CacheStats

logger = logging.getLogger(__name__)


@dataclass
class InvokeResult:
    """Result of a single LLM invocation, with all tracking metadata."""
    response: Any                # LLMResponse
    billable_tokens: int         # tokens charged to budget (cache-aware)
    duration_ms: float = 0.0


@dataclass
class LLMInvoker:
    """Invoke the LLM with retry + exponential backoff. Pure function of
    (backend, config, messages, tools, prompt_metadata) → InvokeResult.

    Does NOT depend on ReActAgent state. Does NOT know about tasks, tools,
    or conversation history beyond what it receives as arguments.
    """

    backend: Any          # LLMBackend
    config: Any           # AgentConfig

    def invoke(
        self,
        messages: list[Any],    # list[LLMMessage]
        tools: list[Any],       # list[LLMToolSchema]
        *,
        cumulative_cache: Any = None,  # CacheStats — mutated in place
        provider_name: str = "",
        prompt_metadata: list[dict[str, Any]] | None = None,
    ) -> InvokeResult:
        """Call the LLM with retry + observability. Returns InvokeResult.

        prompt_metadata is consumed by the CALLER (from agent.prompt) and
        passed in — llm/ does not depend on agent/.
        """
        from observability.tracing import get_observer
        from observability.models import (
            build_generation_input, build_generation_metadata,
            build_generation_output, merge_metadata,
        )

        observer = get_observer()
        capture_prompts = observer.config.capture_prompts if observer.config else True
        capture_llm_outputs = observer.config.capture_llm_outputs if observer.config else True
        provider = provider_name or type(self.backend).__name__.removesuffix("Backend").lower()
        _pm = prompt_metadata or []

        start = _time.perf_counter()
        delay = self.config.llm_retry_delay
        last_exc: Exception | None = None

        for attempt in range(1, self.config.llm_max_retries + 1):
            try:
                with observer.start_generation(
                    name="llm-completion",
                    model=self.backend.model_name,
                    input_data=build_generation_input(messages, tools, capture_prompts=capture_prompts),
                    metadata={"attempt": attempt, "provider": provider, "model": self.backend.model_name, "prompts": _pm},
                ) as gen_obs:
                    if self.config.stream:
                        cb = self.config.stream_callback
                        thought_cb = self.config.thought_callback
                        if hasattr(self.backend, "stream"):
                            response = self.backend.stream(messages, tools, on_text=cb, on_thought=thought_cb)
                        else:
                            response = self.backend.complete(messages, tools)
                    else:
                        response = self.backend.complete(messages, tools)

                    gen_obs.update(
                        output=build_generation_output(response, capture_llm_outputs=capture_llm_outputs),
                        metadata=merge_metadata(
                            build_generation_metadata(response, attempt=attempt, provider=provider, model=self.backend.model_name),
                            {"prompts": _pm},
                        ),
                    )

                billable = response.total_tokens
                if cumulative_cache is not None and response.cache_stats and response.cache_stats.has_cache_activity:
                    cumulative_cache.cache_read_tokens += response.cache_stats.cache_read_tokens
                    cumulative_cache.cache_creation_tokens += response.cache_stats.cache_creation_tokens
                    cumulative_cache.non_cached_input_tokens += response.cache_stats.non_cached_input_tokens
                    billable = max(0, billable - response.cache_stats.cache_read_tokens)

                duration = (_time.perf_counter() - start) * 1000
                return InvokeResult(response=response, billable_tokens=max(0, billable), duration_ms=duration)

            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in ("401", "403", "invalid api key", "authentication", "400", "bad request")):
                    raise
                if attempt < self.config.llm_max_retries:
                    logger.warning("LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                                   attempt, self.config.llm_max_retries, exc, delay)
                    _time.sleep(delay)
                    delay *= 2

        raise last_exc  # type: ignore[misc]

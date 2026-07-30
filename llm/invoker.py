"""LLMInvoker — encapsulates LLM call + retry + token tracking.

Constitution: llm/ owns "provider adapter, request/response normalization,
streaming, token counting." LLMInvoker is a pure function of (backend, config,
messages, tools, prompt_metadata) → InvokeResult — it depends on nothing in
agent/ or above.

Extracted from ReActAgent._call_with_retry().
"""

from __future__ import annotations

import logging
import queue
import random as _random
import threading
import time as _time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def iter_with_timeout(
    iterator_factory,
    *,
    timeout: float,
    cancellation_token: Any = None,
    backend: Any = None,
):
    """Yield a blocking provider iterator under a hard, cancellable deadline.

    Phase 2: if *backend* is provided, its close() is called on timeout/cancel
    to unblock the producer thread's blocking SDK call.
    """
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def _produce() -> None:
        try:
            for item in iterator_factory():
                events.put(("item", item))
        except Exception as exc:
            events.put(("error", exc))
        finally:
            events.put(("done", None))

    worker = threading.Thread(target=_produce, daemon=True)
    worker.start()
    deadline = _time.monotonic() + max(0.0, float(timeout))
    try:
        while True:
            if cancellation_token is not None and getattr(
                cancellation_token, "is_cancelled", False
            ):
                raise InterruptedError(
                    getattr(cancellation_token, "detail", "")
                    or "LLM request cancelled"
                )
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"LLM backend stream timed out after {float(timeout):.0f}s"
                )
            try:
                kind, value = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if kind == "item":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    except (TimeoutError, InterruptedError):
        # Phase 2: attempt graceful close before abandoning the daemon thread
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass
        raise


@dataclass
class InvokeResult:
    """Result of a single LLM invocation, with all tracking metadata."""
    response: Any                # LLMResponse
    billable_tokens: int         # tokens charged to budget (cache-aware)
    duration_ms: float = 0.0
    truncated: bool = False      # CC: output was cut off (finish_reason="length")
    finish_reason: str = ""      # provider finish_reason ("stop", "length", "tool_calls")


@dataclass
class RetryMetrics:
    """Per-invocation retry statistics (P2-18).

    Collected during the LLMInvoker retry loop and accessible via callback
    after the invocation completes.  Zero-overhead when no callback is
    registered.
    """

    attempts: int = 0
    """Total attempts made (1 = success on first try)."""

    retries: int = 0
    """Number of retries after the first attempt."""

    last_error_type: str = ""
    """Type name of the last retryable exception, if any."""

    backoff_total_ms: float = 0.0
    """Cumulative backoff sleep time in milliseconds."""


@dataclass
class LLMInvoker:
    """Invoke the LLM with retry + exponential backoff. Pure function of
    (backend, config, messages, tools, prompt_metadata) → InvokeResult.

    Does NOT depend on ReActAgent state. Does NOT know about tasks, tools,
    or conversation history beyond what it receives as arguments.
    """

    backend: Any          # LLMBackend
    config: Any           # AgentConfig
    metrics_callback: Any = None  # Callable[[RetryMetrics], None] | None

    _DEFAULT_REQUEST_TIMEOUT: float = 300.0
    """Per-request timeout for LLM backend calls (seconds).
    Prevents hung providers from blocking agent threads indefinitely."""

    def _call_with_timeout(self, fn, *args, **kwargs):
        """Run one backend call with a hard wall-clock timeout."""
        from llm.provider_capacity import acquire_provider_capacity

        timeout = max(
            0.001,
            float(getattr(
                self.config, "request_timeout", self._DEFAULT_REQUEST_TIMEOUT,
            )),
        )
        provider_lease = acquire_provider_capacity(
            self.backend,
            args[0] if args else [],
            max_output_tokens=max(
                0, int(getattr(self.config, "max_tokens", 0)),
            ),
            timeout_s=timeout,
            cancellation_token=getattr(
                self.config, "cancellation_token", None,
            ),
        )

        result: list[Any] = []
        error: list[Exception] = []

        def _target() -> None:
            try:
                result.append(fn(*args, **kwargs))
            except Exception as exc:
                error.append(exc)

        try:
            t = threading.Thread(target=_target, daemon=True)
            t.start()
            deadline = _time.monotonic() + max(0.0, float(timeout))
            while t.is_alive():
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=min(0.1, remaining))
                cancellation = getattr(self.config, "cancellation_token", None)
                if cancellation is not None and getattr(
                    cancellation, "is_cancelled", False
                ):
                    raise InterruptedError(
                        getattr(cancellation, "detail", "")
                        or "LLM request cancelled"
                    )
            if t.is_alive():
                backend = getattr(self, "backend", None)
                if backend is not None and hasattr(backend, "close"):
                    try:
                        backend.close()
                    except Exception:
                        pass
                # Give cooperative SDK clients a bounded opportunity to exit.
                t.join(timeout=min(1.0, timeout))
                logger.warning(
                    "LLM backend call timed out after %.0fs; worker_alive=%s",
                    timeout,
                    t.is_alive(),
                )
                raise TimeoutError(
                    f"LLM backend call timed out after {timeout:.0f}s"
                )
            if error:
                raise error[0]
            return result[0]
        finally:
            provider_lease.release(result[0] if result else None)

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
        _metrics = RetryMetrics(attempts=0, retries=0)
        _backoff_total: float = 0.0

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
                            callbacks_active = threading.Event()
                            callbacks_active.set()

                            def guarded_text(*args, **kwargs):
                                if callbacks_active.is_set() and cb is not None:
                                    return cb(*args, **kwargs)
                                return None

                            def guarded_thought(*args, **kwargs):
                                if (
                                    callbacks_active.is_set()
                                    and thought_cb is not None
                                ):
                                    return thought_cb(*args, **kwargs)
                                return None

                            try:
                                response = self._call_with_timeout(
                                    self.backend.stream,
                                    messages,
                                    tools,
                                    on_text=guarded_text,
                                    on_thought=guarded_thought,
                                )
                            finally:
                                callbacks_active.clear()
                        else:
                            response = self._call_with_timeout(
                                self.backend.complete, messages, tools,
                            )
                    else:
                        response = self._call_with_timeout(
                            self.backend.complete, messages, tools,
                        )

                    gen_obs.update(
                        output=build_generation_output(response, capture_llm_outputs=capture_llm_outputs),
                        metadata=merge_metadata(
                            build_generation_metadata(response, attempt=attempt, provider=provider, model=self.backend.model_name),
                            {"prompts": _pm},
                        ),
                    )

                _metrics.attempts = attempt
                billable = response.total_tokens
                if cumulative_cache is not None and response.cache_stats and response.cache_stats.has_cache_activity:
                    cumulative_cache.cache_read_tokens += response.cache_stats.cache_read_tokens
                    cumulative_cache.cache_creation_tokens += response.cache_stats.cache_creation_tokens
                    cumulative_cache.non_cached_input_tokens += response.cache_stats.non_cached_input_tokens
                    billable = max(0, billable - response.cache_stats.cache_read_tokens)

                truncated = (
                    response.finish_reason == "length"
                    or response.output_tokens >= getattr(self.config, "max_tokens", 32000) - 100
                )
                duration = (_time.perf_counter() - start) * 1000
                ret = InvokeResult(
                    response=response,
                    billable_tokens=max(0, billable),
                    duration_ms=duration,
                    truncated=truncated,
                    finish_reason=response.finish_reason,
                )
                if self.metrics_callback is not None:
                    _metrics.backoff_total_ms = _backoff_total
                    self.metrics_callback(_metrics)
                return ret

            except Exception as exc:
                last_exc = exc
                _metrics.last_error_type = type(exc).__name__
                status_code = (
                    getattr(exc, "status_code", None)
                    or getattr(exc, "http_status", None)
                )
                if status_code == 429:
                    provider_governor = getattr(
                        self.backend, "_provider_governor", None,
                    )
                    if provider_governor is not None:
                        provider_governor.record_response(
                            provider,
                            status=429,
                            retry_after=float(
                                getattr(exc, "retry_after", 0.0) or 0.0
                            ),
                        )
                # P2-41: check HTTP status code directly, not substring match
                _is_non_retryable = (
                    isinstance(exc, InterruptedError)
                    or _metrics.last_error_type == "AuthenticationError"
                    or getattr(exc, "status_code", None) in (400, 401, 403)
                    or getattr(exc, "http_status", None) in (400, 401, 403)
                )
                if not _is_non_retryable:
                    exc_str = str(exc).lower()
                    _is_non_retryable = any(
                        kw in exc_str for kw in ("invalid api key", "authentication")
                    )
                if _is_non_retryable:
                    if self.metrics_callback is not None:
                        _metrics.retries = attempt - 1
                        _metrics.backoff_total_ms = _backoff_total
                        self.metrics_callback(_metrics)
                    raise
                if attempt < self.config.llm_max_retries:
                    _metrics.retries = attempt
                    logger.warning("LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                                   attempt, self.config.llm_max_retries, exc, delay)
                    _base = delay
                    _jittered = _base + _random.uniform(0, _base * 0.3)
                    _time.sleep(_jittered)
                    _backoff_total += _jittered * 1000
                    delay *= 2

        if last_exc is not None:
            if self.metrics_callback is not None:
                _metrics.attempts = attempt
                _metrics.retries = attempt - 1
                _metrics.backoff_total_ms = _backoff_total
                self.metrics_callback(_metrics)
            raise last_exc
        raise RuntimeError("LLM invoke failed: no attempts executed")

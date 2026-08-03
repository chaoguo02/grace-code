"""Shared provider-capacity admission for every LLM request path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def attach_provider_governor(
    backend: Any,
    governor: Any,
    *,
    provider_name: str = "",
) -> Any:
    """Attach one shared provider authority to a backend and return it."""
    backend._provider_governor = governor
    name = (
        provider_name
        or type(backend).__name__.removesuffix("Backend")
    )
    backend._provider_limiter = governor.get_limiter(name)
    return backend


def estimate_request_tokens(
    messages: Iterable[Any],
    *,
    max_output_tokens: int = 0,
) -> int:
    """Return the conservative reservation used by the provider TPM limiter."""
    input_chars = sum(
        len(str(getattr(message, "content", "")))
        for message in messages
    )
    return max(1, input_chars // 4) + max(0, int(max_output_tokens))


@dataclass
class ProviderCapacityLease:
    """Idempotent capacity lease shared by agent and maintenance LLM calls."""

    limiter: Any = None
    reserved_tokens: int = 0
    acquired: bool = False
    _released: bool = False

    def release(self, response: Any = None) -> None:
        if self._released:
            return
        self._released = True
        if not self.acquired or self.limiter is None:
            return
        self.limiter.release(
            tokens_used=max(0, int(getattr(response, "total_tokens", 0))),
            reserved_tokens=self.reserved_tokens,
        )


def acquire_provider_capacity(
    backend: Any,
    messages: list[Any],
    *,
    max_output_tokens: int = 0,
    timeout_s: float = 60.0,
    cancellation_token: Any = None,
) -> ProviderCapacityLease:
    """Acquire the backend's shared provider limiter or raise on timeout."""
    limiter = getattr(backend, "_provider_limiter", None)
    if limiter is None:
        return ProviderCapacityLease()

    reserved = estimate_request_tokens(
        messages,
        max_output_tokens=max_output_tokens,
    )
    acquired = limiter.acquire_wait(
        tokens=reserved,
        timeout_s=max(0.001, float(timeout_s)),
        cancellation_token=cancellation_token,
    )
    if not acquired:
        raise TimeoutError(
            f"Provider capacity wait timed out after {timeout_s:.0f}s"
        )
    return ProviderCapacityLease(
        limiter=limiter,
        reserved_tokens=reserved,
        acquired=True,
    )


def complete_with_provider_capacity(
    backend: Any,
    messages: list[Any],
    tools: list[Any],
    *,
    max_output_tokens: int = 0,
    timeout_s: float | None = None,
    cancellation_token: Any = None,
) -> Any:
    """Run a non-streaming auxiliary LLM call through shared admission."""
    effective_timeout = (
        float(timeout_s)
        if timeout_s is not None
        else float(getattr(backend, "timeout_seconds", 300.0))
    )
    lease = acquire_provider_capacity(
        backend,
        messages,
        max_output_tokens=max_output_tokens,
        timeout_s=effective_timeout,
        cancellation_token=cancellation_token,
    )
    response = None
    try:
        response = backend.complete(messages, tools)
        return response
    finally:
        lease.release(response)

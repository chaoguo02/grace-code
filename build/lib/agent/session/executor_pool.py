"""Borrow Runtime's shared executor with an isolated-runtime fallback."""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def borrowed_executor(
    runtime: object,
    *,
    max_workers: int,
    thread_name_prefix: str,
) -> Iterator[Executor]:
    executor = getattr(runtime, "_shared_executor", None)
    owns_executor = executor is None
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix=thread_name_prefix,
        )
    try:
        yield executor
    finally:
        if owns_executor:
            executor.shutdown(wait=True, cancel_futures=True)

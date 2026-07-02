"""
memory/consolidation.py

AutoDream-style cross-session memory consolidation.

Architecture-aligned implementation based on public Claude Code analyses.
The full autoDream.ts/consolidationLock.ts source is not available here, so
this module intentionally documents inferred behavior rather than claiming
source-equivalence:
- Triple gate in cheap-to-expensive order: 24h time gate → 5 sessions → lock
- Lock file content: {PID}\n{TIMESTAMP_MS}, with file mtime as lastConsolidatedAt
- 1h mtime expiry + PID liveness zombie-lock recovery
- Consolidation work is delegated to a restricted dream runner
- On failure, the lock is released so the next session can retry
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from memory.consolidation_prompt import CONSOLIDATION_PROMPT

logger = logging.getLogger(__name__)

_LOCK_FILENAME = ".consolidate-lock"
_SESSION_COUNTER_FILENAME = ".sessions-since-dream"
_MIN_DREAM_INTERVAL_HOURS = 24
_MIN_SESSIONS_SINCE_DREAM = 5
_LOCK_EXPIRY_SECONDS = 60 * 60
_LOCK_EXPIRY_MS = _LOCK_EXPIRY_SECONDS * 1000
_MAX_MEMORY_LINES = 200
_MAX_MEMORY_BYTES = 25 * 1024

class DreamRunner(Protocol):
    allowed_bash: str
    allowed_write_root: Path

    def run(self, *, memory_dir: Path, prompt: str, log_dir: str | None = None) -> bool:
        """Run the restricted dream agent."""


@dataclass
class RuleDreamRunner:
    """
    Local deterministic dream runner used when no LLM child-session runtime is attached.

    It follows the same memory-dir-only write boundary and performs a conservative
    index prune. A production integration can inject a real fork-subagent runner
    with the same interface.
    """

    allowed_write_root: Path
    allowed_bash: str = "read-only"

    def run(self, *, memory_dir: Path, prompt: str, log_dir: str | None = None) -> bool:
        if memory_dir.resolve() != self.allowed_write_root.resolve():
            raise ValueError("Dream runner may only write inside its configured memory directory")
        del prompt
        manifest = _dream_orient(memory_dir)
        gathered = _dream_gather(memory_dir, manifest)
        changed = _dream_consolidate(memory_dir, gathered)
        changed = _dream_prune(memory_dir) or changed
        if log_dir:
            _write_log(log_dir, "dream completed" if changed else "dream completed with no changes")
        return changed


@dataclass
class LLMDreamRunner:
    """LLM-backed restricted DreamAgent runner."""

    backend: Any
    allowed_write_root: Path
    allowed_bash: str = "read-only"

    def run(self, *, memory_dir: Path, prompt: str, log_dir: str | None = None) -> bool:
        if memory_dir.resolve() != self.allowed_write_root.resolve():
            raise ValueError("Dream runner may only write inside its configured memory directory")
        del prompt
        from memory.dream_agent import DreamAgent

        result = DreamAgent(memory_dir=memory_dir, backend=self.backend).run()
        if log_dir:
            _write_log(log_dir, result.summary or "dream completed")
        return result.changed


def _start_async_dream(runner: LLMDreamRunner, memory_dir: Path, lock_path: Path, log_dir: str | None) -> threading.Thread:
    """Start LLM DreamAgent in the background, releasing lock when done."""
    def _target() -> None:
        try:
            changed = runner.run(memory_dir=memory_dir, prompt=CONSOLIDATION_PROMPT, log_dir=log_dir)
            _reset_session_counter(memory_dir)
        except Exception as exc:
            logger.debug("Async dream failed: %s", exc)
        finally:
            _release_lock(lock_path)

    thread = threading.Thread(target=_target, daemon=True, name="dream-consolidation")
    thread.start()
    return thread


def record_session_end(memory_dir: Path) -> int:
    """Record one completed session without triggering consolidation."""
    return _increment_session_counter(memory_dir)


def run_consolidation(
    store,
    log_dir: str | None = None,
    *,
    sessions_since_last_dream: int | None = None,
    runner: DreamRunner | None = None,
    backend: Any | None = None,
    async_run: bool = False,
) -> bool:
    """Run AutoDream consolidation if all gates pass."""
    memory_dir = _validate_memory_dir(store.store_dir)
    lock_path = memory_dir / _LOCK_FILENAME

    # Gate 1: time (cheapest)
    if not _time_gate_passed(memory_dir):
        return False

    # Gate 2: sessions since last dream
    sessions_since = (
        sessions_since_last_dream
        if sessions_since_last_dream is not None
        else _read_session_counter(memory_dir)
    )
    if sessions_since < _MIN_SESSIONS_SINCE_DREAM:
        return False

    # Gate 3: lock acquisition (most expensive)
    if not _acquire_lock(lock_path):
        return False

    async_started = False
    try:
        active_runner = runner or (LLMDreamRunner(backend, memory_dir) if backend is not None else RuleDreamRunner(memory_dir))
        if async_run and isinstance(active_runner, LLMDreamRunner):
            _start_async_dream(active_runner, memory_dir, lock_path, log_dir)
            async_started = True
            return True
        changed = active_runner.run(
            memory_dir=memory_dir,
            prompt=CONSOLIDATION_PROMPT,
            log_dir=log_dir,
        )
        _reset_session_counter(memory_dir)
        return changed
    except Exception:
        if not async_started:
            _release_lock(lock_path)
        raise
    finally:
        if not async_started:
            _release_lock(lock_path)


# ─── Gates and lock helpers ───────────────────────────────────────────────────

def _validate_memory_dir(memory_dir: Path) -> Path:
    """Reject obviously sensitive memory roots before enabling write-capable runners."""
    resolved = memory_dir.expanduser().resolve()
    sensitive_names = {".ssh", ".gnupg", ".aws", ".azure", ".config"}
    if resolved.name.lower() in sensitive_names:
        raise ValueError(f"Refusing to use sensitive directory as memory root: {resolved}")
    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return resolved
    if relative.parts and relative.parts[0].lower() in sensitive_names:
        raise ValueError(f"Refusing to use sensitive directory as memory root: {resolved}")
    return resolved


def _time_gate_passed(memory_dir: Path, *, now_ms: int | None = None) -> bool:
    last_consolidated = _last_consolidated_at(memory_dir)
    if last_consolidated == 0.0:
        return True
    now_seconds = (now_ms / 1000) if now_ms is not None else time.time()
    hours_since = (now_seconds - last_consolidated) / 3600
    return hours_since >= _MIN_DREAM_INTERVAL_HOURS


def _acquire_lock(lock_path: Path, *, now_ms: int | None = None) -> bool:
    """Acquire mtime+PID consolidation lock, recovering stale locks."""
    if lock_path.exists():
        if not _is_stale_lock(lock_path, now_ms=now_ms):
            return False
        _release_lock(lock_path)

    try:
        lock_path.write_text(
            f"{os.getpid()}\n{now_ms if now_ms is not None else _now_ms()}",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def _read_lock(lock_path: Path) -> tuple[int, int]:
    content = lock_path.read_text(encoding="utf-8")
    pid_text, timestamp_text = content.split("\n", 1)
    return int(pid_text.strip()), int(timestamp_text.strip())


def _is_stale_lock(lock_path: Path, *, now_ms: int | None = None) -> bool:
    try:
        mtime = lock_path.stat().st_mtime
        now_seconds = (now_ms / 1000) if now_ms is not None else time.time()
        if now_seconds - mtime > _LOCK_EXPIRY_SECONDS:
            return True
        pid, _timestamp = _read_lock(lock_path)
        os.kill(pid, 0)
        return False
    except (ProcessLookupError, OSError, ValueError, FileNotFoundError):
        return True


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
    except OSError:
        pass


def _counter_path(memory_dir: Path) -> Path:
    return memory_dir / _SESSION_COUNTER_FILENAME


def _read_session_counter(memory_dir: Path) -> int:
    try:
        return int(_counter_path(memory_dir).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_session_counter(memory_dir: Path, value: int) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = _counter_path(memory_dir)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(str(max(0, value)), encoding="utf-8")
    os.replace(tmp_path, path)


def _increment_session_counter(memory_dir: Path) -> int:
    value = _read_session_counter(memory_dir) + 1
    _write_session_counter(memory_dir, value)
    return value


def _reset_session_counter(memory_dir: Path) -> None:
    _write_session_counter(memory_dir, 0)


def _last_consolidated_at(memory_dir: Path) -> float:
    lock_path = memory_dir / _LOCK_FILENAME
    if not lock_path.exists():
        return 0.0
    try:
        return lock_path.stat().st_mtime
    except OSError:
        return 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)


# ─── Restricted local dream fallback ──────────────────────────────────────────

def _dream_orient(memory_dir: Path) -> list[Path]:
    """Phase 1: list current memory files without writing."""
    if not memory_dir.exists():
        return []
    return sorted(
        path for path in memory_dir.glob("*.md")
        if path.name != "MEMORY.md" and not path.name.startswith(".")
    )


def _dream_gather(memory_dir: Path, manifest: list[Path]) -> list[Path]:
    """Phase 2: gather candidate files for consolidation without writing."""
    del memory_dir
    return [path for path in manifest if path.exists()]


def _dream_consolidate(memory_dir: Path, gathered: list[Path]) -> bool:
    """Phase 3: placeholder for memory-dir-only consolidation writes."""
    del memory_dir, gathered
    return False


def _dream_prune(memory_dir: Path) -> bool:
    """Phase 4: prune MEMORY.md to hard limits."""
    return _prune_memory_index(memory_dir)


def _prune_memory_index(memory_dir: Path) -> bool:
    from memory.store import _truncate_index

    index_path = memory_dir / "MEMORY.md"
    if not index_path.exists():
        return False
    try:
        original = index_path.read_text(encoding="utf-8")
        truncated = _truncate_index(original, max_lines=_MAX_MEMORY_LINES, max_bytes=_MAX_MEMORY_BYTES)
        if truncated != original:
            index_path.write_text(truncated, encoding="utf-8")
            return True
    except OSError:
        return False
    return False


def _write_log(log_dir: str, message: str) -> None:
    log_path = Path(log_dir) / "memory-consolidation.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}\n")
    except OSError:
        pass

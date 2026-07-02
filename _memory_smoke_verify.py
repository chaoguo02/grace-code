import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from memory.consolidation import (  # noqa: E402
    _acquire_lock,
    _read_session_counter,
    _release_lock,
    record_session_end,
)
from memory.store import _atomic_write_text  # noqa: E402


with tempfile.TemporaryDirectory() as d:
    memory_dir = Path(d)
    record_session_end(memory_dir)
    record_session_end(memory_dir)
    record_session_end(memory_dir)
    assert _read_session_counter(memory_dir) == 3
    assert not (memory_dir / ".consolidate-lock").exists()
    print("OK record_session_end: counter increments without lock side effects")

with tempfile.TemporaryDirectory() as d:
    memory_dir = Path(d)
    lock_path = memory_dir / ".consolidate-lock"
    assert _acquire_lock(lock_path)
    _release_lock(lock_path)
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").strip() == ""
    print("OK lock release: lock file persists empty")

with tempfile.TemporaryDirectory() as d:
    target = Path(d) / "test.txt"
    _atomic_write_text(target, "hello")
    files = os.listdir(d)
    assert files == ["test.txt"], f"Unexpected files: {files}"
    assert target.read_text(encoding="utf-8") == "hello"
    print("OK atomic write: no temp file remains")

print("ALL MEMORY SMOKE CHECKS PASSED")

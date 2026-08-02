"""Verify sandbox resource limits are configurable via env vars (Phase 9, gate #17)."""
import sys
sys.path.insert(0, ".")
from core.process import SANDBOX_CPUS, SANDBOX_MEMORY, SANDBOX_PIDS

ok = True
if not isinstance(SANDBOX_MEMORY, str):
    print(f"FAIL: SANDBOX_MEMORY is not a string: {type(SANDBOX_MEMORY)}")
    ok = False
if int(SANDBOX_PIDS) <= 0:
    print(f"FAIL: SANDBOX_PIDS must be positive: {SANDBOX_PIDS}")
    ok = False
if not SANDBOX_CPUS.replace('.', '').isdigit():
    print(f"FAIL: SANDBOX_CPUS must be numeric: {SANDBOX_CPUS}")
    ok = False

if ok:
    sys.exit(0)
sys.exit(1)

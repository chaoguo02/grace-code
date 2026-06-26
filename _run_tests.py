import subprocess
import sys
import os

os.chdir(r"d:\gc\forge-agent")
result = subprocess.run(
    [sys.executable, "-m", "pytest", "test_plan_mode.py", "-x", "-k", "analysis", "--tb=short", "-q"],
    cwd=r"d:\gc\forge-agent",
    capture_output=True,
    text=True,
)
with open(r"d:\gc\forge-agent\_test_output.txt", "w", encoding="utf-8") as f:
    f.write("=== STDOUT ===\n")
    f.write(result.stdout)
    f.write("\n=== STDERR ===\n")
    f.write(result.stderr)
    f.write(f"\n=== EXIT CODE: {result.returncode} ===\n")
sys.exit(result.returncode)
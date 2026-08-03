"""SSOT keys check: extract model keys from MODEL_CATALOG."""
import re, sys

c = open("server/routers/config.py", encoding="utf-8").read()
models = re.findall(r'\"key\":\s*\"([^\"]+)\"', c)
assert len(models) > 0, "No model keys found in catalog (checked \"key\" format)"
for m in models:
    print(f"  Model: {m}")
sys.exit(0)

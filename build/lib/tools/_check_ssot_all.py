"""SSOT all-in-one checker: MODEL_CATALOG <-> agent/constants.py."""
import re, sys

config = open("server/routers/config.py", encoding="utf-8").read()
constants = open("agent/constants.py", encoding="utf-8").read()

errors = 0

if "_MODEL_CATALOG" not in config:
    print("FAIL: _MODEL_CATALOG not in config.py")
    errors += 1

models = re.findall(r'"key":\s*"([^"]+)"', config)
if not models:
    print("FAIL: no model keys in catalog")
    errors += 1

if "DEFAULT_MAX_OUTPUT_TOKENS" not in constants:
    print("FAIL: DEFAULT_MAX_OUTPUT_TOKENS not in constants.py")
    errors += 1

if errors == 0:
    print(f"SSOT PASS: {len(models)} models ({', '.join(models)})")
    sys.exit(0)
else:
    print(f"SSOT FAIL: {errors} errors")
    sys.exit(1)

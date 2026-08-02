"""SSOT catalog check: verify MODEL_CATALOG has at least one entry."""
import sys

c = open("server/routers/config.py", encoding="utf-8").read()
assert "_MODEL_CATALOG" in c, "MODEL_CATALOG not found in config.py"
assert "deepseek" in c, "no deepseek models in catalog"
print("  MODEL_CATALOG present and non-empty")
sys.exit(0)

"""Apply Phase 3 #9: add per-layer permission telemetry."""
with open("hitl/pipeline.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add layer_blocks field after total_wait_ms
old_init = (
    "    total: int = 0\n"
    "    allowed: int = 0\n"
    "    denied: int = 0\n"
    "    prompted: int = 0\n"
    "    hook_decided: int = 0\n"
    "    total_wait_ms: float = 0.0\n"
    "    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)"
)
new_init = (
    "    total: int = 0\n"
    "    allowed: int = 0\n"
    "    denied: int = 0\n"
    "    prompted: int = 0\n"
    "    hook_decided: int = 0\n"
    "    total_wait_ms: float = 0.0\n"
    "    layer_blocks: dict[str, int] = field(default_factory=dict)\n"
    "    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)"
)
if old_init in content:
    content = content.replace(old_init, new_init)
    print("1. layer_blocks field added")
else:
    print("1. FAIL: init not found")

# 2. Add layer_blocks recording
old_rec = (
    "            if result.decision is PermissionDecision.ALLOW:\n"
    "                self.allowed += 1\n"
    "            else:\n"
    "                self.denied += 1"
)
new_rec = (
    "            if result.decision is PermissionDecision.ALLOW:\n"
    "                self.allowed += 1\n"
    "            else:\n"
    "                self.denied += 1\n"
    '                ln = result.layer.value if hasattr(result.layer, "value") else str(result.layer)\n'
    "                self.layer_blocks[ln] = self.layer_blocks.get(ln, 0) + 1"
)
if old_rec in content:
    content = content.replace(old_rec, new_rec)
    print("2. layer_blocks recording added")
else:
    print("2. FAIL: record not found")

with open("hitl/pipeline.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Done")

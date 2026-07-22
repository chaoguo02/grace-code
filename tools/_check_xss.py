"""ACC-5a: Verify all dangerouslySetInnerHTML sites use renderMarkdownSafe."""
import os, sys

violations = []
for root, dirs, files in os.walk("web/src"):
    for f in files:
        if not f.endswith((".tsx", ".ts")):
            continue
        path = os.path.join(root, f)
        content = open(path, encoding="utf-8").read()
        if "dangerouslySetInnerHTML" in content and "renderMarkdownSafe" not in content:
            violations.append(path)

if violations:
    for v in violations:
        print(f"VIOLATION: {v} — dangerouslySetInnerHTML without renderMarkdownSafe")
    sys.exit(1)

print("ACC-5a OK: all dangerouslySetInnerHTML sites use renderMarkdownSafe")
sys.exit(0)

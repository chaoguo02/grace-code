#!/bin/bash
# _check_css_lint.sh — verify no inline styles in migrated components (Phase 7 Batch B)
# Targets: SubagentDetail, SubagentProgress, SessionTree
set -euo pipefail

BAD=0
for component in SubagentDetail SubagentProgress SessionTree; do
    file="web/src/components/${component}.tsx"
    if [ -f "$file" ]; then
        count=$(grep -c 'style={{' "$file" 2>/dev/null || echo 0)
        if [ "$count" -gt 0 ]; then
            echo "  FAIL: ${component}.tsx has ${count} inline style blocks"
            BAD=$((BAD + 1))
        else
            echo "  PASS: ${component}.tsx — 0 inline style blocks"
        fi
    else
        echo "  SKIP: ${component}.tsx — file not found"
    fi
done

if [ "$BAD" -gt 0 ]; then
    echo ""
    echo "CSS LINT: ${BAD} file(s) have remaining inline styles"
    exit 1
else
    echo ""
    echo "CSS LINT: all migrated components clean"
    exit 0
fi

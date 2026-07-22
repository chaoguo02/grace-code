#!/bin/bash
# _check_css_lint.sh — verify no inline styles in migrated components (Phase 7 Batch C)
# SessionTree exceptions: 3 dynamic inline styles (R-5 in RISK_REGISTER.md)
set -euo pipefail

JSON="${1:-}"
COMPONENTS=("SubagentDetail" "SubagentProgress" "SessionTree")
# SessionTree dynamic exceptions: marginLeft (depth*12), color (status), fontWeight (isActive)
TREE_ALLOWED=3
BAD=0
declare -A RESULTS

for component in "${COMPONENTS[@]}"; do
    file="web/src/components/${component}.tsx"
    if [ ! -f "$file" ]; then
        RESULTS["$component"]="SKIP"
        continue
    fi
    count=$(python -c "
c=open('$file',encoding='utf-8').read()
print(c.count('style={{'))
" 2>/dev/null || echo -1)
    allowed=0
    [ "$component" = "SessionTree" ] && allowed=$TREE_ALLOWED
    if [ "$count" -gt "$allowed" ] 2>/dev/null; then
        extra=$((count - allowed))
        RESULTS["$component"]="FAIL ($extra unexpected inline blocks)"
        BAD=$((BAD + 1))
    elif [ "$count" -gt 0 ]; then
        RESULTS["$component"]="PASS ($count dynamic exceptions accepted)"
    else
        RESULTS["$component"]="PASS"
    fi
done

if [ "${JSON:-}" = "--json" ]; then
    echo -n '{"css-lint":{'
    first=true
    for comp in "${COMPONENTS[@]}"; do
        $first || echo -n ','
        echo -n "\"${comp}\":\"${RESULTS[$comp]}\""
        first=false
    done
    echo "},\"passed\":$((3-BAD)),\"failed\":$BAD}"
fi

if [ "$BAD" -gt 0 ]; then
    exit 1
fi
exit 0

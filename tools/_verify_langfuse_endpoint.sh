#!/bin/bash
# _verify_langfuse_endpoint.sh — health-check Langfuse connectivity (Phase 7 Batch C)
# Conditional gate: only invoked when FORGE_OBSERVE_RETRIES=1
set -euo pipefail

if [ "${FORGE_OBSERVE_RETRIES:-0}" != "1" ]; then
    echo "Langfuse health: SKIP (FORGE_OBSERVE_RETRIES not set)"
    exit 0
fi

HOST="${LANGFUSE_BASE_URL:-https://cloud.langfuse.com}"

if curl -sf --max-time 5 "${HOST}/api/public/health" > /dev/null 2>&1; then
    echo "Langfuse health: OK (${HOST})"
    exit 0
else
    echo "Langfuse health: FAIL — ${HOST} unreachable or non-200"
    exit 1
fi

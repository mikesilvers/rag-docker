#!/usr/bin/env bash
# Optional collector acceptance (#285). Always guarded by the disposable lock.
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Optional OpenTelemetry collector"
if [ "${RAG_VERIFY_TELEMETRY:-0}" != 1 ]; then
  docker compose exec -T api python -c 'import os; assert os.environ.get("RAG_OTEL_ENABLED", "false") == "false"'
  check "default running API disables telemetry" $?
  skip "enabled collector end-to-end acceptance" "run stack.sh run --telemetry"
else
  if [ "$SKIP_SLOW" = 1 ]; then
    skip "full telemetry query/job acceptance" "requires local model work; run without RAG_SKIP_SLOW"
  else
    python3 ./telemetry_e2e.py
    check "collector traces, metrics, logs and outage acceptance" $?
  fi
fi
summary

#!/usr/bin/env bash
# Settings validation through the running API; the helper owns its collection.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Settings validation before work"
RAG_API="$API" python3 ./settings_validation.py
check "live settings validation and owned-fixture cleanup" $?
summary

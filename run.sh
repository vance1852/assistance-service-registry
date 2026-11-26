#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export DISPATCH_DB="${DISPATCH_DB:-$PWD/dispatch-state.json}"
export PARTNER_BASE="${PARTNER_BASE:-http://127.0.0.1:8090}"

exec python3 middleware.py \
  --host "${DISPATCH_HOST:-${HOST:-127.0.0.1}}" \
  --port "${PORT:-${DISPATCH_PORT:-8080}}" \
  --db "$DISPATCH_DB" \
  --partner-base "$PARTNER_BASE" \
  "$@"

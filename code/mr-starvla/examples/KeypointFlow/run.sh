#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../deployment/astra/env.sh"
backend="${1:?starvla, pi05, or tools}"
operation="${2:?serve or test}"
shift 2
case "$backend" in
  starvla) astra_activate "$STARVLA_ENV" ;;
  pi05|tools) astra_activate "$PI05_ENV" ;;
  *) echo "Unknown backend: $backend" >&2; exit 2 ;;
esac
export PYTHONPATH="$KEYPOINT_FLOW_WORKSPACE:$ASTRA_REPO:$OPENPI_ROOT/src:$OPENPI_ROOT/packages/openpi-client/src:${PYTHONPATH:-}"
export XLA_PYTHON_CLIENT_PREALLOCATE=false
case "$backend:$operation" in
  starvla:serve|pi05:serve) exec python -m "starVLA.keypoint_flow.serve_$backend" "$@" ;;
  tools:test) exec python -m pytest "$KEYPOINT_FLOW_WORKSPACE/../tests" "$@" ;;
  *) echo "Unsupported operation: $backend $operation" >&2; exit 2 ;;
esac

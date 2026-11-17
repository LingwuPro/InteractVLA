#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../deployment/astra/env.sh"
operation="${1:?start, status, or stop}"
backend="${2:?starvla or pi05}"
shift 2
directory="$ASTRA_REPO/work_dirs/keypoint_flow"
mkdir -p "$directory/services" "$directory/logs"
case "$backend" in
  starvla)
    checkpoint="$STARVLA_CHECKPOINT"
    gpu="${KEYPOINT_GPU:-0}"
    ;;
  pi05)
    checkpoint="$ASTRA_RUN/models/checkpoints/pi05_base"
    gpu="${KEYPOINT_GPU:-2}"
    ;;
  *) echo "Unknown backend: $backend" >&2; exit 2 ;;
esac
pidfile="$directory/services/$backend.pid"
case "$operation" in
  start)
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "$backend is already running with PID $(cat "$pidfile")"
      exit 0
    fi
    CUDA_VISIBLE_DEVICES="$gpu" setsid bash "$ASTRA_REPO/examples/KeypointFlow/run.sh" "$backend" serve \
      --checkpoint "$checkpoint" --manifest "${KEYPOINT_MANIFEST:?Set KEYPOINT_MANIFEST}" \
      --config "${KEYPOINT_CONFIG:-$ASTRA_REPO/examples/KeypointFlow/libero.json}" "$@" \
      > "$directory/logs/serve_$backend.log" 2>&1 < /dev/null &
    echo "$!" > "$pidfile"
    echo "$backend starting with PID $!; log: $directory/logs/serve_$backend.log"
    ;;
  status)
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "$backend running with PID $(cat "$pidfile")"
    else
      echo "$backend stopped"
    fi
    ;;
  stop)
    if [[ -f "$pidfile" ]]; then
      process="$(cat "$pidfile")"
      if [[ -r "/proc/$process/cmdline" ]] && tr '\0' ' ' < "/proc/$process/cmdline" | rg -q 'starVLA.keypoint_flow.serve_'; then
        kill "$process"
      fi
      rm -f "$pidfile"
    fi
    ;;
  *) echo "Unknown operation: $operation" >&2; exit 2 ;;
esac

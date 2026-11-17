#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
operation="${1:?start|status|stop}"
model="${2:?starvla|pi05_libero|pi05_base}"
case "$model" in
  starvla) default_gpu=0; default_port=18100; marker="$STARVLA_CHECKPOINT" ;;
  pi05_libero) default_gpu=1; default_port=18101; marker="$ASTRA_RUN/models/checkpoints/pi05_libero" ;;
  pi05_base) default_gpu=2; default_port=18102; marker="$ASTRA_RUN/models/checkpoints/pi05_base" ;;
  *) echo "Unknown model: $model" >&2; exit 2 ;;
esac
mkdir -p "$ASTRA_RUN/pids"
pidfile="$ASTRA_RUN/pids/$model.pid"
pid=""
[[ ! -f "$pidfile" ]] || read -r pid < "$pidfile"
running=false
if [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]]; then
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
  [[ "$command_line" != *"$marker"* ]] || running=true
fi
case "$operation" in
  status)
    if "$running"; then echo "$model running pid=$pid"; else echo "$model stopped"; exit 1; fi
    ;;
  start)
    if "$running"; then echo "$model already running pid=$pid"; exit 0; fi
    export GPU="${GPU:-$default_gpu}" PORT="${PORT:-$default_port}"
    nohup bash "$ASTRA_REPO/deployment/astra/serve.sh" "$model" \
      > "$ASTRA_RUN/logs/${model}_server.log" 2>&1 < /dev/null &
    echo "$!" > "$pidfile"
    echo "$model launched pid=$! port=$PORT; check $ASTRA_RUN/logs/${model}_server.log for readiness"
    ;;
  stop)
    if "$running"; then kill -TERM "$pid"; echo "$model stop requested pid=$pid"; fi
    rm -f "$pidfile"
    ;;
  *) echo "Unknown operation: $operation" >&2; exit 2 ;;
esac

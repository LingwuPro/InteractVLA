#!/usr/bin/env bash
set -euo pipefail
benchmark="${1:?libero|libero_plus|robotwin|simpler}"
shift
source "$(dirname "$0")/sim_env.sh" "$benchmark"
default_gpu=0
[[ "$benchmark" != simpler ]] || default_gpu=3
export CUDA_VISIBLE_DEVICES="${GPU:-$default_gpu}"
export MUJOCO_EGL_DEVICE_ID="${CUDA_VISIBLE_DEVICES%%,*}"
case "$benchmark" in
  libero|libero_plus)
    cd "$ASTRA_RUN"
    exec python "$ASTRA_REPO/deployment/astra/smoke_libero.py" \
      --output "$ASTRA_RUN/rollouts/$benchmark" "$@"
    ;;
  robotwin)
    export PYTHONPATH="$KEYPOINT_FLOW_WORKSPACE:$SIM_ROOT:$ASTRA_WORKSPACE/mr05_memory:$OPENPI_ROOT/packages/openpi-client/src"
    cd "$SIM_ROOT"
    exec python "$ASTRA_REPO/deployment/astra/smoke_robotwin.py" \
      --output "$ASTRA_RUN/rollouts/$benchmark" "$@"
    ;;
  simpler|simpler_ms3)
    cd "$SIM_ROOT"
    exec python "$ASTRA_REPO/deployment/astra/smoke_simpler.py" \
      --backend "$benchmark" --output "$ASTRA_RUN/rollouts/$benchmark" "$@"
    ;;
esac

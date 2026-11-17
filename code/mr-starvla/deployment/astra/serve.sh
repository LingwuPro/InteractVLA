#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
model="${1:-starvla}"
export CUDA_VISIBLE_DEVICES="${GPU:-0}"
astra_offline
case "$model" in
  starvla)
    astra_activate "$STARVLA_ENV"
    export PYTHONPATH="$KEYPOINT_FLOW_WORKSPACE:$ASTRA_WORKSPACE/mr05_memory:$ASTRA_WORKSPACE/conda_env/mr_starvla_tools/lib/python3.11/site-packages"
    cd "$ASTRA_WORKSPACE/mr05_memory"
    exec python examples/RoboTwinMem/eval_files/server.py --checkpoint "$STARVLA_CHECKPOINT" --port "${PORT:-18100}"
    ;;
  pi05_libero|pi05_base)
    astra_activate "$PI05_ENV"
    export XLA_PYTHON_CLIENT_PREALLOCATE=false
    cuda_libs="$(find "$PI05_ENV/lib/python3.11/site-packages/nvidia" -maxdepth 2 -type d -name lib | paste -sd:)"
    export LD_LIBRARY_PATH="$cuda_libs:${LD_LIBRARY_PATH:-}"
    export PATH="$PI05_ENV/lib/python3.11/site-packages/nvidia/cuda_nvcc/bin:$PATH"
    config=pi05_libero
    if [[ "$model" == pi05_base ]]; then
      config=pi05_aloha_pen_uncap
      assets_cache="$OPENPI_DATA_HOME/openpi-assets/checkpoints/pi05_base/assets"
      mkdir -p "$assets_cache"
      cp -an "$ASTRA_RUN/models/checkpoints/pi05_base/assets/trossen" "$assets_cache/"
    fi
    cd "$OPENPI_ROOT"
    exec python scripts/serve_policy.py --port "${PORT:-18101}" policy:checkpoint \
      --policy.config "$config" --policy.dir "$ASTRA_RUN/models/checkpoints/$model"
    ;;
  *) echo "Unknown model: $model" >&2; exit 2 ;;
esac

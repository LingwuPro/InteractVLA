#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
benchmark="${1:?benchmark required}"
case "$benchmark" in
  libero)
    astra_activate "${LIBERO_ENV:-$ASTRA_WORKSPACE/envs/libero}"
    SIM_ROOT="${LIBERO_ROOT:-$ASTRA_RUN/repos/LIBERO-master}"
    export LIBERO_CONFIG_PATH="$ASTRA_RUN/config/libero" MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
    ;;
  libero_plus)
    astra_activate "${LIBERO_PLUS_ENV:-$ASTRA_WORKSPACE/envs/libero_plus}"
    SIM_ROOT="${LIBERO_PLUS_ROOT:-$ASTRA_RUN/repos/LIBERO-plus}"
    export LIBERO_CONFIG_PATH="$ASTRA_RUN/config/libero_plus" MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
    ;;
  robotwin)
    astra_activate "$ASTRA_WORKSPACE/conda_env/robotwin"
    SIM_ROOT="$ASTRA_WORKSPACE/mr-starvla-deps/RoboTwin"
    ;;
  simpler)
    astra_activate "$ASTRA_WORKSPACE/conda_env/simpler_env"
    SIM_ROOT="$ASTRA_WORKSPACE/mr-starvla-deps/SimplerEnv"
    ;;
  simpler_ms3)
    astra_activate "$ASTRA_WORKSPACE/conda_env/robomme"
    SIM_ROOT="$ASTRA_RUN"
    export MS_ASSET_DIR="$ASTRA_RUN/assets/maniskill"
    ;;
  *) echo "Unknown benchmark: $benchmark" >&2; return 2 ;;
esac
astra_offline
export PYTHONPATH="$KEYPOINT_FLOW_WORKSPACE:$SIM_ROOT:$ASTRA_REPO:$OPENPI_ROOT/packages/openpi-client/src"
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export VK_ICD_FILENAMES="$ASTRA_RUN/config/nvidia_icd.json"
if [[ "$benchmark" == simpler_ms3 ]]; then
    export VK_ICD_FILENAMES="$ASTRA_RUN/config/lavapipe_icd.json" LP_NUM_THREADS=4
fi
export __EGL_VENDOR_LIBRARY_FILENAMES="$ASTRA_RUN/config/nvidia_egl.json"
export TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1
export NUMBA_CACHE_DIR="$ASTRA_RUN/cache/numba"
export TORCH_EXTENSIONS_DIR="$TMPDIR/torch_extensions"
export ROBOTWIN_SAVE_ROOT="$ASTRA_RUN/rollouts/robotwin"

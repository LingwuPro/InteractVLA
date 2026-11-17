#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
astra_activate "$PI05_ENV"
python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["ASTRA_RUN"])
sources = {
    "libero": root / "repos/LIBERO-master",
    "libero_plus": Path(os.environ.get("LIBERO_PLUS_ROOT", str(root / "repos/LIBERO-plus"))),
}
for benchmark, source in sources.items():
    package = source / "libero/libero"
    config = {"benchmark_root": str(package), "bddl_files": str(package / "bddl_files"),
              "init_states": str(package / "init_files"), "assets": str(package / "assets"),
              "datasets": str(root / "datasets" / benchmark)}
    for key in ("bddl_files", "init_states", "assets"):
        if not Path(config[key]).is_dir():
            raise FileNotFoundError(config[key])
    target = root / "config" / benchmark
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.yaml").write_text(json.dumps(config, indent=2) + "\n")
drivers = {
    "nvidia_icd": {"api_version": "1.3.204", "library_path": "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0"},
    "nvidia_egl": {"library_path": "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0"},
    "lavapipe_icd": {"api_version": "1.1.255", "library_path": str(root / "mesa/usr/lib/x86_64-linux-gnu/libvulkan_lvp.so")},
}
for name, driver in drivers.items():
    if not Path(driver["library_path"]).is_file():
        raise FileNotFoundError(driver["library_path"])
    (root / "config" / (name + ".json")).write_text(json.dumps({"file_format_version": "1.0.0", "ICD": driver}) + "\n")
print("Local simulation configuration written and asset directories checked.")
PY

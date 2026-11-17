#!/usr/bin/env bash
set -eo pipefail
source "$(dirname "$0")/env.sh"
if [[ ! -x "$PI05_ENV/bin/python" ]]; then
    source /workspace/external/REDACTED_PATH
    conda create --copy --prefix "$PI05_ENV" python=3.11 pip -y
fi
astra_activate "$PI05_ENV"
astra_proxy
export NO_PROXY="internal.example.invalid,internal.example.invalid,localhost,127.0.0.1"
export no_proxy="$NO_PROXY"
export PIP_INDEX_URL=https://pypi.org/simple
mkdir -p "$ASTRA_RUN/tools"
ln -sf "$ASTRA_WORKSPACE/conda_env/robomme/bin/git" "$ASTRA_RUN/tools/git"
export PATH="$ASTRA_RUN/tools:$PATH"
test -f "$OPENPI_ROOT/pyproject.toml"
python -m pip install -r "$ASTRA_REPO/deployment/astra/pi05-requirements.lock.txt"
python -m pip check
python "$ASTRA_REPO/deployment/astra/download_pi05.py" --root "$ASTRA_RUN/models"

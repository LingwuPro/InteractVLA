"""Launch one prepared physical test, keeping the frozen model on the local host."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import sys
from pathlib import Path


# Prepared plan -> one isolated physical episode using the existing local model adapter.
def main() -> None:
    os.umask(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    sys.path.insert(0, plan["root"])
    if plan["source_trial"]["source_run"] == "Sol":
        module = importlib.import_module("run_eval")
    else:
        module = importlib.import_module("astra_retry").runner()
    options = argparse.Namespace(
        slot=plan["slot"],
        attempt=plan["attempt"],
        tasks=plan["task"],
        label=plan["label"],
        strategy=plan["strategy"],
        render_stride=64,
        only_failures=False,
        wait_native=False,
        resume_log=False,
        attach_live=False,
    )
    password = getpass.getpass("SSH password (memory only): ")
    module.campaign(options, password)


if __name__ == "__main__":
    main()

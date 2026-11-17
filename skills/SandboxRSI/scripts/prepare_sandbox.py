"""Prepare an isolated single-case sandbox from a recorded implementation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from release_paths import resolve_path as _release_path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(_release_path('/workspace'))
NEW_NAME = "robodojo_astra_12case_20261022"
SOL_NAME = "robodojo_agent6_20261021"
OLD_NAME = "robodojo_astra_retry10_20261021"
RUNTIME_FILES = [
    "geometry_grasps.json",
    "robot_joint_limits.json",
    "tube_rack_geometry.json",
    "best_prior_actions.json",
    "key_demo_template.json",
    "toaster_button_geometry.json",
    "nut_thread_demo.json",
]


# Task-local recorded attempt -> a fresh clone and an explicit launch plan; no simulator starts.
def main() -> None:
    os.umask(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--episode", required=True, type=int)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--slot", type=int, default=0, choices=range(14))
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        parser.error("Destination must be new; historical experiments must remain intact")
    if "work_dirs" not in destination.parts:
        parser.error("Choose a destination under work_dirs")
    rows: list[dict[str, Any]] = json.loads((ROOT / "evidence/attempt_index.json").read_text())
    row = next(
        r
        for r in rows
        if r["base_task"] == args.task and r["episode"] == args.episode and r["try_number"] == args.attempt
    )
    if row["direct_calls"] and not row["source_hashes"]:
        parser.error("This early trial has no recorded source hash; inspect its full numeric transcript instead")
    destination.mkdir(parents=True)
    (destination / "RoboDojo").symlink_to(BASE / "RoboDojo", target_is_directory=True)
    work = destination / "mr-starvla/work_dirs/keypoint_flow"
    source_run = {"Sol": SOL_NAME, "Astra": OLD_NAME, "Extension": NEW_NAME}[row["source_run"]]
    for name in [SOL_NAME, OLD_NAME, NEW_NAME]:
        source = ROOT / "sources/campaigns" / name
        target = work / name
        (target / "runtime").mkdir(parents=True)
        for file in source.glob("*.py"):
            shutil.copyfile(file, target / file.name)
        for file_name in RUNTIME_FILES:
            file = source / "runtime" / file_name
            if file.exists():
                shutil.copyfile(file, target / "runtime" / file_name)
        shutil.copytree(source / "runtime/experience", target / "runtime/experience")
        if (source / "runtime/remote-bin").exists():
            shutil.copytree(source / "runtime/remote-bin", target / "runtime/remote-bin")
        (target / ".venv").symlink_to(
            BASE / "mr-starvla/work_dirs/keypoint_flow" / NEW_NAME / ".venv", target_is_directory=True
        )
    for key, name in [("strategy_source_sha256", SOL_NAME), ("astra_strategy_sha256", source_run)]:
        if key in row["source_hashes"]:
            shutil.copyfile(
                ROOT / "sources/versions" / f"agent_{row['source_hashes'][key]}.txt", work / name / "agent.py"
            )
    case = json.loads((ROOT / "evidence/case_index.json").read_text())[row["case_id"]]
    manifest = json.loads((ROOT / "evidence/campaigns" / NEW_NAME / "manifest.json").read_text())
    manifest.update(
        cases=[case],
        additional_completed_cases=[],
        extension_case_ids=[case["case_id"]],
        parts=[
            {
                "task": case["task"],
                "base_task": case["base_task"],
                "dimension": case["dimension"],
                "layout_ids": [case["layout_id"]],
                "episodes": 1,
            }
        ],
    )
    target = work / source_run
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(ROOT / "evidence/campaigns" / NEW_NAME / "protocol.json", target / "protocol.json")
    plan = {
        "source_trial": {k: row[k] for k in ["case_id", "episode", "try_number", "source_run", "attempt"]},
        "root": str(target),
        "task": case["task"],
        "slot": args.slot,
        "strategy": "direct" if row["source_hashes"] else "native",
        "attempt": row["attempt"],
        "label": "sandboxrsi_reproduction",
        "source_hashes": row["source_hashes"],
        "note": "Recorded agent code with final transport bridge; fresh physics execution, not bitwise replay.",
    }
    path = destination / "plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n")
    for file in destination.rglob("*"):
        if not file.is_symlink():
            file.chmod(0o777)
    print(json.dumps({"plan": str(path), "simulator_started": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()

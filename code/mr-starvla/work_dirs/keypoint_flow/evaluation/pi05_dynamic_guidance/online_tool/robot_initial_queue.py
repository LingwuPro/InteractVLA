"""Official-reset supplemental LIBERO-plus inventory and native-first evaluation."""

import argparse
import contextlib
import hashlib
import io
import os
import time
from pathlib import Path

from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.online_tool import (
    agent_queue,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.online_tool.run_full import (
    CHECKPOINT,
    USAGE,
    read_json,
    save_json,
    trial,
)

SUITES = ("libero_spatial", "libero_object", "libero_goal", "libero_10")


# Existing immutable scope and official task metadata -> disjoint supplemental manifest.
def prepare(args):
    destination = args.output / "manifest.json"
    if destination.exists():
        raise FileExistsError(destination)
    previous = read_json(args.existing / "manifest.json")
    covered = {(row["case"]["suite"], row["case"]["task_name"]) for row in previous["cases"]}
    classification_path = Path(benchmark.__file__).parent / "task_classification.json"
    classification = read_json(classification_path)
    rows, counts, all_keys = [], {}, set()
    for name in SUITES:
        with contextlib.redirect_stdout(io.StringIO()):
            suite = benchmark.get_benchmark_dict()[name]()
        categories = {row["name"]: row["category"] for row in classification[name]}
        counts[name] = 0
        for index in range(suite.get_num_tasks()):
            task = suite.get_task(index)
            key = (name, task.name)
            all_keys.add(key)
            if key in covered:
                continue
            category = categories[task.name]
            if category != "Robot Initial States":
                raise ValueError(f"Unexpected omitted category: {category}")
            case = {"suite": name, "task_index": index, "task_name": task.name,
                    "category": category, "seed": 0, "demo_id": 0,
                    "benchmark": "libero_plus", "case_id": f"{name}-{index:04d}-demo0-seed0"}
            rows.append({"case": case, "baseline": None, "reset_protocol": "official_robot_variant_v1",
                         "historical_native_records": 0, "historical_sources": []})
            counts[name] += 1
    added = {(row["case"]["suite"], row["case"]["task_name"]) for row in rows}
    assert not covered & added and covered | added == all_keys
    assert len(added) == len(rows)
    client = PolicyClient("127.0.0.1", args.port)
    try:
        policy = client.metadata
    finally:
        client.close()
    assert policy["config"] == "pi05_libero" and Path(policy["checkpoint"]).resolve() == CHECKPOINT
    assert policy["action_horizon"] == policy["num_steps"] == 10
    manifest = {"cases": rows, "policy": policy, "existing_scope": str(args.existing),
                "supplement_counts": counts, "combined_total": len(all_keys),
                "initial_index": 0, "reset_protocol": "official_robot_variant_v1",
                "reset_boundary": "Official object state 0 plus declared robot init_qpos; no dataset access.",
                "legacy_snapshot_label": "initial_state_file is an isolated official reset, not a demonstration reset",
                "strategy": "native attempts 0 and 1 first; failures wait for explicit assistant commands",
                "automatic_recovery": False, "recovery_attempt": 2,
                "classification_sha256": hashlib.sha256(classification_path.read_bytes()).hexdigest(),
                "code": Path(__file__).read_text()}
    save_json(destination, manifest)
    agent_queue.summarize(args.output)
    print({"combined_total": len(all_keys), "new_cases": len(rows), "counts": counts}, flush=True)


# Supplemental rows -> all native baselines; failed cases remain nonterminal for live recovery.
def baseline(args):
    manifest = read_json(args.output / "manifest.json")
    failed = []
    for index, row in enumerate(manifest["cases"]):
        case = row["case"]
        folder = args.output / "cases" / case["case_id"]
        if (folder / "task.json").exists():
            continue
        records = []
        solved = False
        for attempt in (0, 1):
            save_json(args.output / "latest.json",
                      {"status": "native_trials", "pid": os.getpid(), "case_id": case["case_id"],
                       "attempt": attempt, "case_index": index, "timestamp_unix": time.time()})
            result = trial(row, args.output, args.port, attempt, "native", manifest["policy"])
            solved = bool(result["success"])
            records.append({"attempt": attempt, "success": solved,
                            "result_file": str(folder / f"native-{attempt}/result.json")})
            if solved:
                break
        if solved:
            save_json(folder / "task.json",
                      {"case": case, "status": "completed", "success": True,
                       "resolution": "new_native_success", "native_trials": records,
                       "keypoint_reasoning": [], "tool_calls": [{"tool": "pi05_native", **r} for r in records],
                       "token_usage": USAGE})
        else:
            failed.append(case["case_id"])
            save_json(folder / "native_failures.json",
                      {"case_id": case["case_id"], "native_trials": records, "status": "awaiting_assistant"})
        save_json(args.output / "baseline-progress.json",
                  {"processed_this_pass": index + 1, "total": len(manifest["cases"]),
                   "failed_case_ids": failed, "timestamp_unix": time.time()})
        if (index + 1) % 20 == 0:
            agent_queue.summarize(args.output)
    agent_queue.summarize(args.output)
    save_json(args.output / "latest.json",
              {"status": "baseline_complete", "pid": os.getpid(),
               "awaiting_assistant": len(failed), "timestamp_unix": time.time()})


# CLI paths and stage -> immutable inventory, native sweep, or interactive recovery queue.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "baseline", "recover", "summary"))
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18414)
    args = parser.parse_args()
    args.existing, args.output = args.existing.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    args.source = args.output
    if args.stage == "prepare":
        prepare(args)
    elif args.stage == "baseline":
        baseline(args)
    elif args.stage == "recover":
        agent_queue.run(args)
    else:
        agent_queue.summarize(args.output)


if __name__ == "__main__":
    main()


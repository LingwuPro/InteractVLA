"""Resume historical native successes and evaluate sensor-only recovery on the full manifest."""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark, get_libero_path
from robosuite.utils.errors import RandomizationError
from starVLA.keypoint_flow.evaluate_gt_libero import log
from starVLA.keypoint_flow.evaluate_gt_libero import save_json as save_atomic_json
from starVLA.keypoint_flow.gt_libero import transfer_demo_state
from starVLA.keypoint_flow.protocol import manifest_hash
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.native_finish_caps.render_probe.fast_glass import (
    FastGlassEnv,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.online_tool.tool import (
    Session,
)
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.evaluate import serializable
from work_dirs.keypoint_flow.evaluation.pi05_native_noise.evaluate import BUDGETS

ROOT = Path(__file__).resolve().parent
DYNAMIC = ROOT.parent
CHECKPOINT = (DYNAMIC.parents[2] / "astra_deploy/models/checkpoints/pi05_libero").resolve()
USAGE = {"input_tokens": None, "output_tokens": None, "total_tokens": None,
         "status": "unavailable", "reason": "No exact per-task session token counter is exposed."}


# JSON record -> atomically published file with its parent directory created.
def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic_json(path, value)


# JSON file -> object, without accessing trajectory or execution arrays.
def read_json(path):
    return json.loads(Path(path).read_text())


# Historical result path -> completed native result or excluded record.
def read_baseline(path):
    record = read_json(path)
    if (record.get("status") != "completed" or record.get("mode") != "original"
            or record.get("prediction_horizon") != 10 or record.get("execution_horizon") != 10
            or record.get("render_validation") != "both_RGB_cameras_every_observation_v1"
            or not 0 < record["steps"] <= BUDGETS[record["suite"]]):
        return None
    return {"result": record, "source": str(path.resolve())}


# CLI paths -> frozen 8,480-case native-result inventory and no-execution success task records.
def prepare(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "manifest.json"
    if destination.exists():
        raise FileExistsError("Frozen manifest exists; use run or summary to resume it")
    cases = read_json(DYNAMIC / "positive_consolidation/full-cases.json")["cases"]
    roots = [DYNAMIC / "local_progress_full", DYNAMIC / "recorded_source_full"]
    remote = DYNAMIC / "eight_gpu_20261014/runs/REDACTED_HOST/full"
    roots.extend(remote / name for name in ("alignment_strength", "budget_schedule_matching"))
    source_manifests = []
    by_case = {}
    for root in roots:
        launch = read_json(root / "launch.json")
        source_manifests.append({"root": str(root), "launch": launch})
        paths = sorted(root.glob("*/original/result.json"))
        with ThreadPoolExecutor(max_workers=12) as pool:
            for value in pool.map(read_baseline, paths):
                if value is not None:
                    by_case.setdefault(value["result"]["case_id"], []).append(value)
        log({"event": "indexed_native_results", "source": str(root), "cases": len(by_case)})
    rows = []
    disagreements = 0
    for case in cases:
        candidates = by_case.get(case["case_id"], [])
        for candidate in candidates:
            result = candidate["result"]
            if any(result[key] != case[key] for key in ("suite", "task_index", "seed", "demo_id", "task_name")):
                raise ValueError(f"Historical case identity mismatch: {case['case_id']}")
        hashes = {candidate["result"]["initial_state_hash"] for candidate in candidates}
        if len(hashes) > 1:
            raise ValueError(f"Historical reset mismatch: {case['case_id']}")
        successes = [candidate for candidate in candidates if candidate["result"]["success"]]
        chosen = successes[0] if successes else candidates[0] if candidates else None
        disagreements += bool(successes) and len(successes) < len(candidates)
        row = {"case": case, "baseline": chosen, "historical_native_records": len(candidates),
               "historical_successes": len(successes),
               "historical_sources": [candidate["source"] for candidate in candidates]}
        rows.append(row)
        if successes:
            report = {"case": case, "status": "completed", "success": True, "reused_success": True,
                      "historical_native_records": len(candidates), "native_at_most_2": True,
                      "native_at_most_3": True, "vla2_then_rgbd": True, "baseline": chosen,
                      "new_rollouts": 0, "keypoint_reasoning": [],
                      "tool_calls": [{"tool": "pi05_native", "status": "success",
                                      "execution_source": "previous_verified_execution",
                                      "result_file": chosen["source"]}],
                      "token_usage": USAGE}
            save_json(output / "cases" / case["case_id"] / "task.json", report)
    metadata = PolicyClient("127.0.0.1", args.port)
    try:
        policy = metadata.metadata
    finally:
        metadata.close()
    if (policy["config"] != "pi05_libero" or policy["action_horizon"] != 10
            or policy["num_steps"] != 10 or Path(policy["checkpoint"]).resolve() != CHECKPOINT):
        raise ValueError("Policy does not match the historical official pi05_libero H10/N10 protocol")
    catalog = {}
    for family, source in read_json(ROOT / "scene-camera-calibrations.json").items():
        target = output / "calibrations" / f"{family}.json"
        save_json(target, read_json(source))
        catalog[family] = str(target)
    save_json(output / "camera-calibration-catalog.json", catalog)
    manifest = {"cases": rows, "policy": policy, "sources": source_manifests,
                "strategy": "historical native success reuse; native <=2 then scene-calibrated RGB-D",
                "comparison": "same attempt-2 noise: third native versus RGB-D; historical successes shared",
                "historical_disagreements": disagreements,
                "baseline_semantics": "Any verified historical native success per user request; "
                                      "not single-shot accuracy",
                "reset_boundary": "Evaluation setup only: initial_state NPZ member or HDF5 states[0] and reset XML; "
                                  "never read actions, poses, observations, or trajectory points",
                "scene_calibration_catalog": catalog,
                "source_code": {name: {"sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest(),
                                       "text": (ROOT / name).read_text()} for name in ("tool.py", "run_full.py")}}
    save_json(destination, manifest)
    summarize(output)


# Official object reset and robot variant -> opaque state [S] with the declared arm initialization retained.
def prepare_robot_reset(case, output):
    path = output / "resets" / f"{case['case_id']}.npy"
    if path.exists():
        return path
    with contextlib.redirect_stdout(io.StringIO()):
        suite = benchmark.get_benchmark_dict()[case["suite"]]()
    task = suite.get_task(case["task_index"])
    np.random.seed(case["seed"])
    env = FastGlassEnv(
        bddl_file_name=str(Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file),
        camera_heights=224, camera_widths=224, control_freq=20,
        use_camera_obs=False, has_renderer=False, has_offscreen_renderer=False,
    )
    try:
        env.seed(case["seed"])
        env.reset()
        official = np.asarray(suite.get_task_init_states(case["task_index"])[0])
        env.set_init_state(official)
        robot = env.env.robots[0]
        restored = env.env.sim.get_state().flatten().copy()
        before = env.env.sim.data.qpos[robot._ref_joint_pos_indexes].copy()
        expected = robot.robot_model.init_qpos.copy()
        env.env.sim.data.qpos[robot._ref_joint_pos_indexes] = expected
        env.env.sim.data.qvel[robot._ref_joint_vel_indexes] = 0
        env.env.sim.forward()
        initial = env.env.sim.get_state().flatten().copy()
        np.testing.assert_array_equal(env.env.sim.data.qpos[robot._ref_joint_pos_indexes], expected)
        unchanged = np.ones(initial.shape, dtype=bool)
        unchanged[1 + np.asarray(robot._ref_joint_pos_indexes)] = False
        unchanged[1 + env.env.sim.model.nq + np.asarray(robot._ref_joint_vel_indexes)] = False
        np.testing.assert_array_equal(initial[unchanged], restored[unchanged])
        audit = {"protocol": "official_robot_variant_v1", "robot_class": type(robot.robot_model).__name__,
                 "official_state_hash": manifest_hash({"state": official.tolist()}),
                 "prepared_state_hash": manifest_hash({"state": initial.tolist()}),
                 "robot_qpos_before": before.tolist(), "robot_qpos_after": expected.tolist(),
                 "non_arm_state_unchanged": True,
                 "reason": "Full official state overwrites variant init_qpos; restore only the robot arm qpos/velocity.",
                 "object_state_source": "Unmodified opaque official benchmark initial state index 0"}
    finally:
        env.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".partial").open("wb") as stream:
        np.save(stream, initial, allow_pickle=False)
    path.with_suffix(".partial").replace(path)
    save_json(path.with_suffix(".audit.json"), audit)
    return path


# Frozen metadata -> isolated reset [S]; no reset values are provided to planning or policy inputs.
def prepare_reset(row, output):
    case = row["case"]
    if row.get("reset_protocol") == "official_robot_variant_v1":
        if row["baseline"] is not None:
            raise ValueError("Official-reset cases cannot reuse historical demonstration resets")
        return prepare_robot_reset(case, output)
    path = output / "resets" / f"{case['case_id']}.npy"
    if path.exists():
        return path
    reference_roots = [DYNAMIC.parent / "pi05_native_noise_plus", DYNAMIC / "local_progress_full",
                       DYNAMIC / "recorded_source_full"]
    if row["baseline"] is not None:
        reference_roots.insert(0, Path(row["baseline"]["source"]).parents[2])
    initial = None
    for root in reference_roots:
        cache = root / case["case_id"] / "reference/reference.npz"
        if cache.exists():
            with np.load(cache, allow_pickle=False) as archive:
                initial = archive["initial_state"]
            break
    if initial is None:
        with h5py.File(case["hdf5"], "r") as dataset:
            demo = dataset[f"data/demo_{case['demo_id']}"]
            initial = np.asarray(demo["states"][0])
            model_xml = demo.attrs["model_file"] if case["category"] == "Objects Layout" else None
        if model_xml is not None:
            with contextlib.redirect_stdout(io.StringIO()):
                suite = benchmark.get_benchmark_dict()[case["suite"]]()
            task = suite.get_task(case["task_index"])
            np.random.seed(case["seed"])
            env = FastGlassEnv(
                bddl_file_name=str(Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file),
                camera_heights=224, camera_widths=224, control_freq=20,
                use_camera_obs=False, has_renderer=False, has_offscreen_renderer=False,
            )
            try:
                env.seed(case["seed"])
                env.reset()
                initial = transfer_demo_state(initial, model_xml,
                                              np.asarray(suite.get_task_init_states(case["task_index"])[0]),
                                              env.env.sim.model)
            finally:
                env.close()
    if row["baseline"] is not None:
        if manifest_hash({"state": initial.tolist()}) != row["baseline"]["result"]["initial_state_hash"]:
            raise ValueError("Opaque reset hash differs from the reused native execution")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".partial").open("wb") as stream:
        np.save(stream, initial, allow_pickle=False)
    path.with_suffix(".partial").replace(path)
    return path


# One reset, strategy and noise attempt -> resumable complete rollout and per-attempt tool report.
def trial(row, output, port, attempt, strategy, policy):
    case = row["case"]
    folder = output / "cases" / case["case_id"] / f"{strategy}-{attempt}"
    marker = folder / "result.json"
    if marker.exists():
        return read_json(marker)
    initial = prepare_reset(row, output)
    args = SimpleNamespace(output=folder, suite=case["suite"], task_index=case["task_index"],
                           seed=case["seed"], initial_index=0, port=port, max_goal_distance=0.25,
                           residual_position=0.10, residual_rotation=1.2, residual_gripper=0.04,
                           initial_state_file=initial, case_id=case["case_id"], attempt=attempt,
                           historical_noise_seed=True)
    session = Session(args)
    try:
        for key in ("checkpoint", "config", "action_horizon", "num_steps"):
            if session.client.metadata[key] != policy[key]:
                raise ValueError(f"Policy metadata changed: {key}")
        command = {"chunks": 60}
        if strategy == "rgbd":
            command["camera_calibration_catalog"] = str(output / "camera-calibration-catalog.json")
            command["reasoning_summary"] = "Two native failures; reproject visible RGB-D into the calibrated "
            command["reasoning_summary"] += "nominal scene camera. No object coordinates or GT keypoints used."
        result = session.vla(command)
        result["observation"] = session.snapshot()
        result["task_report"] = str(session.video_path.with_suffix(".task.json"))
        result["strategy"] = strategy
        save_json(marker, serializable(result))
        return result
    finally:
        session.close()


# One case -> native retries, equal-noise counterfactual and sensor-only recovery, with separate budgets.
def evaluate_case(row, args, policy):
    case = row["case"]
    folder = args.output / "cases" / case["case_id"]
    baseline = row["baseline"]
    traces = []
    native0 = baseline["result"]["success"] if baseline else None
    trials = {}
    if native0 is None:
        result = trial(row, args.output, args.port, 0, "native", policy)
        native0 = result["success"]
        trials["native_0"] = native0
        traces.append(result["task_report"])
    solved = native0
    if not solved:
        result = trial(row, args.output, args.port, 1, "native", policy)
        solved = result["success"]
        trials["native_1"] = solved
        traces.append(result["task_report"])
    native3, recovery = solved, solved
    if not solved:
        native = trial(row, args.output, args.port, 2, "native", policy)
        rgbd = trial(row, args.output, args.port, 2, "rgbd", policy)
        native3, recovery = native["success"], rgbd["success"]
        trials.update(native_2=native3, rgbd_2=recovery)
        traces.extend((native["task_report"], rgbd["task_report"]))
        if native["observation"]["initial_state_sha256"] != rgbd["observation"]["initial_state_sha256"]:
            raise ValueError("Counterfactual reset mismatch")
        for left, right in zip(native["chunks"], rgbd["chunks"], strict=False):
            if left["details"]["seed"] != right["details"]["seed"]:
                raise ValueError("Counterfactual sampling seed mismatch")
    reports = [read_json(path) for path in traces]
    calls = [call for report in reports for call in report["tool_calls"]]
    if baseline:
        calls.insert(0, {"tool": "pi05_native", "status": "historical_failure",
                         "execution_source": "previous_verified_execution", "result_file": baseline["source"]})
    record = {"case": case, "status": "completed", "success": recovery, "reused_success": False,
              "baseline": baseline, "native_initial_success": native0, "native_at_most_2": solved,
              "native_at_most_3": native3, "vla2_then_rgbd": recovery, "trials": trials,
              "task_reports": traces, "tool_calls": calls, "keypoint_reasoning": [], "token_usage": USAGE,
              "new_rollouts": len(traces), "counterfactual_is_diagnostic": True,
              "planning_summary": "Frozen scene-calibrated RGB-D recovery after two native failures; "
                                  "no automatic semantic rewriting or invented visual keypoints."}
    save_json(folder / "task.json", serializable(record))
    return record


# Durable per-case task files -> all-suite completion and matched recovery statistics.
def summarize(output):
    manifest = read_json(output / "manifest.json")
    files = list((output / "cases").glob("*/task.json"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(read_json, files))
    reused = sum(row["reused_success"] for row in rows)
    summary = {"total_cases": len(manifest["cases"]), "completed_cases": len(rows), "reused_successes": reused,
               "new_evaluated_cases": len(rows) - reused, "pending_cases": len(manifest["cases"]) - len(rows),
               "native_at_most_2": sum(row["native_at_most_2"] for row in rows),
               "native_at_most_3": sum(row["native_at_most_3"] for row in rows),
               "vla2_then_rgbd": sum(row["vla2_then_rgbd"] for row in rows),
               "rescued_vs_third_native": sum(row["vla2_then_rgbd"] and not row["native_at_most_3"] for row in rows),
               "harmed_vs_third_native": sum(row["native_at_most_3"] and not row["vla2_then_rgbd"] for row in rows),
               "new_rollouts": sum(row["new_rollouts"] for row in rows),
               "historical_disagreements": manifest["historical_disagreements"],
               "historical_baseline": manifest["baseline_semantics"],
               "timestamp_unix": time.time(),
               "errors": len(list((output / "cases").glob("*/error.json")))}
    save_json(output / "summary.json", summary)
    log({"event": "full_recovery_progress", **summary})
    return summary


# Frozen case order and shard -> lock-protected resumable worker; engineering errors remain separate.
def run(args):
    manifest = read_json(args.output / "manifest.json")
    for name, source in manifest["source_code"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Frozen implementation changed: {name}")
    rows = [row for row in manifest["cases"] if not (row["baseline"] and row["baseline"]["result"]["success"])]
    rows.sort(key=lambda row: row["baseline"] is None)
    completed = 0
    for row in rows[args.shard::args.shards]:
        folder = args.output / "cases" / row["case"]["case_id"]
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "task.json").exists():
            continue
        with (folder / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            save_json(args.output / f"worker-{args.shard}-status.json",
                      {"pid": os.getpid(), "case": row["case"]["case_id"], "timestamp_unix": time.time()})
            try:
                result = evaluate_case(row, args, manifest["policy"])
                (folder / "error.json").unlink(missing_ok=True)
                log({"event": "case_complete", "case": row["case"]["case_id"],
                     "success": result["success"], "trials": result["trials"]})
            except Exception as error:
                save_json(folder / "error.json", {"status": "engineering_error", "error": str(error),
                                                   "traceback": traceback.format_exc()})
                log({"event": "engineering_error", "case": row["case"]["case_id"], "error": str(error)})
                if not isinstance(error, RandomizationError):
                    raise
        completed += 1
        if completed % 10 == 0 or args.limit:
            summarize(args.output)
        if args.limit and completed >= args.limit:
            break
    summarize(args.output)


# CLI -> prepare frozen inventory, run a resumable shard, or summarize existing completed tasks.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "run", "summary"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18414)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.stage == "prepare":
        prepare(args)
    elif args.stage == "run":
        run(args)
    else:
        summarize(args.output)


if __name__ == "__main__":
    main()

"""Paired original/native-keypoint/pose-keypoint noise experiments using official LIBERO actions."""

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

import h5py
import imageio.v2 as imageio
import numpy as np
from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark
from scipy.spatial.transform import Rotation
from starVLA.keypoint_flow.evaluate_gt_libero import (
    make_env,
    replay_reference,
    save_json,
)
from starVLA.keypoint_flow.geometry import interpolate
from starVLA.keypoint_flow.libero_control import measured_base_pose

ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT.parent / "pi05_50k_gt_full"
MODES = ("original", "native_keypoints", "pose_keypoints")
BUDGETS = {"libero_spatial": 220, "libero_object": 280, "libero_goal": 300, "libero_10": 520}
KEY_STEPS = np.array([2, 5, 8, 10])


# JSON event -> timestamped progress, with execution events emitted every ten steps.
def log(record: dict) -> None:
    print(json.dumps({"timestamp": datetime.now().astimezone().isoformat(), **record}), flush=True)


# Reference actions [T,7], completed ticks, previous executed action [7] -> sparse-keypoint sketch [10,7].
def native_sketch(actions: np.ndarray, completed: int, previous: np.ndarray) -> np.ndarray:
    indices = completed + KEY_STEPS - 1
    values = actions[np.minimum(indices, len(actions) - 1)].copy()
    values[indices >= len(actions), :6] = 0
    times = np.concatenate(([0], KEY_STEPS))
    controls = np.concatenate((previous[None], values))
    return np.stack([np.interp(np.arange(1, 11), times, controls[:, channel]) for channel in range(7)], axis=-1)


# Current and GT base poses [1,8], [T+1,1,8] -> interpolated physical poses [10,1,8].
def pose_sketch(poses: np.ndarray, completed: int, current: np.ndarray) -> np.ndarray:
    result = np.empty((10, 1, 8))
    previous_step, previous_pose = 0, current
    for step in KEY_STEPS:
        target = poses[min(completed + int(step), len(poses) - 1)]
        fractions = np.arange(1, step - previous_step + 1) / (step - previous_step)
        result[previous_step:step] = interpolate(previous_pose, target, fractions)
        previous_step, previous_pose = int(step), target
    return result


# Base-frame desired poses [H,1,8] and current [1,8] -> approximate native OSC deltas [H,7], not inverse dynamics.
def pose_to_actions(poses: np.ndarray, current: np.ndarray, base_rotation: Rotation, controller) -> tuple:
    chain = np.concatenate((current[None], poses))[:, 0]
    positions = base_rotation.apply(np.diff(chain[:, :3], axis=0))
    rotations = base_rotation * Rotation.from_quat(chain[:, 3:7])
    angles = (rotations[1:] * rotations[:-1].inv()).as_rotvec()
    physical = np.concatenate((positions, angles), axis=-1)
    scale = (np.asarray(controller.output_max) - controller.output_min) / (
        np.asarray(controller.input_max) - controller.input_min
    )
    input_mid = (np.asarray(controller.input_max) + controller.input_min) / 2
    output_mid = (np.asarray(controller.output_max) + controller.output_min) / 2
    raw = (physical - output_mid) / scale + input_mid
    clipped = np.clip(raw, controller.input_min, controller.input_max)
    reconstructed = (clipped - input_mid) * scale + output_mid
    unsaturated = np.all(raw == clipped, axis=1)
    if unsaturated.any() and not np.allclose(reconstructed[unsaturated], physical[unsaturated], atol=1e-8):
        raise ValueError("OSC action-scale inversion failed")
    widths = np.diff(chain[:, 7])
    gripper = np.where(np.abs(widths) > 1e-4, -np.sign(widths), 0)
    actions = np.concatenate((clipped, gripper[:, None]), axis=-1).astype(np.float32)
    diagnostics = {"saturated_components": int(np.count_nonzero(raw != clipped)),
                   "component_count": int(raw.size), "gripper": "negative_width_change_closes_1e-4m_deadband"}
    return actions, diagnostics


# One paired task, frozen GT, and sampling mode -> success, exact noise/action traces, and replay video.
def rollout(case: dict, task, poses: np.ndarray, initial: np.ndarray, actions: np.ndarray,
            reference: dict, mode: str, client: PolicyClient, output: Path, verify_warmstart: bool = False) -> dict:
    folder = output / case["case_id"] / mode
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "result.json"
    if marker.exists():
        return json.loads(marker.read_text())
    env = make_env(task, case["seed"], absolute=False)
    video = imageio.get_writer(str(folder / "policy.mp4"), fps=20)
    before = time.monotonic()
    try:
        observation = env.set_init_state(initial)
        if not np.array_equal(env.env.sim.get_state().flatten(), initial):
            raise ValueError("Paired initial state changed")
        robot = env.env.robots[0]
        base_position = np.asarray(reference["base_position"])
        base_rotation = Rotation.from_matrix(reference["base_rotation"])
        controller = robot.controller
        if not controller.use_delta or controller.impedance_mode != "fixed":
            raise ValueError("Expected native fixed-impedance delta OSC")
        controller_spec = {name: np.asarray(getattr(controller, name)).tolist()
                           for name in ("input_min", "input_max", "output_min", "output_max")}
        completed, success = 0, False
        previous = np.zeros(7)
        requests, generated, noises, sketches, measured = [], [], [], [], []
        while completed < BUDGETS[case["suite"]] and not success:
            current = measured_base_pose(observation, base_position, base_rotation)
            conversion = {}
            sketch = np.zeros((10, 7), dtype=np.float32)
            if mode == "native_keypoints":
                sketch = native_sketch(actions, completed, previous)
            elif mode in ("pose_keypoints", "pose_warmstart", "pose_standardized"):
                sketch, conversion = pose_to_actions(pose_sketch(poses, completed, current),
                                                     current, base_rotation, controller)
            seed_text = f"{case['case_id']}:{completed}".encode()
            seed = int.from_bytes(hashlib.sha256(seed_text).digest()[:4], "little")
            payload = {
                "observation/image": np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]),
                "observation/wrist_image": np.ascontiguousarray(observation["robot0_eye_in_hand_image"][::-1, ::-1]),
                "observation/state": np.concatenate((observation["robot0_eef_pos"],
                    Rotation.from_quat(observation["robot0_eef_quat"]).as_rotvec(),
                    observation["robot0_gripper_qpos"])).astype(np.float32),
                "prompt": task.language, "sketch": sketch.astype(np.float32),
                "experiment": {"mode": mode, "seed": seed, "sigma": 0.3,
                               "verify_original": verify_warmstart and mode == "pose_warmstart" and completed == 0},
            }
            result = client.infer(payload)
            chunk = np.asarray(result["actions"])
            if chunk.shape != (10, 7) or not np.isfinite(chunk).all():
                raise ValueError("Official sampler returned invalid actions")
            details = result["experiment"]
            if mode == "original" and completed == 0 and case["task_index"] == 0:
                repeated = client.infer(payload)
                if not np.array_equal(chunk, repeated["actions"]):
                    raise ValueError("Explicit-noise sampling is not reproducible")
            generated.append(chunk)
            noises.append(details["initial_noise"])
            sketches.append(details["normalized_sketch"])
            requests.append({"step": completed, "seed": seed, "keypoint_steps": KEY_STEPS.tolist(),
                             "raw_sketch": sketch.tolist(), "conversion": conversion,
                             "normalization_roundtrip_max_error": details["normalization_roundtrip_max_error"]})
            if mode == "pose_warmstart":
                requests[-1]["warmstart"] = {key: value for key, value in details.items()
                                            if key not in ("initial_noise", "normalized_sketch")}
            if mode == "pose_standardized":
                requests[-1]["standardization"] = details["standardization"]
            for action in chunk[:min(10, BUDGETS[case["suite"]] - completed)]:
                observation, _, done, _ = env.step(action.tolist())
                previous = action.copy()
                completed += 1
                success = bool(done)
                measured.append(measured_base_pose(observation, base_position, base_rotation))
                video.append_data(np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]))
                if success:
                    break
            log({"case": case["case_id"], "mode": mode, "step": completed, "success": success})
        np.savez_compressed(folder / "execution.npz", actions=np.asarray(generated), noise=np.asarray(noises),
                            normalized_sketch=np.asarray(sketches), measured=np.asarray(measured))
        save_json(folder / "requests.json", {"requests": requests})
        report = {**case, "mode": mode, "success": success, "steps": completed,
                  "budget": BUDGETS[case["suite"]], "reference_success": reference["reference_success"],
                  "reference_steps": len(actions), "initial_state_hash": reference["initial_state_hash"],
                  "controller": controller_spec, "seconds": time.monotonic() - before,
                  "video": str((folder / "policy.mp4").resolve()), "status": "completed"}
        save_json(marker, report)
        return report
    finally:
        video.close()
        env.close()


# Completed per-case results -> atomic mode/suite counts, without treating missing cases as failures.
def summarize(shard: int, output: Path) -> None:
    rows = [json.loads(path.read_text()) for path in output.glob("libero*-demo0-seed0/*/result.json")]
    counts = {}
    for mode in MODES:
        selected = [row for row in rows if row["mode"] == mode]
        counts[mode] = {"completed": len(selected), "successes": sum(row["success"] for row in selected),
                        "by_suite": {suite: {"completed": sum(row["suite"] == suite for row in selected),
                                             "successes": sum(row["success"] and row["suite"] == suite
                                                              for row in selected)} for suite in BUDGETS}}
    save_json(output / f"summary-{shard}.json", {"counts": counts, "cases": rows})


# Resumable paired task inventory -> three sampling modes under the same native simulator interface.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18401)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--benchmark", choices=("libero", "libero_plus"), default="libero")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-at-end", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        parser.error("Invalid shard assignment")
    output = args.output or (ROOT if args.benchmark == "libero" else ROOT.parent / "pi05_native_noise_plus")
    output.mkdir(parents=True, exist_ok=True)
    inventory = json.loads((PREVIOUS / f"inventory-{args.benchmark}.json").read_text())["cases"]
    if any(case["category"] == "Robot Initial States" for case in inventory):
        raise ValueError("Robot-initial perturbations must be excluded")
    selected = inventory
    if args.smoke:
        if args.benchmark == "libero":
            selected = [case for case in inventory if case["task_index"] == 0]
        else:
            categories = {}
            for case in inventory:
                categories.setdefault(case["category"], case)
            selected = list(categories.values())
    cases = [case for index, case in enumerate(selected) if index % args.shards == args.shard]
    client = PolicyClient("127.0.0.1", args.port)
    try:
        if client.metadata["action_horizon"] != 10 or client.metadata["config"] != "pi05_libero":
            raise ValueError("Incorrect official checkpoint configuration")
        manifest = {"server": client.metadata, "sigma": 0.3, "keypoint_steps": KEY_STEPS.tolist(),
                    "modes": MODES, "control_frequency_hz": 20, "replan_steps": 10,
                    "budgets": BUDGETS, "reset": "paired_hdf5_demo0_seed0_no_extra_wait",
                    "guidance": "initial_noise_only_no_added_tokens_no_sampler_guidance",
                    "noise": "first_7_channels_normalized_sketch_plus_0.3epsilon_padding_original_epsilon",
                    "pose_conversion": "world_frame_pose_differences_inverse_OSC_scaling_clipping_gripper_width_sign",
                    "pose_conversion_limit": "kinematic_command_approximation_not_inverse_dynamics",
                    "tail": "native_zero_motion_last_demo_gripper;pose_hold_final_measured_pose",
                    "benchmark": args.benchmark, "task_count": len(inventory),
                    "scope": "one demonstration initial state per task; not standard multi-trial benchmark"}
        marker = output / f"manifest-{args.shard}.json"
        if marker.exists() and json.loads(marker.read_text()) != json.loads(json.dumps(manifest)):
            raise ValueError("Cannot resume an evaluation with different settings")
        save_json(marker, manifest)
        save_json(output / "inventory.json", {"cases": inventory, "count": len(inventory)})
        for case in cases:
            if all((output / case["case_id"] / mode / "result.json").exists() for mode in MODES):
                continue
            save_json(output / f"active-{args.shard}.json", {"case": case, "mode": "reference"})
            with contextlib.redirect_stdout(io.StringIO()):
                suite = benchmark.get_benchmark_dict()[case["suite"]]()
            if suite.get_task(case["task_index"]).name != case["task_name"]:
                raise ValueError("Loaded simulator benchmark differs from the saved task inventory")
            folder = output / case["case_id"] / "reference"
            folder.mkdir(parents=True, exist_ok=True)
            previous = PREVIOUS / args.benchmark / case["case_id"]
            for name in ("reference.npz", "reference.json"):
                if not (folder / name).exists() and (previous / name).exists():
                    shutil.copyfile(previous / name, folder / name)
            try:
                poses, initial, reference = replay_reference(case, suite, folder)
            except Exception as error:
                traceback.print_exc()
                for mode in MODES:
                    target = output / case["case_id"] / mode
                    target.mkdir(parents=True, exist_ok=True)
                    save_json(target / "result.json", {**case, "mode": mode, "success": False,
                              "status": "reference_error", "error": str(error), "reference_success": False})
                continue
            with h5py.File(case["hdf5"], "r") as data:
                actions = data["data/demo_0/actions"][:]
            for mode in MODES:
                save_json(output / f"active-{args.shard}.json", {"case": case, "mode": mode})
                log({"stage": "start", "case": case["case_id"], "mode": mode, "shard": args.shard})
                try:
                    result = rollout(case, suite.get_task(case["task_index"]), poses, initial,
                                     actions, reference, mode, client, output)
                except Exception as error:
                    traceback.print_exc()
                    result = {**case, "mode": mode, "success": False, "status": "error", "error": str(error),
                              "reference_success": reference["reference_success"]}
                    target = output / case["case_id"] / mode
                    target.mkdir(parents=True, exist_ok=True)
                    save_json(target / "result.json", result)
                    client.close()
                    client = PolicyClient("127.0.0.1", args.port)
                log({"stage": "done", **result})
                if not args.summary_at_end:
                    summarize(args.shard, output)
        summarize(args.shard, output)
    finally:
        client.close()


if __name__ == "__main__":
    main()

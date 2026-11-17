"""Four-suite paired smoke evaluation of frozen pi05 with alpha=0 and alpha=1."""

import argparse
import contextlib
import hashlib
import io
import json
import time
from pathlib import Path

import h5py
import imageio.v2 as imageio
import numpy as np
from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark
from scipy.spatial.transform import Rotation
from starVLA.keypoint_flow.evaluate_gt_libero import make_env, save_json
from work_dirs.keypoint_flow.evaluation.pi05_native_noise.evaluate import (
    BUDGETS,
    KEY_STEPS,
    log,
)

ROOT = Path(__file__).resolve().parent
REFERENCES = ROOT.parent / "pi05_native_noise"


# Nested NumPy diagnostics -> JSON-compatible detached values.
def serializable(value):
    if isinstance(value, dict):
        return {name: serializable(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


# Current observation and cached poses [T+1,1,8] -> external world-frame geometric targets at fixed execution ticks.
def geometric_request(observation, poses, actions, completed, reference, controller):
    targets = poses[np.minimum(completed + KEY_STEPS, len(poses) - 1), 0]
    base = Rotation.from_matrix(reference["base_rotation"])
    target_position = base.apply(targets[:, :3]) + reference["base_position"]
    target_rotation = (base * Rotation.from_quat(targets[:, 3:7])).as_matrix()
    request = {"steps": KEY_STEPS, "current_position": observation["robot0_eef_pos"],
               "current_rotation": Rotation.from_quat(observation["robot0_eef_quat"]).as_matrix(),
               "target_position": target_position, "target_rotation": target_rotation,
               "target_gripper": actions[np.minimum(completed + KEY_STEPS - 1, len(actions) - 1), 6]}
    for name in ("input_min", "input_max", "output_min", "output_max"):
        request[name] = np.asarray(getattr(controller, name)).copy()
    return request


# Initial observation and native OSC -> first-command position/rotation parity without executing robot actions.
def verify_controller(observation, controller):
    saved_state = dict(controller.__dict__)
    controller.update(force=True)
    action = np.array([0.11, -0.17, 0.23, -0.13, 0.19, 0.07])
    controller.set_goal(action)
    scaled = controller.scale_action(action)
    position = observation["robot0_eef_pos"] + scaled[:3]
    body_rotation = Rotation.from_quat(observation["robot0_eef_quat"])
    body_to_site = body_rotation.inv() * Rotation.from_matrix(controller.ee_ori_mat)
    rotation = Rotation.from_rotvec(scaled[3:]) * body_rotation * body_to_site
    errors = {"position_max_error": float(np.max(np.abs(position - controller.goal_pos))),
              "rotation_matrix_max_error": float(np.max(np.abs(rotation.as_matrix() - controller.goal_ori)))}
    controller.__dict__.update(saved_state)
    if max(errors.values()) > 1e-6:
        raise ValueError(f"OSC first-command decoding mismatch: {errors}")
    return errors


# One case and alpha -> completed rollout, video, actual executed state errors and generation diagnostics.
def rollout(case, task, alpha, client, output, references=REFERENCES, mode=None,
            probe=True, compare_baseline=False, baseline_client=None):
    mode = mode or f"alpha_{alpha:g}"
    folder = output / case["case_id"] / mode
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "result.json"
    if marker.exists():
        return json.loads(marker.read_text())
    reference_folder = references / case["case_id"] / "reference"
    reference = json.loads((reference_folder / "reference.json").read_text())
    with np.load(reference_folder / "reference.npz") as data:
        poses, initial = data["poses"], data["initial_state"]
    with h5py.File(case["hdf5"], "r") as data:
        gt_actions = data["data/demo_0/actions"][:]
    env = make_env(task, case["seed"], absolute=False)
    video = imageio.get_writer(str(folder / "policy.mp4"), fps=20)
    before = time.monotonic()
    try:
        observation = env.set_init_state(initial)
        if not np.array_equal(env.env.sim.get_state().flatten(), initial):
            raise ValueError("Initial state differs between paired conditions")
        controller = env.env.robots[0].controller
        if not controller.use_delta or controller.impedance_mode != "fixed":
            raise ValueError("Expected unmodified native delta OSC")
        controller_errors = verify_controller(observation, controller)
        completed, success = 0, False
        generated, noises, requests, measured, execution_errors = [], [], [], [], []
        while completed < BUDGETS[case["suite"]] and not success:
            geometry = geometric_request(observation, poses, gt_actions, completed, reference, controller)
            seed = int.from_bytes(hashlib.sha256(f"{case['case_id']}:{completed}".encode()).digest()[:4], "little")
            payload = {
                "observation/image": np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]),
                "observation/wrist_image": np.ascontiguousarray(observation["robot0_eye_in_hand_image"][::-1, ::-1]),
                "observation/state": np.concatenate((observation["robot0_eef_pos"],
                    Rotation.from_quat(observation["robot0_eef_quat"]).as_rotvec(),
                    observation["robot0_gripper_qpos"])).astype(np.float32),
                "prompt": task.language, "geometry": geometry,
                "experiment": {"alpha": alpha, "seed": seed, "verify_original": alpha == 0 and completed == 0,
                               "probe_gradient": probe and alpha != 0 and completed == 0
                                                 and case["suite"] == "libero_spatial"},
            }
            baseline_error, official_error, live_baseline_error = None, None, None
            if compare_baseline and completed == 0:
                validation = dict(payload)
                validation["experiment"] = {**payload["experiment"], "alpha": 0,
                                            "verify_original": True, "probe_gradient": False}
                baseline = client.infer(validation)
                with np.load(output / case["case_id"] / "original/execution.npz") as saved:
                    if not np.array_equal(saved["noise"][0], baseline["experiment"]["initial_noise"]):
                        raise ValueError("Paired baseline noise differs")
                    baseline_error = float(np.max(np.abs(saved["actions"][0] - baseline["actions"])))
                official_error = baseline["experiment"]["original_parity_max_error"]
                if baseline_client is None:
                    raise ValueError("Baseline comparison requires the original policy client")
                native_payload = {name: value for name, value in payload.items()
                                  if name not in ("geometry", "experiment")}
                native_payload.update(sketch=np.zeros((10, 7), dtype=np.float32),
                                      experiment={"mode": "original", "seed": seed, "sigma": 0.3})
                live_baseline = baseline_client.infer(native_payload)
                live_baseline_error = float(np.max(np.abs(live_baseline["actions"] - baseline["actions"])))
                if live_baseline_error > 1e-5:
                    raise ValueError(f"Same-observation baseline mismatch: {live_baseline_error}")
            result = client.infer(payload)
            chunk = np.asarray(result["actions"])
            if chunk.shape != (10, 7) or not np.isfinite(chunk).all():
                raise ValueError("Invalid action chunk")
            details = result["experiment"]
            noises.append(details["initial_noise"])
            generated.append(chunk)
            requests.append({"step": completed, "geometry": geometry, **details,
                             "paired_baseline_max_error": baseline_error,
                             "paired_official_max_error": official_error,
                             "same_observation_baseline_max_error": live_baseline_error})
            log({"stage": "generation", "case": case["case_id"], "alpha": alpha, "step": completed,
                 "sample_seconds": details["sample_seconds"], "parity": details["original_parity_max_error"]})
            for local_tick, action in enumerate(chunk[:min(10, BUDGETS[case["suite"]] - completed)], start=1):
                observation, _, done, _ = env.step(action.tolist())
                completed += 1
                success = bool(done)
                position = observation["robot0_eef_pos"]
                rotation = Rotation.from_quat(observation["robot0_eef_quat"])
                measured.append(np.concatenate((position, rotation.as_quat(), observation["robot0_gripper_qpos"])))
                if local_tick in KEY_STEPS:
                    index = int(np.flatnonzero(KEY_STEPS == local_tick)[0])
                    execution_errors.append({"step": completed,
                        "position_error_m": float(np.linalg.norm(position - geometry["target_position"][index])),
                        "orientation_error_rad": float((rotation * Rotation.from_matrix(
                            geometry["target_rotation"][index]).inv()).magnitude())})
                video.append_data(np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]))
                if success:
                    break
            log({"stage": "execution", "case": case["case_id"], "alpha": alpha,
                 "step": completed, "success": success})
        save_json(folder / "requests.json", serializable({"requests": requests}))
        np.savez_compressed(folder / "execution.npz", actions=np.asarray(generated), noise=np.asarray(noises),
                            measured=np.asarray(measured))
        save_json(folder / "execution_errors.json", {"errors": execution_errors})
        report = {**case, "alpha": alpha, "mode": mode, "success": success, "steps": completed,
                  "budget": BUDGETS[case["suite"]], "reference_success": reference["reference_success"],
                  "initial_state_hash": reference["initial_state_hash"], "controller_validation": controller_errors,
                  "seconds": time.monotonic() - before, "status": "completed",
                  "video": str((folder / "policy.mp4").resolve())}
        save_json(marker, report)
        return report
    finally:
        video.close()
        env.close()


# Four cached reference tasks -> eight paired rollouts with a single fixed guidance coefficient.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18402)
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inventory = json.loads((REFERENCES.parent / "pi05_50k_gt_full/inventory-libero.json").read_text())["cases"]
    cases = [case for case in inventory if case["task_index"] == 0]
    client = PolicyClient("127.0.0.1", args.port, timeout=1800)
    rows = []
    try:
        save_json(args.output / "manifest.json", {"server": client.metadata, "alphas": [0, 1],
                  "keypoint_steps": KEY_STEPS.tolist(), "control_frequency_hz": 20,
                  "cases": cases, "scope": "four_suite_smoke_one_demo0_seed0_each",
                  "loss_scaling": "raw_meters_squared_rotation_chordal_and_native_gripper_no_tuning"})
        for case in cases:
            with contextlib.redirect_stdout(io.StringIO()):
                suite = benchmark.get_benchmark_dict()[case["suite"]]()
            task = suite.get_task(case["task_index"])
            if task.name != case["task_name"]:
                raise ValueError("Task differs from cached reference")
            for alpha in (0, 1):
                log({"stage": "start", "case": case["case_id"], "alpha": alpha})
                rows.append(rollout(case, task, alpha, client, args.output))
                counts = {f"alpha_{a}": {"completed": sum(row["alpha"] == a for row in rows),
                    "successes": sum(row["alpha"] == a and row["success"] for row in rows)} for a in (0, 1)}
                save_json(args.output / "summary.json", {"counts": counts, "cases": rows})
    finally:
        client.close()


if __name__ == "__main__":
    main()

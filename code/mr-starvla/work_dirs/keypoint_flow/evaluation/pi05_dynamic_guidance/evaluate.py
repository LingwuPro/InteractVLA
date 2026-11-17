import argparse
import contextlib
import hashlib
import io
import itertools
import json
import pickle
import tempfile
import time
from pathlib import Path

import h5py
import imageio.v2 as imageio
import mujoco
import numpy as np
from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark, get_libero_path
from libero.libero.envs.env_wrapper import ControlEnv
from robosuite.utils.errors import RandomizationError
from scipy.spatial.transform import Rotation
from starVLA.keypoint_flow.evaluate_gt_libero import (
    make_env,
    replay_reference,
    save_json,
)
from starVLA.keypoint_flow.libero_control import LiberoPoseFrame, measured_base_pose
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.recorded_reference_audit.recover_commands import (
    reconstruct,
)
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.evaluate import (
    serializable,
    verify_controller,
)
from work_dirs.keypoint_flow.evaluation.pi05_native_noise.evaluate import BUDGETS, log

ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT.parent / "pi05_native_noise_plus"
RECORDED_COMMAND_STRATEGIES = ("recorded_clock_command", "recorded_event_before_command", "recorded_event_command",
                               "recorded_local_command")
RECORDED_COMMAND_PREFIXES = tuple(strategy + "~" for strategy in RECORDED_COMMAND_STRATEGIES)
RECORDED_COMMAND_CACHE = ROOT / "recorded_reference_audit/shared_command_cache"
FIRST_CONTROL_MODE = "first_control_exact_fm_time_gripper_expectation"


class NativeReferenceEnv(ControlEnv):

    def step(self, action):
        return self.env.step(action)



def make_reference_env(task, seed, absolute):
    if absolute:
        raise ValueError("Headless command references require the native delta controller")
    np.random.seed(seed)
    env = NativeReferenceEnv(
        bddl_file_name=str(Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file),
        camera_heights=224, camera_widths=224, control_freq=20,
        use_camera_obs=False, has_renderer=False, has_offscreen_renderer=False,
    )
    env.seed(seed)
    env.reset()
    return env



def select_cases(inventory, limit):
    buckets = {}
    for case in inventory:
        buckets.setdefault((case["category"], case["suite"]), []).append(case)
    for values in buckets.values():
        values.sort(key=lambda case: hashlib.sha256(("dynamic-v1:" + case["case_id"]).encode()).hexdigest())
    selected = []
    for depth in range(max(map(len, buckets.values()))):
        for key in sorted(buckets):
            if depth < len(buckets[key]):
                selected.append(buckets[key][depth])
                if len(selected) == limit:
                    return selected
    return selected



def target_index(actions, completed, strategy, pose_count):
    if strategy == "first_control_command":
        return min(completed + 1, pose_count - 1)
    if strategy in RECORDED_COMMAND_STRATEGIES:
        strategy = strategy.removeprefix("recorded_")
    if strategy == "clock_finite_command":
        return min(completed + 10, pose_count - 1) if completed < len(actions) else None
    if strategy in ("clock", "clock_command", "recorded_clock_command"):
        return min(completed + 10, pose_count - 1)
    switches = np.flatnonzero(np.sign(actions[1:, 6]) != np.sign(actions[:-1, 6])) + 1
    if strategy in ("transition_chunk_command", "transition_approach_command"):
        lookahead = 20 if strategy == "transition_approach_command" else 10
        active = np.any((switches >= completed) & (switches < completed + lookahead))
        return min(completed + 10, pose_count - 1) if active else None
    event_poses = switches + (0 if strategy in ("event_before", "event_before_command") else 1)
    if strategy in ("event_stable", "event_settled"):
        boundaries = np.r_[0, switches, len(actions)]
        events = []
        for segment, (start, end) in enumerate(itertools.pairwise(boundaries)):
            if actions[start, 6] > 0 and end - start >= 20:
                if start > 0:
                    events.append(start + (10 if strategy == "event_settled" else 1))
                if end < len(actions):
                    offset = 10 if strategy == "event_settled" else 1
                    events.append(min(end + offset, boundaries[segment + 2]))
        event_poses = np.asarray(sorted(set(events)), dtype=np.int64)
    future = event_poses[event_poses > completed]
    if strategy in ("event_only", "event_horizon"):
        if not len(future) or (strategy == "event_horizon" and future[0] > completed + 10):
            return None
        return min(int(future[0]), pose_count - 1)
    return min(int(future[0]) if len(future) else pose_count - 1, pose_count - 1)



def gripper_event_progress(actions, executed_gripper):
    directions = np.sign(actions[:, 6])
    switches = np.flatnonzero(directions[1:] != directions[:-1]) + 1
    observed = np.sign(np.r_[actions[0, 6], executed_gripper])
    changed = observed[1:][observed[1:] != observed[:-1]]
    cursor = 0
    for direction in changed:
        if cursor < len(switches) and direction == directions[switches[cursor]]:
            cursor += 1
    index = int(switches[cursor]) + 1 if cursor < len(switches) else len(actions)
    return index, cursor



def reference_progress(current_pose, poses, previous, control_scale):
    remaining = poses[previous:, 0]
    dp2 = np.sum(((remaining[:, :3] - current_pose[:3]) / control_scale[:3]) ** 2, axis=-1)
    rotations = Rotation.from_quat(remaining[:, 3:7]).as_matrix()
    current_rotation = Rotation.from_quat(current_pose[3:7]).as_matrix()
    dr2 = np.sum((rotations - current_rotation) ** 2, axis=(-1, -2)) / np.mean(control_scale[3:]) ** 2
    dw2 = ((remaining[:, 7] - current_pose[7]) / 0.04) ** 2
    return previous + int(np.argmin(0.5 * dp2 + 0.25 * dr2 + 0.5 * dw2))



def local_path_target(current_pose, poses, actions, executed_gripper, previous, control_scale):
    phase_end, cursor = gripper_event_progress(actions, executed_gripper)
    boundaries = np.flatnonzero(np.sign(actions[1:, 6]) != np.sign(actions[:-1, 6])) + 2
    phase_start = int(boundaries[cursor - 1]) if cursor else 0
    progress = reference_progress(current_pose, poses[:phase_end + 1], max(previous, phase_start), control_scale)
    return min(progress + 10, phase_end), progress, cursor



def nearest_command_target(current_pose, poses, control_scale):
    progress = reference_progress(current_pose, poses, 0, control_scale)
    return min(progress + 10, len(poses) - 1), progress



def local_command_target(current_pose, poses, previous, control_scale):
    end = min(previous + 20, len(poses) - 1)
    progress = reference_progress(current_pose, poses[:end + 1], previous, control_scale)
    return min(progress + 10, len(poses) - 1), progress



def first_local_command_target(current_pose, poses, previous, control_scale):
    _, progress = local_command_target(current_pose, poses, previous, control_scale)
    return min(progress + 1, len(poses) - 1), progress



def history_command_target(history, poses, previous, control_scale):
    lower = max(0, previous - 10)
    remaining = poses[lower:, 0]
    dp2 = np.sum(((history[:, None, :3] - remaining[None, :, :3]) / control_scale[:3]) ** 2, axis=-1)
    observed_rotations = Rotation.from_quat(history[:, 3:7]).as_matrix()
    reference_rotations = Rotation.from_quat(remaining[:, 3:7]).as_matrix()
    dr2 = np.sum((observed_rotations[:, None] - reference_rotations[None]) ** 2, axis=(-1, -2))
    dw2 = ((history[:, None, 7] - remaining[None, :, 7]) / 0.04) ** 2
    errors = 0.5 * dp2 + 0.25 * dr2 / np.mean(control_scale[3:]) ** 2 + 0.5 * dw2
    costs = np.zeros(len(remaining) + 1)
    for row in errors:
        current = np.full_like(costs, np.inf)
        for j, error in enumerate(row, start=1):
            current[j] = error + min(costs[j - 1], costs[j], current[j - 1])
        costs = current
    first_endpoint = previous - lower
    progress = previous + int(np.argmin(costs[1:][first_endpoint:]))
    return min(progress + 10, len(poses) - 1), progress



def command_reference_poses(case, task, initial, poses, actions, output, env_factory=make_env):
    cache = output / "commands.npz"
    if cache.exists():
        with np.load(cache) as values:
            return values["commands"]
    output.mkdir(parents=True, exist_ok=True)
    env = env_factory(task, case["seed"], absolute=False)
    try:
        observation = env.set_init_state(initial)
        frame = LiberoPoseFrame(env)
        measured = [frame.measured_pose(observation)]
        commands = [measured[0]]
        success = False
        for action in actions:
            observation, _, done, _ = env.step(action.tolist())
            measured.append(frame.measured_pose(observation))
            commands.append(frame.command_pose())
            success |= bool(done)
        values = np.asarray(commands)
        np.savez_compressed(cache, commands=values, measured=np.asarray(measured))
        save_json(output / "source.json", {
            "source": "native_GT_replay_actual_OSC_goal_and_gripper_actuator_command",
            "reference_success": success, "steps": len(actions), "index_base": 0,
            "index_semantics": "index h is the command submitted during action h-1; index 0 is initial measured pose",
            "cached_measured_pose_max_difference": float(np.max(np.abs(np.asarray(measured) - poses))),
        })
        return values
    finally:
        env.close()



def recorded_command_reference(case, poses, output):
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "recorded_reference_audit/recover_commands.py"
    identity = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "hdf5": case["hdf5"], "hdf5_size": Path(case["hdf5"]).stat().st_size,
                "hdf5_mtime_ns": Path(case["hdf5"]).stat().st_mtime_ns}
    cache = output / "commands.npz"
    metadata = output / "source.json"
    if cache.exists():
        if json.loads(metadata.read_text())["identity"] != identity:
            raise ValueError("Recorded command cache source changed; use a new output directory")
        with np.load(cache) as values:
            return values["commands"]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    shared = RECORDED_COMMAND_CACHE / f"{key}.npz"
    if shared.exists():
        with np.load(shared) as stored:
            commands, ticks = stored["commands"], int(stored["ticks"])
    else:
        with h5py.File(case["hdf5"], "r") as data:
            commands, ticks = reconstruct(data["data/demo_0"], json.loads(data["data"].attrs["env_args"]))
        shared.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=shared.parent, suffix=".partial", delete=False) as stream:
            np.savez_compressed(stream, commands=commands, ticks=ticks)
            temporary = Path(stream.name)
        temporary.replace(shared)
    values = np.concatenate((poses[:1], commands), axis=0)
    assert values.shape == poses.shape and np.isfinite(values).all()
    np.savez_compressed(cache, commands=values)
    save_json(metadata, {"identity": identity, "physics_ticks_per_control": ticks, "shared_cache": str(shared),
              "source": "recorded_pre_action_states_native_controller_with_forced_refresh",
              "index_semantics": "h >= 1 corresponds to action h-1; index 0 is initial replay measurement",
              "limitations": ["Native controller caches unavailable; fresh FK anchors and initial orientation are used.",
                              "Zero rotation retains preceding reconstructed goal; gripper starts at native zero.",
                              "First five dataset actions unavailable; no target-variant geometry adaptation."]})
    return values



def first_control_anchors(env):
    controller = env.env.robots[0].controller
    if (not controller.use_delta or not controller.use_ori or controller.impedance_mode != "fixed"
            or controller.position_limits is not None or controller.orientation_limits is not None
            or controller.interpolator_pos is not None or controller.interpolator_ori is not None):
        raise ValueError("First-control codec requires native unrestricted fixed-impedance delta OSC")
    position, rotation = controller.ee_pos.copy(), controller.ee_ori_mat.copy()
    if controller.new_update:
        model = controller.sim.model._model
        shadow = mujoco.MjData(model)
        shadow.qpos[:] = controller.sim.data.qpos
        shadow.mocap_pos[:] = controller.sim.data.mocap_pos
        shadow.mocap_quat[:] = controller.sim.data.mocap_quat
        mujoco.mj_kinematics(model, shadow)
        site = controller.sim.model.site_name2id(controller.eef_name)
        position, rotation = shadow.site_xpos[site].copy(), shadow.site_xmat[site].reshape(3, 3).copy()
    return {"controller_position": position, "controller_rotation": rotation,
            "controller_goal_rotation": controller.goal_ori.copy(),
            "body_to_site_rotation": LiberoPoseFrame(env).body_to_site_rotation.as_matrix()}



def geometry_request(observation, pose, reference, env, reduction):
    controller = env.env.robots[0].controller
    gripper = env.env.robots[0].gripper
    base = Rotation.from_matrix(reference["base_rotation"])
    request = {"coordinate_frame": "world", "codec": "libero_panda_delta_osc_v1", "reduction": reduction,
               "current_position": observation["robot0_eef_pos"],
               "current_rotation": Rotation.from_quat(observation["robot0_eef_quat"]).as_matrix(),
               "current_gripper_command": float(gripper.current_action[0]),
               "target_position": base.apply(pose[:3]) + reference["base_position"],
               "target_rotation": (base * Rotation.from_quat(pose[3:7])).as_matrix(),
               "target_width": float(pose[7]),
               "gripper_increment": float(gripper.speed * int(env.env.control_timestep / env.env.model_timestep)),
               "gripper_width_scale": 0.04}
    for name in ("input_min", "input_max", "output_min", "output_max"):
        request[name] = np.asarray(getattr(controller, name)).copy()
    if reduction == FIRST_CONTROL_MODE:
        request.update(first_control_anchors(env))
    return request



def verify_gripper_codec(env):
    gripper = env.env.robots[0].gripper
    saved = gripper.current_action.copy()
    ids = [env.env.sim.model.actuator_name2id(name) for name in gripper.actuators]
    ranges = env.env.sim.model.actuator_ctrlrange[ids]
    np.testing.assert_allclose(ranges, [[0, 0.04], [-0.04, 0]], atol=1e-9)
    ticks = int(env.env.control_timestep / env.env.model_timestep)
    previous, maximum = float(saved[0]), 0.0
    try:
        for action in (-0.4, -0.7, 0.2, 0.9, 0.0):
            previous = float(np.clip(previous - ticks * gripper.speed * np.sign(action), -1, 1))
            for _ in range(ticks):
                formatted = gripper.format_action(np.array([action]))
            controls = ranges.mean(axis=1) + (ranges[:, 1] - ranges[:, 0]) * formatted / 2
            width = float(controls[0] - controls[1])
            maximum = max(maximum, abs(width - 0.04 * (previous + 1)))
    finally:
        gripper.current_action = saved
    assert maximum < 1e-9
    return {"gripper_width_command_max_error_m": maximum, "physics_ticks_per_control": ticks}



def make_payload(case, task, observation, geometry, completed, scale):
    seed = int.from_bytes(hashlib.sha256(f"{case['case_id']}:{completed}".encode()).digest()[:4], "little")
    return {"observation/image": np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]),
            "observation/wrist_image": np.ascontiguousarray(observation["robot0_eye_in_hand_image"][::-1, ::-1]),
            "observation/state": np.concatenate((observation["robot0_eef_pos"],
                Rotation.from_quat(observation["robot0_eef_quat"]).as_rotvec(),
                observation["robot0_gripper_qpos"])).astype(np.float32),
            "prompt": task.language, "geometry": geometry, "experiment": {"seed": seed},
            "keypoint_guidance": {"enabled": scale > 0, "guidance_scale": scale}}



def rollout_settings(mode):
    native = mode in ("original", "original_halfchunk")
    strategy, scale = (("clock", 0.0) if native else
                       (mode.rsplit("_s", 1)[0], float(mode.rsplit("_s", 1)[1])))
    strategy, separator, reduction = strategy.partition("~")
    reduction = reduction if separator else "hard"
    execution_steps = 5 if mode == "original_halfchunk" or strategy == "clock_halfchunk_command" else 10
    strategy = "clock_command" if strategy == "clock_halfchunk_command" else strategy
    strategy = "local_command" if strategy == "recorded_local_command" else strategy
    return strategy, scale, reduction, execution_steps



def rollout(
    case, task, poses, initial, reference, actions, mode, client, output, fixture_only=False, command_poses=None
):
    strategy, scale, reduction, execution_steps = rollout_settings(mode)
    if strategy not in (
        "clock", "event_before", "event_at", "event_only", "event_horizon", "event_pose", "event_command",
        "event_stable", "event_settled", "event_pose_command", "event_phase_command", "event_gripper_command",
        "clock_command", "event_before_command", "path_command", "nearest_command",
        "history_command", "first_control_command", "first_local_command",
        "transition_chunk_command", "transition_approach_command", "clock_finite_command", "local_command",
        *RECORDED_COMMAND_STRATEGIES,
    ):
        raise ValueError(f"Unknown target strategy: {strategy}")
    folder = output / case["case_id"] / mode
    marker = folder / "result.json"
    if marker.exists() and not fixture_only:
        return json.loads(marker.read_text())
    folder.mkdir(parents=True, exist_ok=True)
    env = make_env(task, case["seed"], absolute=False)
    video = None
    before = time.monotonic()
    try:
        observation = env.set_init_state(initial)
        assert np.array_equal(env.env.sim.get_state().flatten(), initial)
        controller_errors = verify_controller(observation, env.env.robots[0].controller)
        controller_errors.update(verify_gripper_codec(env))
        base_position = np.asarray(reference["base_position"])
        base_rotation = Rotation.from_matrix(reference["base_rotation"])
        command_based = strategy in ("event_command", "event_before_command", "event_pose_command",
                                     "event_phase_command",
                                     "event_gripper_command", "clock_command", "path_command", "nearest_command",
                                     "history_command", "transition_chunk_command", "transition_approach_command",
                                     "clock_finite_command", "local_command", "first_control_command",
                                     "first_local_command",
                                     *RECORDED_COMMAND_STRATEGIES)
        progress_based = strategy in ("event_pose", "event_pose_command", "event_phase_command", "path_command",
                                      "nearest_command", "history_command", "local_command", "first_local_command")
        target_poses = command_poses if command_based else poses
        assert target_poses is not None
        completed, success = 0, False
        progress = 0
        requests, generated, noises, measured = [], [], [], []
        executed_actions = []
        executed_gripper = []
        pose_history = ([measured_base_pose(observation, base_position, base_rotation)[0]]
                        if strategy == "history_command" else [])
        event_cursor = None
        if not fixture_only:
            video = imageio.get_writer(str(folder / "policy.mp4"), fps=20)
        while completed < BUDGETS[case["suite"]] and not success:
            if strategy == "event_gripper_command":
                index, event_cursor = gripper_event_progress(actions, executed_gripper)
            elif progress_based:
                current = measured_base_pose(observation, base_position, base_rotation)[0]
                controller = env.env.robots[0].controller
                control_scale = (controller.output_max - controller.output_min) / 2
                if strategy in ("local_command", "first_local_command"):
                    first = strategy == "first_local_command"
                    target_fn = first_local_command_target if first else local_command_target
                    index, progress = ((min(1 if first else 10, len(poses) - 1), 0) if completed == 0 else
                                       target_fn(current, poses, progress, control_scale))
                elif strategy == "history_command":
                    index, progress = ((min(10, len(poses) - 1), 0) if completed == 0 else
                                       history_command_target(np.asarray(pose_history), poses, progress, control_scale))
                elif strategy == "nearest_command":
                    index, progress = nearest_command_target(current, poses, control_scale)
                elif strategy == "path_command":
                    index, progress, event_cursor = local_path_target(
                        current, poses, actions, executed_gripper, progress, control_scale)
                else:
                    phase_end = target_index(actions, progress, "event_at", len(poses))
                    progress_poses = poses[:phase_end + 1] if strategy == "event_phase_command" else poses
                    progress = reference_progress(current, progress_poses, progress, control_scale)
                    index = target_index(actions, progress, "event_at", len(poses))
            else:
                index = target_index(actions, completed, strategy, len(poses))
            geometry = None if index is None else geometry_request(
                observation, target_poses[index, 0], reference, env, reduction)
            payload = make_payload(case, task, observation, geometry, completed, scale if index is not None else 0.0)
            if fixture_only:
                with (ROOT / "fixture.pkl").open("wb") as stream:
                    pickle.dump(payload, stream)
                save_json(ROOT / "fixture.json", {"case": case, "target_pose_index": index,
                          "controller_validation": controller_errors, "geometry": serializable(geometry)})
                return {"fixture": str(ROOT / "fixture.pkl")}
            result = client.infer(payload)
            if result.get("status", "accepted") != "accepted":
                requests.append({"step": completed, "status": result["status"],
                                 "reason": result.get("reason"), "proposal": serializable(result)})
                break
            chunk = np.asarray(result["actions"])
            assert chunk.shape == (10, 7) and np.isfinite(chunk).all()
            details = result["experiment"]
            generated.append(chunk)
            noises.append(details["initial_noise"])
            camera_diagnostics = {name: {
                "sha256": hashlib.sha256(payload[name].tobytes()).hexdigest(),
                "mean": float(payload[name].mean()), "std": float(payload[name].std()),
            } for name in ("observation/image", "observation/wrist_image")}
            progress_index = progress if progress_based else None
            requests.append({"step": completed, "target_reference_pose_index": index, "geometry": geometry,
                             "execution_horizon": execution_steps,
                             "reference_progress_index": progress_index,
                             "gripper_event_cursor": event_cursor,
                             "camera_diagnostics": camera_diagnostics, **details})
            if completed % 10 == 0:
                log({"stage": "generation", "case": case["case_id"], "mode": mode, "step": completed,
                     "target_reference_pose_index": index, "sample_seconds": details["sample_seconds"],
                     "reference_progress_index": progress_index,
                     "matching_indices": np.asarray(details["logs"])[..., 0].reshape(-1).tolist()})
            for action in chunk[:min(execution_steps, BUDGETS[case["suite"]] - completed)]:
                observation, _, done, _ = env.step(action.tolist())
                executed_actions.append(action.copy())
                if strategy in ("event_gripper_command", "path_command"):
                    executed_gripper.append(float(action[6]))
                if strategy == "history_command":
                    pose_history.append(measured_base_pose(observation, base_position, base_rotation)[0])
                    pose_history = pose_history[-11:]
                completed += 1
                success = bool(done)
                measured.append(np.concatenate((observation["robot0_eef_pos"], observation["robot0_eef_quat"],
                                                 observation["robot0_gripper_qpos"])))
                video.append_data(np.ascontiguousarray(observation["agentview_image"][::-1, ::-1]))
                if success:
                    break
        save_json(folder / "requests.json", serializable({"requests": requests}))
        np.savez_compressed(folder / "execution.npz", actions=np.asarray(generated), noise=np.asarray(noises),
                            measured=np.asarray(measured), executed_actions=np.asarray(executed_actions))
        report = {**case, "mode": mode, "success": success, "steps": completed, "status": "completed",
                  "prediction_horizon": 10, "execution_horizon": execution_steps,
                  "seconds": time.monotonic() - before, "initial_state_hash": reference["initial_state_hash"],
                  "reference_success": reference["reference_success"], "controller_validation": controller_errors,
                  "render_validation": "both_RGB_cameras_every_observation_v1",
                  "initial_camera_diagnostics": requests[0].get("camera_diagnostics") if requests else None,
                  "termination_reason": requests[-1].get("reason") if requests else None,
                  "video": str((folder / "policy.mp4").resolve())}
        save_json(marker, report)
        return report
    finally:
        if video is not None:
            video.close()
        env.close()



def summarize(rows, modes):
    by_case = {}
    for row in rows:
        if row["status"] == "completed":
            by_case.setdefault(row["case_id"], {})[row["mode"]] = row
    report = {}
    for mode in modes:
        pairs = [values for values in by_case.values() if "original" in values and mode in values]
        report[mode] = {"pairs": len(pairs), "baseline_successes": sum(value["original"]["success"] for value in pairs),
                        "successes": sum(value[mode]["success"] for value in pairs),
                        "rescued": sum(value[mode]["success"] and not value["original"]["success"] for value in pairs),
                        "harmed": sum(value["original"]["success"] and not value[mode]["success"] for value in pairs)}
    return report



def record_placement_error(case, mode, output, error, phase):
    marker = output / case["case_id"] / mode / "result.json"
    if marker.exists():
        return json.loads(marker.read_text())
    marker.parent.mkdir(parents=True, exist_ok=True)
    result = {**case, "mode": mode, "status": "environment_error", "success": None,
              "error_type": "RandomizationError", "error": str(error), "phase": phase,
              "reference_success": None}
    save_json(marker, result)
    log({"stage": "environment_error", **result})
    return result



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18403)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--modes", nargs="+", default=["original", "clock_s1", "event_before_s1", "event_at_s1"])
    parser.add_argument("--output", type=Path, default=ROOT / "pilot")
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--headless-reference-commands", action="store_true")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--cases-file", type=Path)
    args = parser.parse_args()
    cases = select_cases(json.loads((PREVIOUS / "inventory.json").read_text())["cases"], args.limit)
    selection = "sha256_dynamic-v1_stratified_no_outcome_filter"
    if args.cases_file is not None:
        selected = json.loads(args.cases_file.read_text())
        cases, selection = selected["cases"], selected["selection"]
        if len({case["case_id"] for case in cases}) != len(cases):
            raise ValueError("Duplicate selected cases")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"cases": cases, "modes": args.modes, "selection": selection,
                "index_base": 0, "event_definition": "GT native gripper command sign change",
                "event_before": "pose immediately before switch action", "event_at": "pose after switch action",
                "event_selection": "next event after executed step; single fixed goal within each chunk",
                "matching": "full_pose_argmin_recomputed_each_generation_step", "horizon": 10,
                "scope": "exploratory_subset_not_full_benchmark_or_held_out_confirmation"}
    if args.headless_reference_commands:
        manifest["new_command_reference_replays"] = {
            "renderer": "disabled", "step": "native physics; skip image-only corruptions",
            "cache": "existing rendered command caches remain valid; policy rollouts retain original RGB rendering",
        }
    if "original_halfchunk" in args.modes or any(mode.startswith("clock_halfchunk_command~") for mode in args.modes):
        manifest["half_chunk_execution"] = {
            "prediction_horizon": 10, "execution_horizon": 5, "control_frequency_hz": 20,
            "target": "same clock command at executed + 10, with original final-command persistence",
            "context": "new observation and prefix cache after every five executed controls",
            "candidate_diagnostics": "full ten-action candidate; only first five are executed before replanning",
            "comparison": "pair guided half-chunk with original_halfchunk to separate feedback frequency effects",
            "cost": "up to twice as many complete sampling calls per physical control sequence",
        }
    if any(mode.startswith(("event_only~", "event_horizon~")) for mode in args.modes):
        manifest["strict_events"] = {
            "event_only": "next real GT switch; no future switch means native sampling with no geometry",
            "event_horizon": "next real GT switch within 10 control cycles; otherwise native sampling with no geometry",
        }
    if any(mode.startswith("event_pose~") for mode in args.modes):
        manifest["event_pose"] = (
            "nearest full measured GT pose in controller units, monotonic reference progress; "
            "next gripper event after that progress, final pose if none; one fixed goal per chunk"
        )
    if any(mode.startswith("event_command~") for mode in args.modes):
        manifest["event_command"] = (
            "actual OSC goal and gripper actuator command recorded during native GT replay; "
            "same base/body pose representation as measured references; cached once per case; "
            "next event selected by executed count, final command if none"
        )
    if any(mode.startswith("event_before_command~") for mode in args.modes):
        manifest["event_before_command"] = (
            "actual OSC goal and gripper actuator command one control before each GT action sign change; "
            "a switch at action index s selects command pose index s, from action s-1; "
            "next event selected by executed count, final command fallback; same codec as event_command"
        )
    if any(mode.startswith("event_pose_command~") for mode in args.modes):
        manifest["event_pose_command"] = (
            "monotonic nearest measured GT full pose selects reference progress; "
            "next gripper event after that progress supplies its actual recorded command pose; "
            "final command fallback; one fixed goal per chunk, no controller reset changes"
        )
    if any(mode.startswith("first_control_command~") for mode in args.modes):
        manifest["first_control_command"] = {
            "target": "actual GT command at executed+1, clamped to final; one fixed goal per H10 chunk",
            "codec": "actual next controller cache or isolated fresh FK; zero rotation retains prior goal",
            "loss": "first action only; input gradient still passes through full action expert",
            "execution": "unchanged synchronous H10; no claims about later command poses or contact",
        }
    if any(mode.startswith("first_local_command~") for mode in args.modes):
        manifest["first_local_command"] = {
            "alignment": "same full-pose local progress in [previous, previous+20]; no backward movement",
            "target": "actual GT command at progress+1, clamped to final; initial command index1/progress0",
            "changed_factor": "GT target selection only; exact first-command codec retained",
            "limits": "nearest measured GT pose can alias or stall; target need not lie in the one-command range",
        }
    if any(mode.startswith("event_phase_command~") for mode in args.modes):
        manifest["event_phase_command"] = (
            "monotonic nearest measured full-pose progress, search capped at next gripper event; "
            "at most one event boundary crossed per chunk; next event supplies its actual command pose; "
            "final command fallback; no new distance threshold or controller reset change"
        )
    if any(mode.startswith("event_gripper_command~") for mode in args.modes):
        manifest["event_gripper_command"] = (
            "ordered GT sign-change events advanced only by matching sign changes in actually executed actions; "
            "initial direction from the first GT action; next event's actual command pose, final command fallback; "
            "no wall-clock advancement, pose threshold, or model input change; retries can consume later events"
        )
    if any(mode.startswith(("clock_command~", "path_command~", "nearest_command~")) for mode in args.modes):
        manifest["local_command_targets"] = {
            "clock_command": "actual GT command at executed count + native horizon 10, clamped to final command",
            "path_command": (
                "monotonic full-pose nearest GT state within current executed-gripper event phase; "
                "actual command 10 GT controls ahead, clamped to phase end; one fixed target per chunk"
            ),
            "prior_information": "dense same-episode GT pose path, not only gripper event poses",
            "limitations": "repeated poses may alias within phase; gripper retries may advance incorrect phase",
        }
    if any(mode.startswith("nearest_command~") for mode in args.modes):
        manifest["local_command_targets"]["nearest_command"] = (
            "fresh full-pose nearest GT state without monotonic or gripper-phase bounds; "
            "actual command 10 GT controls ahead, clamped to final command; fixed goal within each chunk; "
            "repeated GT states can alias and progress may move backward"
        )
    if any(mode.startswith("clock_finite_command~") for mode in args.modes):
        manifest["clock_finite_command"] = {
            "target": "same command index min(executed + 10, GT length) as clock_command while executed < GT length",
            "inactive": "executed >= GT length: no geometry, zero scale, direct original sampler",
            "boundary": "last partial GT interval may still provide a target; no final-pose persistence afterwards",
            "limitation": "GT and policy clocks may diverge; recovery before GT end still receives guidance",
        }
    if any(mode.startswith("recorded_clock_command~") for mode in args.modes):
        source = ROOT / "recorded_reference_audit/recover_commands.py"
        manifest["recorded_clock_command"] = {
            "target": "command reconstructed from recorded state/action at executed + 10, clamped to final command",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "cache_metadata": "per-case recorded_reference_commands/source.json records reconstruction assumptions",
            "limitations": "controller-cache and initial-gripper approximations; original scene, no variant adaptation",
        }
    if any(mode.startswith(("recorded_event_before_command~", "recorded_event_command~")) for mode in args.modes):
        source = ROOT / "recorded_reference_audit/recover_commands.py"
        manifest["recorded_event_command_targets"] = {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "before": "sign switch at native action s selects reconstructed command h=s, from action s-1",
            "at": "sign switch at native action s selects reconstructed command h=s+1, from action s",
            "selection": "next event command index strictly greater than executed controls; final command fallback",
            "limits": "GT-clock selection, not observed contact; may target beyond native prediction horizon",
            "cache_metadata": "recorded_reference_commands/source.json records source and reconstruction assumptions",
        }
    if any(mode.startswith("history_command~") for mode in args.modes):
        manifest["history_command"] = {
            "history_control_states": 11,
            "alignment": "subsequence DTW of full measured poses in controller units; sum cost; earliest endpoint tie",
            "progress": "nondecreasing endpoint; reference search starts at max(0, previous endpoint - 10)",
            "target": "actual command 10 GT controls after matched endpoint, clamped to final command",
            "initial_chunk": "GT command index 10, clamped to final command",
            "prior_information": "dense same-episode measured and command pose paths",
            "limitations": "repeated motion segments can still alias; no guarantee of grasp or contact phase alignment",
        }
    if any(mode.startswith("local_command~") for mode in args.modes):
        manifest["local_command"] = {
            "alignment": "nearest full measured GT pose in [previous_progress, previous_progress + 20], inclusive",
            "target": "actual GT command at matched progress + 10, clamped to the final command",
            "initial_target": "GT command 10 and progress 0",
            "progress": "nondecreasing; at most 20 GT controls per ten-control replanning call",
            "search_span": "two native prediction horizons; explicit exploratory setting, not validated default",
            "matching_metric": "same position, rotation and measured jaw-width controller units as nearest_command",
            "limits": "may stall or lag fast execution; bounds do not certify grasp/contact phase",
        }
    if any(mode.startswith("recorded_local_command~") for mode in args.modes):
        source = ROOT / "recorded_reference_audit/recover_commands.py"
        manifest["recorded_local_command"] = {
            "alignment": "unchanged local_command alignment using target-variant replay measured full poses",
            "progress": "nearest in [previous_progress, previous_progress + 20], nondecreasing; starts at 0",
            "target": "original recorded-state command at matched progress + 10, clamped to final command",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "changed_factor": "command target source only; replay measured poses still supply progress alignment",
            "limitations": "same original-scene reconstruction assumptions; no contact guarantee or scene adaptation",
        }
    if any(mode.startswith("transition_chunk_command~") for mode in args.modes):
        manifest["transition_chunk_command"] = {
            "trigger": "GT action sign switch index k lies in [executed, executed + 10), 0-based action indices",
            "target": "same actual GT command at executed + 10 as clock_command, clamped to final command",
            "inactive": "no geometry, zero guidance scale, direct original sampler; includes time after final GT switch",
            "limitation": "GT command transitions are not verified contact events; GT and policy clocks may diverge",
        }
    if any(mode.startswith("transition_approach_command~") for mode in args.modes):
        manifest["transition_approach_command"] = {
            "trigger": "GT sign switch k in [executed, executed + 20): transition chunk and one preceding chunk",
            "target": "same actual GT command at executed + 10; predicted/action horizon remains 10 controls",
            "inactive": "no geometry, zero guidance scale, direct original sampler",
            "limitation": "GT transitions are not verified contact events; GT and policy clocks may diverge",
        }
    if any(mode.startswith(("event_stable~", "event_settled~")) for mode in args.modes):
        manifest["stable_events"] = {
            "minimum_close_duration_cycles": 20,
            "selection": "retain close/open boundaries of positive gripper runs lasting at least two native chunks",
            "event_stable_pose_offset": 1,
            "event_settled_pose_offset": 10,
            "release_offset_limit": "clamped to next sign boundary so target remains in the opening segment",
            "time_semantics": "offset is measured from action switch index; next selected pose after executed count",
            "fallback": "final measured GT pose",
        }
    if not 0 <= args.shard < args.shards:
        parser.error("Invalid shard")
    marker = args.output / f"manifest-{args.shard}.json"
    if marker.exists() and json.loads(marker.read_text()) != manifest:
        raise ValueError("Use a different output directory for a different manifest")
    save_json(marker, manifest)
    client = None if args.fixture_only else PolicyClient("127.0.0.1", args.port, timeout=1800)
    rows = []
    try:
        for case in cases[args.shard::args.shards]:
            markers = [args.output / case["case_id"] / mode / "result.json" for mode in args.modes]
            if not args.fixture_only and all(marker.exists() for marker in markers):
                rows.extend(json.loads(marker.read_text()) for marker in markers)
                continue
            with contextlib.redirect_stdout(io.StringIO()):
                suite = benchmark.get_benchmark_dict()[case["suite"]]()
            task = suite.get_task(case["task_index"])
            assert task.name == case["task_name"]
            folder = PREVIOUS / case["case_id"] / "reference"
            if not (folder / "reference.json").exists():
                folder = args.output / case["case_id"] / "reference"
                folder.mkdir(parents=True, exist_ok=True)
            try:
                poses, initial, reference = replay_reference(case, suite, folder)
                with h5py.File(case["hdf5"], "r") as data:
                    actions = data["data/demo_0/actions"][:]
                command_poses = None
                recorded_poses = None
                if any(mode.startswith(RECORDED_COMMAND_PREFIXES) for mode in args.modes):
                    recorded_poses = recorded_command_reference(
                        case, poses, args.output / case["case_id"] / "recorded_reference_commands")
                if any(mode.startswith(("event_command~", "event_before_command~", "event_pose_command~",
                                        "event_phase_command~",
                                        "event_gripper_command~", "clock_command~", "path_command~", "nearest_command~",
                                        "history_command~", "transition_chunk_command~", "transition_approach_command~",
                                        "clock_finite_command~", "clock_halfchunk_command~", "local_command~",
                                        "first_control_command~", "first_local_command~"))
                       for mode in args.modes):
                    command_poses = command_reference_poses(
                        case, task, initial, poses, actions, args.output / case["case_id"] / "reference_commands",
                        env_factory=make_reference_env if args.headless_reference_commands else make_env,
                    )
            except RandomizationError as error:
                if args.fixture_only:
                    raise
                rows.extend(record_placement_error(case, mode, args.output, error, "reference")
                            for mode in args.modes)
                save_json(args.output / f"summary-{args.shard}.json", {"completed_rollouts": len(rows),
                          "expected_rollouts": len(cases) * len(args.modes), "paired": summarize(rows, args.modes)})
                continue
            for mode in args.modes:
                log({"stage": "start", "case": case["case_id"], "mode": mode})
                try:
                    selected_commands = recorded_poses if mode.startswith(RECORDED_COMMAND_PREFIXES) else command_poses
                    result = rollout(case, task, poses, initial, reference, actions, mode, client,
                                     args.output, args.fixture_only, selected_commands)
                except RandomizationError as error:
                    if args.fixture_only:
                        raise
                    result = record_placement_error(case, mode, args.output, error, "rollout")
                if args.fixture_only:
                    log(result)
                    return
                rows.append(result)
                save_json(args.output / f"summary-{args.shard}.json", {"completed_rollouts": len(rows),
                          "expected_rollouts": len(cases) * len(args.modes), "paired": summarize(rows, args.modes)})
                log({"stage": "done", **result})
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()

import argparse
import contextlib
import hashlib
import io
import json
import time
from pathlib import Path

import cv2
import imageio.v2 as imageio
import numpy as np
from deployment.astra.policy_client import PolicyClient
from libero.libero import benchmark, get_libero_path
from robosuite import macros
from robosuite.utils.camera_utils import (
    get_camera_extrinsic_matrix,
    get_camera_intrinsic_matrix,
    get_real_depth_map,
)
from scipy.spatial.transform import Rotation
from starVLA.keypoint_flow.evaluate_gt_libero import (
    log,
    save_json,
    validate_rendered_observation,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.evaluate import (
    first_control_anchors,
    geometry_request,
    make_payload,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.native_finish_caps.render_probe.fast_glass import (
    FastGlassEnv,
)
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.evaluate import serializable
from work_dirs.keypoint_flow.evaluation.pi05_native_noise.evaluate import BUDGETS

BEST = "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03"
CAMERAS = ("agentview", "robot0_eye_in_hand")



def reproject(rgb, depth, source, target):
    height, width = depth.shape
    rows, columns = np.indices((height, width))
    pixels = np.stack((columns.ravel(), rows.ravel(), np.ones(height * width)))
    points = np.linalg.solve(np.asarray(source["K"]), pixels) * depth.ravel()
    transform = np.linalg.inv(np.asarray(target["world_from_camera"])) @ np.asarray(source["world_from_camera"])
    points = transform[:3, :3] @ points + transform[:3, 3:4]
    projected = np.asarray(target["K"]) @ points
    valid = np.isfinite(points).all(axis=0) & (points[2] > 0)
    denominator = np.where(valid, projected[2], 1)
    u, v = projected[:2] / denominator
    output = np.zeros_like(rgb)
    distances = np.full(height * width, np.inf)
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        x, y = np.floor(u + 1e-7).astype(int) + dx, np.floor(v + 1e-7).astype(int) + dy
        weight = np.maximum(0, 1 - np.abs(u - x)) * np.maximum(0, 1 - np.abs(v - y))
        keep = valid & (weight > 1e-5) & (x >= 0) & (x < width) & (y >= 0) & (y < height)
        indices = np.flatnonzero(keep)
        if not len(indices):
            continue
        destination = y[keep] * width + x[keep]
        order = np.lexsort((points[2, keep], destination))
        unique = np.r_[True, np.diff(destination[order]) != 0]
        picked = order[unique]
        dest, origin = destination[picked], indices[picked]
        nearer = points[2, origin] < distances[dest]
        dest, origin = dest[nearer], origin[nearer]
        output.reshape(-1, 3)[dest] = rgb.reshape(-1, 3)[origin]
        distances[dest] = points[2, origin]
    mask = (~np.isfinite(distances)).reshape(height, width).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    small = np.zeros_like(mask)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] <= 64:
            small[labels == label] = 1
    output = cv2.inpaint(output, small, 2, cv2.INPAINT_TELEA)
    return output, {"observed_coverage": float(1 - mask.mean()), "inpainted_fraction": float(small.mean()),
                    "large_holes": "black; no unobserved scene geometry accessed"}



def unproject(folder, camera, row, column):
    calibration = json.loads((folder / "observation.json").read_text())["cameras"][camera]
    depth = np.load(folder / f"{camera}-depth.npy")
    height, width = depth.shape
    if not (0 <= row < height and 0 <= column < width):
        raise ValueError("Pixel outside image")
    z = float(depth[row, column])
    if not np.isfinite(z) or z <= 0:
        raise ValueError("Invalid measured depth")
    pixel = np.array([column, row, 1.0])
    camera_point = np.linalg.solve(np.asarray(calibration["K"]), pixel) * z
    transform = np.asarray(calibration["world_from_camera"])
    return transform[:3, :3] @ camera_point + transform[:3, 3]


class Session:

    def __init__(self, args):
        self.args = args
        self.folder = args.output.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.suite = benchmark.get_benchmark_dict()[args.suite]()
        self.task = self.suite.get_task(args.task_index)
        self.case = {"case_id": getattr(args, "case_id", None) or f"{args.suite}-{args.task_index:04d}-reset{args.seed}",
                     "suite": args.suite, "seed": args.seed}
        self.client = PolicyClient("127.0.0.1", args.port, timeout=1800)
        self.env = None
        self.video = None
        self.attempt = -1
        self.reset_count = 0
        self.steps = 0
        self.success = False
        self.obs = {}
        self.tool_calls = []
        self.reset(getattr(args, "attempt", 0))


    def reset(self, attempt):
        log({"event": "reset_started", "case": self.case, "attempt": attempt})
        if self.video is not None:
            self.video.close()
        if self.env is not None:
            self.env.close()
        np.random.seed(self.args.seed)
        self.env = FastGlassEnv(
            bddl_file_name=str(Path(get_libero_path("bddl_files")) / self.task.problem_folder / self.task.bddl_file),
            camera_heights=224, camera_widths=224, camera_depths=True, control_freq=20,
        )
        self.env.seed(self.args.seed)
        self.env.reset()
        initial_file = getattr(self.args, "initial_state_file", None)
        initial = (np.load(initial_file, allow_pickle=False) if initial_file else
                   np.asarray(self.suite.get_task_init_states(self.args.task_index)[self.args.initial_index]))
        self.obs = validate_rendered_observation(self.env.set_init_state(initial))
        self.attempt, self.steps, self.success = attempt, 0, False
        self.tool_calls = []
        self.attempt_started = time.monotonic()
        self.reset_count += 1
        self.video_path = self.folder / f"{self.case['case_id']}-attempt-{attempt}-reset-{self.reset_count}.mp4"
        self.video = imageio.get_writer(str(self.video_path), fps=20)
        self.initial_hash = hashlib.sha256(initial.tobytes()).hexdigest()
        self.snapshot()


    def snapshot(self):
        assert self.env is not None
        record = {"task": self.task.language, "task_name": self.task.name,
                  "case": self.case, "attempt": self.attempt, "steps": self.steps,
                  "success": self.success, "budget": BUDGETS[self.args.suite],
                  "video": str(self.video_path),
                  "initial_state_sha256": self.initial_hash,
                  "policy_checkpoint": self.client.metadata["checkpoint"],
                  "policy_config": self.client.metadata["config"],
                  "camera_mount_family": type(self.env.env).__name__,
                  "reset_source": ("isolated opaque historical evaluation reset; no trajectory access" if
                                   getattr(self.args, "initial_state_file", None) else
                                   "official benchmark initial state; no demonstration trajectory"),
                  "position": self.obs["robot0_eef_pos"], "quaternion_xyzw": self.obs["robot0_eef_quat"],
                  "gripper_qpos": self.obs["robot0_gripper_qpos"],
                  "cameras": {}, "planner_inputs": "RGB-D, calibration, robot proprioception, language only"}
        for camera in CAMERAS:
            rgb = self.obs[f"{camera}_image"]
            depth = get_real_depth_map(self.env.env.sim, self.obs[f"{camera}_depth"])[..., 0]
            if macros.IMAGE_CONVENTION == "opengl":
                rgb, depth = rgb[::-1], depth[::-1]
            imageio.imwrite(self.folder / f"{camera}.png", np.ascontiguousarray(rgb))
            np.save(self.folder / f"{camera}-depth.npy", depth)
            record["cameras"][camera] = {
                "K": get_camera_intrinsic_matrix(self.env.env.sim, camera, 224, 224),
                "world_from_camera": get_camera_extrinsic_matrix(self.env.env.sim, camera),
                "pixel_convention": "top-left origin; row,column; OpenCV optical axes; depth is optical z meters",
            }
        save_json(self.folder / "observation.json", serializable(record))
        return record


    def task_report(self):
        report = {"task": self.task.language, "case": self.case, "attempt": self.attempt,
                  "status": "success" if self.success else "budget_exhausted" if self.steps >= BUDGETS[self.args.suite]
                  else "partial", "success": self.success, "steps": self.steps,
                  "control_budget": BUDGETS[self.args.suite], "video": str(self.video_path),
                  "elapsed_seconds": time.monotonic() - self.attempt_started,
                  "keypoint_reasoning": [call for call in self.tool_calls if "goal" in call],
                  "planning_summary": "VLA first; external goals only from current sensor observations. "
                  "No keypoint was supplied when keypoint_reasoning is empty.",
                  "tool_calls": self.tool_calls,
                  "information_sources": ["RGB", "optical-depth-meters", "camera-calibration",
                                          "robot-proprioception", "language-instruction"],
                  "excluded_sources": ["demonstration-points", "object-ground-truth", "hidden-goal-coordinates"],
                  "token_usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None,
                                  "status": "unavailable", "scope": "planning assistant for this task",
                                  "reason": "Current session does not expose exact per-task token counters; "
                                  "no separate planner API was called. VLA inference is counted as tool calls."}}
        save_json(self.video_path.with_suffix(".task.json"), serializable(report))
        return report


    def step(self, action):
        assert self.env is not None and self.video is not None
        self.obs, _, done, _ = self.env.step(np.asarray(action).tolist())
        validate_rendered_observation(self.obs)
        self.steps += 1
        self.success = bool(done)
        self.video.append_data(np.ascontiguousarray(self.obs["agentview_image"][::-1, ::-1]))


    def goal_request(self, goal):
        position = np.asarray(goal["position"], dtype=float)
        quat = np.asarray(goal["quaternion_xyzw"], dtype=float)
        width = float(goal["width_m"])
        if (position.shape != (3,) or quat.shape != (4,) or not np.isfinite(np.r_[position, quat, width]).all()
                or np.linalg.norm(quat) <= 1e-12 or not 0 <= width <= 0.08):
            return None, {"status": "invalid_request", "reason": "invalid complete pose"}
        quat = quat / np.linalg.norm(quat)
        pose = np.r_[position, quat, width]
        geometry = geometry_request(self.obs, pose, {"base_rotation": np.eye(3), "base_position": np.zeros(3)},
                                    self.env, BEST)
        return geometry, None


    def vla(self, command):
        records = []
        start_steps = self.steps
        for _ in range(int(command.get("chunks", 1))):
            if self.success or self.steps >= BUDGETS[self.args.suite]:
                break
            geometry = None
            if "goal" in command:
                geometry, rejection = self.goal_request(command["goal"])
                if rejection is not None:
                    self.tool_calls.append({"tool": "validate_keypoint", "step": self.steps,
                                            "goal": command["goal"], "response": rejection.copy(),
                                            "reasoning_summary": command.get("reasoning_summary", "Validate pose.")})
                    rejection.update(executed_controls=self.steps - start_steps, chunks=records)
                    return rejection
            case = {**self.case, "case_id": f"{self.case['case_id']}:attempt{self.attempt}"}
            if self.attempt == 0 and getattr(self.args, "historical_noise_seed", False):
                case = self.case
            scale = float(command.get("scale", 30.0)) if geometry is not None else 0.0
            payload = make_payload(case, self.task, self.obs, geometry, self.steps, scale)
            if "subgoal" in command:
                payload["prompt"] = self.task.language + "\nSubgoal: " + command["subgoal"]
            projection = None
            if "canonical_calibration" in command or "camera_calibration_catalog" in command:
                assert self.env is not None
                if "camera_calibration_catalog" in command:
                    catalog = json.loads(Path(command["camera_calibration_catalog"]).read_text())
                    families = [cls.__name__ for cls in type(self.env.env).__mro__ if cls.__name__ in catalog]
                    if not families:
                        raise ValueError("No calibrated nominal camera for this scene family")
                    calibration_path = catalog[families[0]]
                else:
                    calibration_path = command["canonical_calibration"]
                target = json.loads(Path(calibration_path).read_text())["cameras"]["agentview"]
                source = self.snapshot()["cameras"]["agentview"]
                rgb = imageio.imread(self.folder / "agentview.png")
                depth = np.load(self.folder / "agentview-depth.npy")
                rgb, projection = reproject(rgb, depth, source, target)
                projection["calibration_file"] = calibration_path
                payload["observation/image"] = np.ascontiguousarray(rgb[:, ::-1])
                imageio.imwrite(self.folder / "canonical-input.png", rgb)
            before = time.monotonic()
            result = self.client.infer(payload)
            if result.get("status", "accepted") != "accepted":
                self.tool_calls.append({"tool": "pi05_guided", "status": result["status"],
                                        "reason": result.get("reason"), "step": self.steps})
                return {"status": result["status"], "reason": result.get("reason"),
                        "executed_controls": self.steps-start_steps, "proposal": serializable(result),
                        "requested_action": "inspect_observation_and_revise_goal"}
            details = result["experiment"]
            record = {"step": self.steps, "attempt": self.attempt, "guided": geometry is not None,
                      "latency_seconds": time.monotonic() - before, "details": details,
                      "rgbd_reprojection": projection, "subgoal": command.get("subgoal")}
            call = {"tool": "pi05_guided" if geometry is not None else "pi05_native", "step": self.steps,
                    "guidance_scale": scale, "rgbd_reprojection": projection,
                    "reasoning_summary": command.get("reasoning_summary", "Use frozen VLA with the task instruction."),
                    "subgoal": command.get("subgoal"), "inference_seconds": record["latency_seconds"]}
            self.tool_calls.append(call)
            if geometry is not None:
                error = details["final_command_errors"]
                record["goal"] = command["goal"]
                call["goal"] = command["goal"]
                record["final_errors"] = error
            actions = np.asarray(result["actions"])
            if actions.shape != (10, 7) or not np.isfinite(actions).all():
                raise ValueError("Invalid policy action chunk")
            record["actions"] = actions
            records.append(record)
            for action in actions[:min(10, BUDGETS[self.args.suite] - self.steps)]:
                self.step(action)
                if self.success:
                    break
            self.snapshot()
            log({"event": "vla", "attempt": self.attempt, "step": self.steps, "success": self.success,
                 "guided": geometry is not None, "latency_seconds": record["latency_seconds"]})
        if self.success or self.steps >= BUDGETS[self.args.suite]:
            self.task_report()
        return {"status": "executed", "success": self.success, "steps": self.steps, "chunks": records}


    def evaluate(self, command):
        cases = json.loads(Path(command["cases_file"]).read_text())["cases"]
        rows = []
        for case in cases:
            self.command({"type": "task", "suite": case["suite"], "task_index": case["task_index"]})
            folder = self.folder / self.case["case_id"]
            folder.mkdir(exist_ok=True)
            trials = {}
            for attempt in (0, 1):
                if attempt:
                    self.reset(attempt)
                result = self.vla({"chunks": 60})
                result["observation"] = self.snapshot()
                save_json(folder / f"native-{attempt}.json", serializable(result))
                trials[f"native_{attempt}"] = self.success
                if self.success:
                    break
            solved = any(trials.values())
            baseline, recovery = solved, solved
            if not solved:
                for strategy in ("native", "rgbd"):
                    self.reset(2)
                    request = {"chunks": 60}
                    if strategy == "rgbd":
                        key = ("camera_calibration_catalog" if "camera_calibration_catalog" in command
                               else "canonical_calibration")
                        request[key] = command[key]
                    result = self.vla(request)
                    result["observation"] = self.snapshot()
                    save_json(folder / f"{strategy}-2.json", serializable(result))
                    trials[f"{strategy}_2"] = self.success
                    if strategy == "native":
                        baseline = self.success
                    else:
                        recovery = self.success
            row = {"case": case, "trials": trials, "baseline_at_most_3": baseline,
                   "vla2_then_rgbd": recovery, "baseline_at_most_2": solved,
                   "initial_state_sha256": self.initial_hash}
            rows.append(row)
            save_json(folder / "result.json", row)
            report = {"cases": len(rows), "rows": rows,
                      "baseline_at_most_2": sum(r["baseline_at_most_2"] for r in rows),
                      "baseline_at_most_3": sum(r["baseline_at_most_3"] for r in rows),
                      "vla2_then_rgbd": sum(r["vla2_then_rgbd"] for r in rows),
                      "rescued": sum(r["vla2_then_rgbd"] and not r["baseline_at_most_3"] for r in rows),
                      "harmed": sum(r["baseline_at_most_3"] and not r["vla2_then_rgbd"] for r in rows),
                      "budget": "same at most three attempts; same per-suite control budget and attempt-2 noise"}
            save_json(self.folder / "paired-summary.json", report)
            log({"event": "paired_case", **{key: value for key, value in report.items() if key != "rows"}})
        return {"status": "evaluated", **report}


    def servo(self, command):
        assert self.env is not None
        target = np.asarray(command["position"], dtype=float)
        if target.shape != (3,) or not np.isfinite(target).all():
            raise ValueError("Expected finite position [3]")
        controller = self.env.env.robots[0].controller
        scale = (controller.output_max - controller.output_min) / 2
        records = []
        self.tool_calls.append({"tool": "rgbd_servo", "step": self.steps, "goal": command,
                                "reasoning_summary": command.get("reasoning_summary", "Follow an RGB-D point.")})
        for _ in range(min(int(command.get("steps", 10)), BUDGETS[self.args.suite] - self.steps)):
            if self.success:
                break
            anchors = first_control_anchors(self.env)
            delta = np.zeros(6)
            delta[:3] = np.clip(target - anchors["controller_position"], -0.02, 0.02)
            if "quaternion_xyzw" in command:
                wanted = Rotation.from_quat(command["quaternion_xyzw"]).as_matrix()
                body = wanted @ anchors["body_to_site_rotation"]
                delta[3:] = Rotation.from_matrix(body @ anchors["controller_rotation"].T).as_rotvec()
            action = np.r_[np.clip(delta / scale, -1, 1), float(command["gripper_action"])]
            self.step(action)
            records.append(action)
        return {"status": "executed", "success": self.success, "steps": self.steps,
                "source": "observed_rgbd_goal_plus_robot_proprioception", "actions": records}


    def controls(self, command):
        try:
            actions = np.asarray(command["actions"], dtype=float)
        except (TypeError, ValueError):
            return {"status": "invalid_request", "reason": "Expected finite native controls [T,7]",
                    "executed_controls": 0, "steps": self.steps}
        if actions.ndim != 2 or actions.shape[1] != 7 or not np.isfinite(actions).all():
            return {"status": "invalid_request", "reason": "Expected finite native controls [T,7]",
                    "executed_controls": 0, "steps": self.steps}
        self.tool_calls.append({"tool": "native_controls", "step": self.steps, "count": len(actions),
                                "reasoning_summary": command.get("reasoning_summary", "Explicit supplied controls.")})
        for action in actions[:max(0, BUDGETS[self.args.suite] - self.steps)]:
            if self.success:
                break
            self.step(action)
        return {"status": "executed", "success": self.success, "steps": self.steps}


    def reuse_success(self, command):
        result = json.loads(Path(command["result_file"]).read_text())
        observation = result["observation"]
        if (not observation["success"] or observation["case"] != self.case
                or observation["attempt"] != self.attempt
                or observation["initial_state_sha256"] != self.initial_hash
                or observation["budget"] != BUDGETS[self.args.suite]
                or observation["policy_checkpoint"] != self.client.metadata["checkpoint"]
                or observation["policy_config"] != self.client.metadata["config"]
                or any(chunk["guided"] or chunk["rgbd_reprojection"] is not None or chunk["subgoal"] is not None
                       for chunk in result["chunks"])):
            raise ValueError("Cached native success does not match this evaluation protocol")
        report = {"task": self.task.language, "case": self.case, "status": "success", "success": True,
                  "keypoint_reasoning": [], "tool_calls": [{"tool": "pi05_native", "status": "success",
                    "execution_source": "previous_verified_execution", "result_file": command["result_file"],
                    "steps": observation["steps"]}], "video": observation["video"],
                  "token_usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None,
                                  "status": "unavailable", "reason": "No exact per-task session counter exposed."}}
        destination = self.folder / f"{self.case['case_id']}-cached.task.json"
        save_json(destination, report)
        return {"status": "reused_native_success", "task_success": True, "executed_controls": 0,
                "task_report": str(destination)}


    def command(self, command):
        kind = command["type"]
        if kind == "reset":
            if self.tool_calls:
                self.task_report()
            self.reset(int(command["attempt"]))
            result = {"status": "reset"}
        elif kind == "task":
            if self.tool_calls:
                self.task_report()
            self.args.suite, self.args.task_index = command["suite"], int(command["task_index"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.suite = benchmark.get_benchmark_dict()[self.args.suite]()
            self.task = self.suite.get_task(self.args.task_index)
            self.case = {"case_id": f"{self.args.suite}-{self.args.task_index:04d}-reset{self.args.seed}",
                         "suite": self.args.suite, "seed": self.args.seed}
            self.reset(int(command.get("attempt", 0)))
            result = {"status": "task_ready"}
        elif kind == "vla":
            result = self.vla(command)
        elif kind == "controls":
            result = self.controls(command)
        elif kind == "servo":
            result = self.servo(command)
        elif kind == "evaluate":
            result = self.evaluate(command)
        elif kind == "reuse_success":
            result = self.reuse_success(command)
        elif kind == "observe":
            result = {"status": "observed"}
        else:
            raise ValueError(f"Unknown command {kind}")
        result["observation"] = self.snapshot()
        if self.success or self.steps >= BUDGETS[self.args.suite]:
            result["task_report"] = str(self.video_path.with_suffix(".task.json"))
            self.task_report()
        return serializable(result)


    def close(self):
        self.task_report()
        if self.video is not None:
            self.video.close()
        if self.env is not None:
            self.env.close()
        self.client.close()



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="libero_goal")
    parser.add_argument("--task-index", type=int, default=2015)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--initial-index", type=int, default=0)
    parser.add_argument("--port", type=int, default=18414)
    parser.add_argument("--max-goal-distance", type=float, default=0.25)
    parser.add_argument("--residual-position", type=float, default=0.10)
    parser.add_argument("--residual-rotation", type=float, default=1.2)
    parser.add_argument("--residual-gripper", type=float, default=0.04)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    session = Session(args)
    log({"event": "ready", "folder": str(args.output), "task": session.task.language})
    last_heartbeat = time.monotonic()
    try:
        while True:
            requests = sorted(args.output.glob("command-*.json"))
            for request in requests:
                response = request.with_name(request.name.replace("command-", "response-"))
                if response.exists():
                    continue
                command = json.loads(request.read_text())
                if command["type"] == "stop":
                    save_json(response, {"status": "stopped", "steps": session.steps, "success": session.success})
                    return
                result = session.command(command)
                save_json(response, result)
                log({"event": "response", "request": request.name, "status": result["status"],
                     "step": session.steps, "success": session.success})
            if time.monotonic() - last_heartbeat >= 60:
                log({"event": "waiting_for_tool_command", "step": session.steps, "attempt": session.attempt})
                last_heartbeat = time.monotonic()
            time.sleep(0.2)
    finally:
        session.close()


if __name__ == "__main__":
    main()

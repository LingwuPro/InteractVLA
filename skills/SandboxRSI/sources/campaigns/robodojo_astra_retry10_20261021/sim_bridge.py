"""Pause the native evaluator for explicit tool calls; preserve physics and scoring."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import types
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import transforms3d as t3d
from release_paths import resolve_path as _release_path

BASE = Path(_release_path('/workspace'))
ROBO = BASE / "RoboDojo"
MAIN = ROBO / "src/eval_client/main.py"
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# Arrays/tensors/nested values -> lossless JSON-compatible numerical values.
def plain(value: Any) -> Any:
    if hasattr(value, "detach"):
        return value.detach().cpu().tolist()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


# Dictionary and destination -> atomic, world-writable JSON artifact.
def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(plain(value), allow_nan=False) + "\n")
    temporary.chmod(0o777)
    temporary.replace(path)


class Session:
    # Native single environment -> persistent, exactly-once file tool session.
    def __init__(self, env: Any) -> None:
        self.env = env
        self.root = Path(os.environ["RD_CONTROL_DIR"])
        self.case_id = f"{env.task_name}/seed{env.eval_seed}/layout{int(env.env_seeds[0])}"
        self.attempt = int(os.environ.get("RD_ATTEMPT", "0"))
        self.folder = self.root / f"layout{int(env.env_seeds[0])}_attempt{self.attempt}"
        self.folder.mkdir(parents=True, exist_ok=True)
        assert not (self.folder / "ready.json").exists(), "Use a fresh session directory"
        self.events: list[dict[str, Any]] = []
        self.answer_accesses = 0
        self.sequence = 0
        self.started = time.perf_counter()
        self.initial_state = plain(env.get_obs()["state"])
        self.state_cache = self.initial_state
        self.arm_targets = {side: np.asarray(self.initial_state[f"{side}_arm_joint_state"])
                            for side in ("left", "right")}
        self.metadata_cache: dict[str, Any] = {}
        self.gripper_targets = {side: float(self.initial_state[f"{side}_ee_joint_state"][0])
                                for side in ("left", "right")}
        self.render_stride = max(16, int(os.environ.get("RD_RENDER_STRIDE", "16")))
        self.context = env.model_client.call("set_context", {
            "case_id": self.case_id, "task": env.task_name, "eval_seed": env.eval_seed,
            "layout_id": int(env.env_seeds[0]), "attempt": self.attempt,
        })

    # Current simulator state -> RGB paths, robot state and official terminal signal.
    def observe(self) -> dict[str, Any]:
        obs = self.env.get_obs()
        self.state_cache = plain(obs["state"])
        images = {}
        low_variance = []
        for name, camera in obs["vision"].items():
            image = np.asarray(camera["color"])
            deviation = float(image.std())
            if deviation <= 1:
                low_variance.append(name)
            path = self.folder / f"{name}.png"
            cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
            images[name] = {
                "path": str(path), "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "std": deviation, "mean": float(image.mean()), "uniform": deviation <= 1,
            }
        assert len(low_variance) < len(images), "All cameras are uniform after synchronized capture"
        if self.env.take_action_cnt[0] == 0:
            assert not low_variance, f"Invalid initial cameras: {low_variance}"
        result = {
            "case_id": self.case_id, "instruction": obs["instruction"], "images": images,
            "state": obs["state"], "executed_steps": int(self.env.take_action_cnt[0]),
            "step_limit": int(self.env.step_lim), "terminal": bool(self.env.end_flag[0]),
            "success": bool(self.env.end_flag[0] and self.env.success[0]),
            "benchmark_invalid": 0 in self.env.unstable_envs,
            "commanded_arm_joint_targets": self.arm_targets,
        }
        write_json(self.folder / "observation.json", result)
        return plain(result)

    # Authorized scene query -> live object poses and asset functional-point metadata.
    def scene(self) -> dict[str, Any]:
        manager = self.env.scene_manager.layout_manager
        objects = {}
        for kind in ("Rigid", "Geometry", "Articulation", "Garment"):
            for record in manager.get_layout_records(0, kind):
                name = record["inst_name"]
                label = record.get("label", name)
                position, quaternion = manager.get_instance_pose(env_idx=0, inst_name=name)
                if name not in self.metadata_cache:
                    self.metadata_cache[name] = manager.get_instance_metadata(env_idx=0, inst_name=name)
                metadata = self.metadata_cache[name]
                objects[label] = {
                    "instance": name, "kind": kind, "position": position,
                    "quaternion_wxyz": quaternion, "metadata": metadata,
                }
                if kind == "Articulation":
                    articulation = manager.get_scene_object(inst_name=name, env_idx=0)
                    objects[label]["physical_joints"] = articulation.get_all_joints_info()
        return plain({"privileged_simulator_state": True, "objects": objects})

    # Native task fields and pending reward descriptions -> read-only internal-answer evidence.
    def answers(self) -> dict[str, Any]:
        self.answer_accesses += 1
        names = ("target_label", "aim_label", "target_place_tag", "push", "push_idx", "kong",
                 "query_support_times", "support_checked", "pending_support_check")
        fields = {name: plain(getattr(self.env, name)) for name in names if hasattr(self.env, name)}
        reward = self.env.reward_manager
        result = {
            "oracle_answer_access": True, "case_id": self.case_id, "task_fields": fields,
            "remaining_support_actions": [len(queue) for queue in getattr(self.env, "support_arm_action", [])],
            "pending_native_checks": plain(reward.check_list[0]),
            "native_failure_queries": plain(reward.query_list[0]),
            "native_score_completed_count": plain(reward.score_completed_count[0]),
        }
        return json.loads(json.dumps(result, default=lambda value: f"<{type(value).__name__}>"))

    # Native action dictionaries [T] -> actual execution count and fresh observation.
    def execute(self, actions: list[dict[str, Any]], stop_on_query_change: bool = False) -> dict[str, Any]:
        before = self.env.take_action_cnt[0]
        query_counts = [query[2] for query in self.env.reward_manager.query_list[0]]
        for index, action in enumerate(actions):
            if self.env.is_episode_end():
                break
            self.env.take_action(action)
            for side in ("left", "right"):
                self.arm_targets[side] = np.asarray(action[f"{side}_arm_joint_state"])
                self.gripper_targets[side] = float(action[f"{side}_ee_joint_state"][0])
            if stop_on_query_change and query_counts != [query[2] for query in self.env.reward_manager.query_list[0]]:
                break
            if (index + 1) % self.render_stride == 0 and index + 1 < len(actions):
                self.env.get_obs()
        result = {"executed_steps": self.env.take_action_cnt[0] - before, "observation": self.observe()}
        if self.answer_accesses:
            result["native_query_counts"] = [{"required": plain(query[1]), "actual": plain(query[2])}
                                             for query in self.env.reward_manager.query_list[0]]
        return result

    # Native robot state -> concatenated physical joint/gripper vector [14].
    def joints(self) -> np.ndarray:
        return np.concatenate([
            np.concatenate([self.arm_targets[side].reshape(-1),
                            np.array([self.gripper_targets[side]])])
            for side in ("left", "right")
        ])

    # Physical joints [14] -> native dual-arm joint action dictionary.
    def action(self, joints: np.ndarray) -> dict[str, Any]:
        return {
            "left_arm_joint_state": joints[:6].tolist(), "left_ee_joint_state": [float(joints[6])],
            "right_arm_joint_state": joints[7:13].tolist(), "right_ee_joint_state": [float(joints[13])],
        }

    # Named arm and world TCP pose [7] -> physical dual-arm joint goal [14], IK status.
    def joint_goal(self, command: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
        side = command["arm"]
        pose = np.asarray(command["pose"], dtype=float).copy()
        robot = self.env.robot_manager.get_robot_by_arm_name(f"{side}_arm")
        if command.get("frame", "tcp") == "tcp":
            pose[:3] -= t3d.quaternions.quat2mat(pose[3:]) @ np.array([robot.gripper_bias, 0, 0])
        result = self.env.robot_manager.solve_ik(pose.tolist(), 0, robot)
        target = self.joints()
        entity = self.env.robot_manager.robot_key[self.env.robot_manager.robot_list.index(robot)]
        limits = entity.data.joint_pos_limits[0, robot.arm_joint_indices].detach().cpu().numpy()
        raw_goal = None
        if result["status"] == "Success":
            offset = 0 if side == "left" else 7
            raw_goal = np.asarray(result["joint_value"]).reshape(6)
            options = raw_goal[None] + 2 * math.pi * np.arange(-3, 4)[:, None]
            valid = (options >= limits[:, 0]) & (options <= limits[:, 1])
            if not valid.any(0).all():
                result["status"] = "Fail_actual_joint_limits"
            else:
                cost = np.where(valid, np.abs(options - target[offset:offset + 6]), np.inf)
                selected = np.argmin(cost, axis=0)
                target[offset:offset + 6] = options[selected, np.arange(6)]
        return target, plain({
            "status": result["status"], "flange_pose": pose, "joint_goal": target,
            "raw_ik_joint_goal": raw_goal, "actual_joint_limits": limits,
        })

    # Tool request -> complete return including actual motion feedback.
    def call(self, command: dict[str, Any]) -> dict[str, Any]:
        operation = command["type"]
        if operation == "observe":
            return self.observe()
        if operation == "scene":
            return self.scene()
        if operation == "answers":
            return self.answers()
        if operation == "links":
            manager = self.env.scene_manager.layout_manager
            instance = manager.get_instance_name(label=command["label"], env_idx=0)
            articulation = manager.get_scene_object(inst_name=instance, env_idx=0)
            origin = np.asarray(plain(manager.scene_manager.env_origins[0]))
            poses = {}
            for name in command["names"]:
                pose = np.asarray(articulation.get_link_pose(name)).copy()
                pose[:3] -= origin
                poses[name] = pose
            return plain({"label": command["label"], "link_poses_world": poses})
        if operation == "vla":
            target = command.get("target")
            scale = float(command.get("scale", 0))
            goal_result = self.env.model_client.call("set_goal", {"target": target, "scale": scale})
            results = []
            for _ in range(int(command.get("chunks", 1))):
                if self.env.is_episode_end():
                    break
                self.env.model_client.call("update_obs", self.env.get_obs())
                actions = self.env.model_client.call("get_action")
                model_result = self.env.model_client.call("get_tool_result")
                feedback = self.execute(actions)
                results.append({"model_return": model_result, "execution": feedback})
            return {"goal_return": goal_result, "chunks": results, "observation": self.observe()}
        if operation == "ik":
            _, result = self.joint_goal(command)
            return result
        if operation == "motion_plan":
            side = command["arm"]
            robot = self.env.robot_manager.get_robot_by_arm_name(f"{side}_arm")
            planner = self.env.robot_manager.planner[robot.robot_name]
            result = planner.plan_joint(np.asarray(self.state_cache[f"{side}_arm_joint_state"]),
                                        np.asarray(command["joint_goal"]))
            return plain({**result, "sample_dt": planner.dt,
                          "action_dt": self.env.dt * int(self.env.obs_manager.collect_interval)})
        if operation == "controls":
            return self.execute(command["actions"], command.get("stop_on_query_change", False))
        if operation == "move":
            start = self.joints()
            target, result = self.joint_goal(command)
            if result["status"] != "Success":
                return {"ik_return": result, "executed_steps": 0, "observation": self.observe()}
            steps = max(1, math.ceil(float(np.max(np.abs(target - start))) / 0.05))
            actions = [self.action(start + (target - start) * (index + 1) / steps) for index in range(steps)]
            return {"ik_return": result, **self.execute(actions)}
        if operation == "grip":
            start = self.joints()
            target = start.copy()
            index = 6 if command["arm"] == "left" else 13
            target[index] = float(command["opening"])
            steps = max(5, math.ceil(abs(target[index] - start[index]) / 0.1))
            return self.execute([
                self.action(start + (target - start) * (i + 1) / steps) for i in range(steps)
            ])
        if operation == "home":
            state = self.initial_state
            target = np.concatenate([
                np.concatenate([np.asarray(state[f"{side}_arm_joint_state"]).reshape(-1), np.array([1.0])])
                for side in ("left", "right")
            ])
            start = self.joints()
            steps = max(1, math.ceil(float(np.max(np.abs(target - start))) / 0.05))
            return self.execute([self.action(start + (target - start) * (i + 1) / steps) for i in range(steps)])
        if operation == "wait":
            return self.execute([self.action(self.joints())] * int(command["steps"]))
        if operation == "stop":
            raise SystemExit("Sandbox stopped by controller without assigning a score")
        raise ValueError(f"Unknown tool: {operation}")

    # Ordered atomic request files -> ordered responses; successful episode JSON only.
    def run(self) -> None:
        initial = self.observe()
        write_json(self.folder / "ready.json", {
            "case_id": self.case_id, "attempt": self.attempt, "context_return": self.context,
            "observation": initial, "initial_state": self.initial_state,
            "native_motion_enabled": os.environ.get("RD_ENABLE_NATIVE_MOTION", "1") == "1",
            "internal_answers_available": True,
            "link_pose_query_available": True,
        })
        print(json.dumps({"event": "tool_ready", "folder": str(self.folder)}), flush=True)
        last_activity = time.monotonic()
        while not self.env.is_episode_end():
            request = self.folder / f"command-{self.sequence:05d}.json"
            if not request.exists():
                if time.monotonic() - last_activity > 900:
                    raise TimeoutError("Controller inactive for 900 seconds")
                time.sleep(0.02)
                continue
            command = json.loads(request.read_text())
            assert int(command["id"]) == self.sequence
            started = time.perf_counter()
            result = self.call(command)
            event = {
                "id": self.sequence, "tool": command["type"], "arguments": command,
                "return_value": result, "wall_seconds": time.perf_counter() - started,
            }
            self.events.append(event)
            write_json(self.folder / f"response-{self.sequence:05d}.json", event)
            self.sequence += 1
            last_activity = time.monotonic()
        final = self.observe()
        if final["success"] and not final["benchmark_invalid"]:
            write_json(self.folder / "success.json", {
                "case_id": self.case_id, "attempt": self.attempt, "success": True,
                "information_access": ("RGB_simulator_coordinates_and_internal_answers" if self.answer_accesses
                                       else "RGB_and_authorized_simulator_coordinates"),
                "internal_answer_accesses": self.answer_accesses,
                "context_return": self.context, "initial_observation": initial,
                "sim_bridge_sha256": SOURCE_SHA256, "render_stride": self.render_stride,
                "final_observation": final, "tool_calls": self.events,
                "wall_seconds": time.perf_counter() - self.started,
                "token_usage": None, "token_usage_status": "unavailable",
            })
        write_json(self.folder / "terminal.json", {
            "case_id": self.case_id, "attempt": self.attempt,
            "success": final["success"], "benchmark_invalid": final["benchmark_invalid"],
            "executed_steps": final["executed_steps"], "tool_calls": self.sequence,
            "internal_answer_accesses": self.answer_accesses,
            "native_query_counts": ([{"required": plain(query[1]), "actual": plain(query[2])}
                                     for query in self.env.reward_manager.query_list[0]]
                                    if self.answer_accesses else None),
        })


# Native factory -> factory that replaces only the policy execution method.
def build_bridge(native_factory: Any) -> Any:
    # Original factory arguments -> native environment with persistent interactive control.
    def create(*args: Any, **kwargs: Any) -> Any:
        env = native_factory(*args, **kwargs)
        original = env.seed_manager.init_eval

        # Existing layout index -> frozen explicitly selected layout IDs.
        def select_layouts(*selection_args: Any, **selection_kwargs: Any) -> None:
            original(*selection_args, **selection_kwargs)
            ids = json.loads(os.environ["RD_LAYOUT_IDS"])
            assert all(layout in env.seed_manager.seed_info for layout in ids)
            env.seed_manager.seed_list = ids
            env.seed_manager.idx = 0
            env.seed_manager.st_idx = 0
            env.seed_manager.ed_idx = len(ids)

        env.seed_manager.init_eval = select_layouts
        select_layouts()

        # Initialized native environment -> session-controlled policy rollout.
        def episode(instance: Any) -> None:
            Session(instance).run()

        env.eval_one_episode = types.MethodType(episode, env)

        # Native camera frame -> no video artifact; observations and physics remain unchanged.
        def discard_video(instance: Any, env_idx: int, frame: Any) -> None:
            return None

        env._stream_vision = types.MethodType(discard_video, env)
        return env

    return create


if __name__ == "__main__":
    os.umask(0)
    source = MAIN.read_text()
    anchor = "from src.eval_client.eval_env import create_eval_env\n"
    assert source.count(anchor) == 1
    hook = anchor + "from sim_bridge import build_bridge\ncreate_eval_env = build_bridge(create_eval_env)\n"
    globals()["__file__"] = str(MAIN)
    exec(compile(source.replace(anchor, hook), str(MAIN), "exec"), globals())

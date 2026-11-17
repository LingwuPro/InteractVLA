import argparse
import contextlib
import io
import json
import os
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import h5py
import imageio.v2 as imageio
import numpy as np
from libero.libero import benchmark, get_libero_path
from libero.libero.envs import OffScreenRenderEnv
from scipy.spatial.transform import Rotation

from .gt_libero import base_task_name, transfer_demo_state
from .libero_control import LiberoPoseController, measured_base_pose, register_gripper
from .protocol import manifest_hash

SUITES = ("libero_spatial", "libero_object", "libero_goal", "libero_10")


class RenderingError(RuntimeError):
    pass



def validate_rendered_observation(observation):
    for name in ("agentview_image", "robot0_eye_in_hand_image"):
        pixels = np.asarray(observation[name])
        if pixels.ndim != 3 or pixels.shape[-1] != 3 or not np.isfinite(pixels).all() or not np.any(pixels):
            raise RenderingError(
                f"Invalid rendered observation: {name}, shape={pixels.shape}, all_zero={not np.any(pixels)}"
            )
    return observation


class CheckedRenderEnv:

    def __init__(self, environment):
        self._environment = environment


    def __getattr__(self, name):
        return getattr(self._environment, name)


    def reset(self):
        return validate_rendered_observation(self._environment.reset())


    def set_init_state(self, initial_state):
        return validate_rendered_observation(self._environment.set_init_state(initial_state))


    def step(self, action):
        observation, reward, done, info = self._environment.step(action)
        return validate_rendered_observation(observation), reward, done, info



def log(event: dict) -> None:
    print(json.dumps({"timestamp": datetime.now().astimezone().isoformat(), **event}), flush=True)



def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.partial")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)



def cases_for(args) -> tuple[list[dict], dict]:
    classification = {}
    if args.benchmark == "libero_plus":
        root = Path(get_libero_path("benchmark_root"))
        classification = json.loads((root / "benchmark/task_classification.json").read_text())
    suites, cases = {}, []
    for suite_name in SUITES:
        with contextlib.redirect_stdout(io.StringIO()):
            suite = benchmark.get_benchmark_dict()[suite_name]()
        suites[suite_name] = suite
        names = [p.name.removesuffix("_demo.hdf5") for p in (Path(args.hdf5_root) / suite_name).glob("*_demo.hdf5")]
        categories = {entry["name"]: entry["category"] for entry in classification.get(suite_name, [])}
        for task_index in range(suite.n_tasks):
            task = suite.get_task(task_index)
            category = categories[task.name] if categories else "Original"
            if category == "Robot Initial States":
                continue
            base = base_task_name(task.name, names)
            cases.append(
                {
                    "suite": suite_name,
                    "task_index": task_index,
                    "task_name": task.name,
                    "base_task": base,
                    "category": category,
                    "demo_id": args.demo,
                    "seed": args.seed,
                    "benchmark": args.benchmark,
                    "case_id": f"{suite_name}-{task_index:04d}-demo{args.demo}-seed{args.seed}",
                    "hdf5": str(Path(args.hdf5_root).resolve() / suite_name / (base + "_demo.hdf5")),
                }
            )
    return cases, suites



def make_env(task, seed: int, absolute: bool):
    options = {"gripper_types": register_gripper()} if absolute else {}
    np.random.seed(seed)
    env = CheckedRenderEnv(OffScreenRenderEnv(
        bddl_file_name=str(Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file),
        camera_heights=224,
        camera_widths=224,
        control_freq=20,
        **options,
    ))
    env.seed(seed)
    try:
        env.reset()
    except RenderingError:
        env.close()
        raise
    return env



def replay_reference(case: dict, suite, output: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    cache, marker = output / "reference.npz", output / "reference.json"
    if cache.is_file() and marker.is_file():
        with np.load(cache, allow_pickle=False) as values:
            return values["poses"], values["initial_state"], json.loads(marker.read_text())
    with h5py.File(case["hdf5"], "r") as source:
        demo = source[f"data/demo_{case['demo_id']}"]
        actions, initial = demo["actions"][:], demo["states"][0]
        model_xml = demo.attrs["model_file"]
        source_control = json.loads(source["data"].attrs["env_args"])["env_kwargs"]["control_freq"]
        if source_control != 20:
            raise ValueError(f"Reference control frequency is {source_control}, expected 20 Hz")
    env = make_env(suite.get_task(case["task_index"]), case["seed"], absolute=False)
    try:
        initial_source = "hdf5_demo_states_0"
        if case["category"] == "Objects Layout":
            states = suite.get_task_init_states(case["task_index"])
            initial = transfer_demo_state(initial, model_xml, np.asarray(states[0]), env.env.sim.model)
            initial_source = "hdf5_shared_joints_with_variant_added_object_joints"
        actual_size = len(env.env.sim.get_state().flatten())
        if len(initial) != actual_size:
            raise ValueError(f"Initial state size {len(initial)} differs from simulator {actual_size}")
        observation = env.set_init_state(initial)
        initial = env.env.sim.get_state().flatten().copy()
        robot = env.env.robots[0]
        body = env.env.sim.model.body_name2id(robot.robot_model.root_body)
        base_position = env.env.sim.data.body_xpos[body].copy()
        base_rotation = Rotation.from_matrix(env.env.sim.data.body_xmat[body].reshape(3, 3))
        poses = [measured_base_pose(observation, base_position, base_rotation)]
        successful = False
        for index, action in enumerate(actions):
            observation, _, done, _ = env.step(action)
            poses.append(measured_base_pose(observation, base_position, base_rotation))
            successful |= bool(done)
            if (index + 1) % 100 == 0:
                log({"case": case["case_id"], "stage": "reference", "step": index + 1})
        record = {
            "reference_success": successful,
            "reference_steps": len(actions),
            "initial_state_source": initial_source,
            "control_period_s": 0.05,
            "reference_source": "native_hdf5_actions_replayed_in_target_variant",
            "initial_state_hash": manifest_hash({"state": initial.tolist()}),
            "base_position": base_position.tolist(),
            "base_rotation": base_rotation.as_matrix().tolist(),
            "hdf5_identity": {
                "path": case["hdf5"],
                "size": Path(case["hdf5"]).stat().st_size,
                "mtime_ns": Path(case["hdf5"]).stat().st_mtime_ns,
            },
        }
        values = np.asarray(poses)
        np.savez_compressed(cache, poses=values, initial_state=initial)
        save_json(marker, record)
        return values, initial, record
    finally:
        env.close()

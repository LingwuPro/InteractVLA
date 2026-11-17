"""Exercise a real LIBERO environment, optionally with an OpenPI policy."""

import argparse
import json
import time
from collections import deque
from pathlib import Path

import numpy as np
from libero.libero import benchmark, get_libero_path
from libero.libero.envs import OffScreenRenderEnv
from PIL import Image
from policy_client import PolicyClient
from scipy.spatial.transform import Rotation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="libero_spatial")
    parser.add_argument("--task", type=int, default=0)
    parser.add_argument("--episode", type=int, default=0)
    parser.add_argument("--steps", type=int, default=220)
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    suite = benchmark.get_benchmark_dict()[args.suite]()
    task = suite.get_task(args.task)
    env = OffScreenRenderEnv(
        bddl_file_name=str(Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file),
        camera_heights=256, camera_widths=256,
    )
    env.seed(0)
    env.reset()
    obs = env.set_init_state(suite.get_task_init_states(args.task)[args.episode])
    hold = np.array([0., 0., 0., 0., 0., 0., -1.])
    for _ in range(10):
        obs, _, _, _ = env.step(hold)
    Image.fromarray(obs["agentview_image"][::-1, ::-1]).save(args.output / "initial.png")
    policy = None
    if args.port is not None:
        policy = PolicyClient("127.0.0.1", args.port)
    queue: deque = deque()
    success = False
    start = time.monotonic()
    for step in range(args.steps):
        if policy is not None and not queue:
            state = np.concatenate((obs["robot0_eef_pos"],
                                    Rotation.from_quat(obs["robot0_eef_quat"]).as_rotvec(),
                                    obs["robot0_gripper_qpos"]))
            result = policy.infer({
                "observation/image": np.ascontiguousarray(obs["agentview_image"][::-1, ::-1]),
                "observation/wrist_image": np.ascontiguousarray(obs["robot0_eye_in_hand_image"][::-1, ::-1]),
                "observation/state": state.astype(np.float32), "prompt": task.language,
            })
            actions = np.asarray(result["actions"])
            if actions.ndim != 2 or actions.shape[1] != 7 or not np.isfinite(actions).all():
                raise ValueError(f"Invalid LIBERO actions: {actions.shape}")
            queue.extend(actions[:10])
        action = queue.popleft() if policy is not None else hold
        obs, _, done, _ = env.step(action.tolist())
        if step % 20 == 0:
            print(f"step={step + 1}/{args.steps} success={done}", flush=True)
        if done:
            success = True
            break
    Image.fromarray(obs["agentview_image"][::-1, ::-1]).save(args.output / "final.png")
    result = {"suite": args.suite, "task": args.task, "episode": args.episode,
              "instruction": task.language, "completed_steps": step + 1,
              "success": success, "seconds": time.monotonic() - start,
              "policy": "pi05_libero" if policy is not None else "hold_smoke_only"}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    env.close()
    if policy is not None:
        policy.close()


if __name__ == "__main__":
    main()

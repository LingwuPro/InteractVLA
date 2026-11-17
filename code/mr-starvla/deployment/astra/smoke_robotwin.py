"""Run RoboTwin with its native joint controller and optional StarVLA server."""

import argparse
import importlib
import json
import time
from pathlib import Path

import numpy as np
import yaml
from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="adjust_bottle")
    parser.add_argument("--seed", type=int, default=100001)
    parser.add_argument("--instruction", default="Adjust the bottle to an upright position.")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = yaml.safe_load(Path("task_config/demo_clean.yml").read_text())
    bodies = yaml.safe_load(Path("task_config/_embodiment_config.yml").read_text())
    body = bodies[config["embodiment"][0]]["file_path"]
    camera = yaml.safe_load(Path("task_config/_camera_config.yml").read_text())[config["camera"]["head_camera_type"]]
    config.update(task_name=args.task, task_config="demo_clean", ckpt_setting="astra_smoke",
                  left_robot_file=body, right_robot_file=body, dual_arm_embodied=True,
                  left_embodiment_config=yaml.safe_load((Path(body) / "config.yml").read_text()),
                  right_embodiment_config=yaml.safe_load((Path(body) / "config.yml").read_text()),
                  head_camera_h=camera["h"], head_camera_w=camera["w"],
                  eval_mode=True, eval_video_log=False, render_freq=0, collect_data=False)
    env = getattr(importlib.import_module("envs." + args.task), args.task)()
    start = time.monotonic()
    env.setup_demo(now_ep_num=0, seed=args.seed, is_test=True, **config)
    env.set_instruction(args.instruction)
    observation = env.get_obs()
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("head_camera", "left_camera", "right_camera"):
        Image.fromarray(observation["observation"][name]["rgb"]).save(args.output / (name + ".png"))
    initial = np.asarray(observation["joint_action"]["vector"])
    print("RESET", initial.shape, flush=True)
    if args.port is None:
        for _ in range(args.steps):
            env.take_action(initial, action_type="qpos")
    else:
        from examples.RoboTwinMem.eval_files.client import ModelClient
        from examples.RoboTwinMem.eval_files.client import eval as policy_step

        model = ModelClient("127.0.0.1", args.port)
        while env.take_action_cnt < env.step_lim and not env.eval_success:
            policy_step(env, model, env.get_obs())
    result = {"task": args.task, "seed": args.seed, "steps": env.take_action_cnt,
              "success": bool(env.check_success()), "seconds": time.monotonic() - start,
              "policy": "starvla_50k" if args.port else "hold_smoke_only",
              "expert_filtered": False}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    env.close_env()


if __name__ == "__main__":
    main()

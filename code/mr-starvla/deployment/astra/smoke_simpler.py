"""Exercise SimplerEnv RGB-D observations and the native delta-pose controller."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="widowx_put_eggplant_in_basket")
    parser.add_argument("--backend", choices=["simpler", "simpler_ms3"], default="simpler")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = time.monotonic()
    if args.backend == "simpler":
        import simpler_env

        env = simpler_env.make(args.task, renderer_kwargs={"device": "cuda:0", "offscreen_only": True})
    else:
        import gymnasium as gym
        import mani_skill.envs.tasks.digital_twins.bridge_dataset_eval  # noqa: F401
        import sapien

        # SAPIEN 3 requires automatic device selection for software Vulkan.
        render_system = sapien.render.RenderSystem
        sapien.render.RenderSystem = lambda device=None: render_system()

        tasks = {
            "widowx_put_eggplant_in_basket": "PutEggplantInBasketScene-v1",
            "widowx_carrot_on_plate": "PutCarrotOnPlateInScene-v1",
            "widowx_stack_cube": "StackGreenCubeOnYellowCubeBakedTexInScene-v1",
            "widowx_spoon_on_towel": "PutSpoonOnTableClothInScene-v1",
        }
        env = gym.make(tasks[args.task], obs_mode="rgb+segmentation", num_envs=1,
                       sim_backend="physx_cpu", render_backend="sapien_cpu")
    obs, _ = env.reset(seed=0)
    print("RESET", list(obs), env.action_space, flush=True)
    for step in range(args.steps):
        action = np.array([0., 0., 0., 0., 0., 0., 1.], dtype=np.float32)
        obs, _, terminated, truncated, info = env.step(action)
        if step % 10 == 0:
            print("STEP", step, terminated, truncated, flush=True)
        if terminated or truncated:
            break
    args.output.mkdir(parents=True, exist_ok=True)
    images = obs["image"] if args.backend == "simpler" else obs["sensor_data"]
    for name, data in images.items():
        rgb = data["rgb"] if args.backend == "simpler" else data["rgb"][0].cpu().numpy()
        Image.fromarray(rgb).save(args.output / (name + ".png"))
    result = {"task": args.task, "reset": True, "steps": step + 1,
              "seconds": time.monotonic() - start, "info": str(info), "policy": "hold_smoke_only",
              "backend": args.backend, "original_simpler_protocol": args.backend == "simpler"}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    env.close()


if __name__ == "__main__":
    main()

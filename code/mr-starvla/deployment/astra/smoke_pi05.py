"""Check native OpenPI websocket inference without driving a robot."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
from policy_client import PolicyClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["pi05_base", "pi05_libero"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rng = np.random.default_rng(0)
    if args.model == "pi05_base":
        obs = {
            "state": np.zeros(14, dtype=np.float32),
            "images": {name: rng.integers(0, 256, (3, 224, 224), dtype=np.uint8)
                       for name in ("cam_high", "cam_left_wrist", "cam_right_wrist")},
            "prompt": "Pick up the object.",
        }
        expected = (50, 14)
    else:
        obs = {
            "observation/state": np.zeros(8, dtype=np.float32),
            "observation/image": rng.integers(0, 256, (224, 224, 3), dtype=np.uint8),
            "observation/wrist_image": rng.integers(0, 256, (224, 224, 3), dtype=np.uint8),
            "prompt": "Pick up the black bowl.",
        }
        expected = (10, 7)
    policy = PolicyClient("127.0.0.1", args.port)
    elapsed = []
    for _ in range(2):
        start = time.monotonic()
        result = policy.infer(obs)
        actions = np.asarray(result["actions"])
        if actions.shape != expected or not np.isfinite(actions).all():
            raise ValueError(f"Invalid native action tensor: {actions.shape}, expected {expected}")
        elapsed.append(time.monotonic() - start)
        print(args.model, actions.shape, elapsed[-1], flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    np.save(args.output / "actions.npy", actions)
    record = {"model": args.model, "action_shape": list(expected), "finite": True,
              "inference_seconds": elapsed, "input": "synthetic", "robot_execution": False}
    (args.output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    policy.close()


if __name__ == "__main__":
    main()

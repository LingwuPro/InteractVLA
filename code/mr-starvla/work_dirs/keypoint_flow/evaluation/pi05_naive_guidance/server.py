"""Frozen official pi05 with naive geometric input-gradient guidance."""

import argparse
import logging
import time
from pathlib import Path

import jax
import jax.numpy as jnp
import numpy as np
from openpi.models import model as model_api
from openpi.policies import policy_config
from openpi.serving.websocket_policy_server import WebsocketPolicyServer
from openpi.shared import nnx_utils
from openpi.training import checkpoints
from openpi.training.config import get_config
from openpi_client.base_policy import BasePolicy
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.geometry import (
    evaluate_final,
)
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.sampler import NaiveSampler


# Unbatched external geometric request -> validated JAX geometry, independent of the model context.
def prepare_geometry(request: dict) -> dict:
    shapes = {"current_position": (3,), "current_rotation": (3, 3),
              "target_position": (4, 3), "target_rotation": (4, 3, 3), "target_gripper": (4,),
              "input_min": (6,), "input_max": (6,), "output_min": (6,), "output_max": (6,)}
    result = {}
    for name, shape in shapes.items():
        value = np.asarray(request[name], dtype=np.float32)
        if value.shape != shape or not np.isfinite(value).all():
            raise ValueError(f"Invalid geometry field {name}: expected {shape}")
        if "rotation" in name:
            if not np.allclose(value @ np.swapaxes(value, -1, -2), np.eye(3), atol=1e-5):
                raise ValueError(f"Invalid rotation in {name}")
            if not np.allclose(np.linalg.det(value), 1, atol=1e-5):
                raise ValueError(f"Rotation determinant differs from +1 in {name}")
        batched = name.startswith(("current_", "target_"))
        result[name] = jnp.asarray(value[None] if batched else value)
    steps = np.asarray(request["steps"])
    if not np.array_equal(steps, [2, 5, 8, 10]):
        raise ValueError("This experiment uses fixed execution keypoints [2,5,8,10]")
    if np.any(np.asarray(result["input_max"]) <= np.asarray(result["input_min"])):
        raise ValueError("Controller input ranges must be positive")
    result["steps"] = jnp.asarray(steps, dtype=jnp.int32)
    return result


class GuidedPolicy(BasePolicy):
    # Official checkpoint -> frozen native pi05 and JIT functions that differentiate only their action input.
    def __init__(self, checkpoint: Path):
        config = get_config("pi05_libero")
        data = config.data.create(config.assets_dirs, config.model)
        if not data.use_quantile_norm or data.asset_id is None:
            raise ValueError("Expected official LIBERO quantile action normalization")
        stats = checkpoints.load_norm_stats(checkpoint / "assets", data.asset_id)["actions"]
        self.policy = policy_config.create_trained_policy(config, checkpoint)
        self.sampler = NaiveSampler(self.policy._model, stats.q01, stats.q99)
        self.sample = nnx_utils.module_jit(self.sampler.sample, static_argnames=("guided", "num_steps"))
        self.probe = nnx_utils.module_jit(self.sampler.probe)
        self.final_errors = jax.jit(evaluate_final)
        self.metadata = {
            "checkpoint": str(checkpoint.resolve()), "config": "pi05_libero", "action_horizon": 10,
            "action_dim": 32, "noise": "standard_gaussian_all_dimensions", "num_steps": 10,
            "sampler": "original_native_Euler_grid_plus_uncapped_input_gradient", "weights": "frozen",
            "loss": "mean_keypoints(0.5*position_m_squared+0.25*rotation_F_squared+0.5*gripper_native_squared)",
            "codec": "ideal_tracking_world_delta_OSC; measured_state_feedback_and_contact_not_simulated",
            "gripper": "native_GT_command_surrogate_not_physical_width", "guidance_padding": "zero",
            "guidance_schedule": "every_sampling_step_fixed_alpha_no_window_no_cap_no_tolerance",
        }

    # Original observation plus geometry/experiment -> original native actions [10,7] and numerical diagnostics.
    def infer(self, obs: dict) -> dict:
        observation = dict(obs)
        experiment = observation.pop("experiment")
        geometry = prepare_geometry(observation.pop("geometry"))
        alpha = float(experiment["alpha"])
        if not np.isfinite(alpha) or alpha < 0:
            raise ValueError("Alpha must be finite and nonnegative")
        epsilon = np.random.default_rng(int(experiment["seed"])).standard_normal((10, 32)).astype(np.float32)
        inputs = self.policy._input_transform(dict(observation))
        batched = jax.tree.map(lambda x: jnp.asarray(x)[None], inputs)
        model_observation = model_api.Observation.from_dict(batched)
        before = time.monotonic()
        normalized, logs, by_tick = self.sample(
            model_observation, jnp.asarray(epsilon[None]), geometry, jnp.asarray(alpha, dtype=jnp.float32),
            guided=alpha != 0,
        )
        normalized, logs, by_tick = jax.device_get((normalized, logs, by_tick))
        seconds = time.monotonic() - before
        if not all(np.isfinite(value).all() for value in (normalized, logs, by_tick)):
            raise FloatingPointError("Nonfinite sampling state or gradient; no actions returned")
        result = self.policy._output_transform({"state": inputs["state"], "actions": normalized[0]})
        expected = np.asarray(self.sampler.unnormalize(jnp.asarray(normalized)))[0]
        decoder_error = float(np.max(np.abs(expected - result["actions"])))
        if decoder_error > 1e-5:
            raise ValueError(f"Differentiable denormalization differs from official output: {decoder_error}")
        diagnostics = jax.device_get(self.final_errors(jnp.asarray(result["actions"][None]), geometry))
        if not all(np.isfinite(value).all() for value in diagnostics.values()):
            raise FloatingPointError("Nonfinite final pose diagnostics; no actions returned")
        parity = None
        if experiment["verify_original"]:
            if alpha != 0:
                raise ValueError("Original parity check requires alpha=0")
            original = self.policy.infer(observation, noise=epsilon)
            parity = float(np.max(np.abs(original["actions"] - result["actions"])))
            if parity > 1e-5:
                raise ValueError(f"Alpha-zero sampling parity failed: {parity}")
        probe = None
        if experiment["probe_gradient"]:
            full, direct = jax.device_get(self.probe(model_observation, jnp.asarray(epsilon[None]), geometry))
            if not np.isfinite(full).all() or not np.isfinite(direct).all():
                raise FloatingPointError("Gradient probe is nonfinite")
            probe = {"full_gradient": full, "detached_expert_gradient": direct,
                     "expert_jacobian_contribution_norm": float(np.linalg.norm(full - direct))}
        result["experiment"] = {
            "alpha": alpha, "seed": int(experiment["seed"]), "initial_noise": epsilon,
            "sample_seconds": seconds, "original_parity_max_error": parity,
            "decoder_max_error": decoder_error, "guidance_steps": 10 if alpha else 0,
            "log_columns": ["position_loss", "rotation_loss", "gripper_loss", "gradient_rms",
                            "native_velocity_rms", "correction_rms", "native_tau"],
            "logs": logs, "gradient_norm_by_action_tick": by_tick,
            "final_command_errors": diagnostics, "gradient_probe": probe,
            "execution_success_probability": None,
        }
        return result


# Checkpoint and port -> experimental websocket policy server.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18402)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy = GuidedPolicy(args.checkpoint)
    WebsocketPolicyServer(policy, host="127.0.0.1", port=args.port, metadata=policy.metadata).serve_forever()


if __name__ == "__main__":
    main()

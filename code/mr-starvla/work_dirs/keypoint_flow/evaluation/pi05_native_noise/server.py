"""Unchanged official pi05 weights and sampler with paired, externally supplied initial noise."""

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
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.sampler import NaiveSampler


# Noise [H,D] -> per-channel time-standardized first seven dimensions and detached moment diagnostics.
def standardize_noise(noise: np.ndarray) -> tuple[np.ndarray, dict]:
    valid = noise[:, :7].astype(np.float64)
    mean = valid.mean(axis=0, keepdims=True)
    std = valid.std(axis=0, keepdims=True, ddof=0)
    if not np.isfinite(valid).all() or np.any(std == 0):
        raise ValueError("Noise standardization requires finite values and positive channel variance")
    result = noise.copy()
    result[:, :7] = ((valid - mean) / std).astype(np.float32)
    diagnostics = {"axis": "time_per_action_channel", "ddof": 0, "target_mean": 0.0, "target_variance": 1.0,
                   "before_mean": mean[0].tolist(), "before_std": std[0].tolist(),
                   "after_mean": result[:, :7].mean(axis=0, dtype=np.float64).tolist(),
                   "after_variance": result[:, :7].var(axis=0, dtype=np.float64).tolist(),
                   "padding": "unchanged_standard_gaussian", "tau_start": 1.0, "denoise_steps": 10}
    return result, diagnostics


class WarmStartSampler(NaiveSampler):
    # Observation and noisy sketch [B,H,D] -> actions [B,H,D] and visited native times [tau_start*N].
    def sample_from_tau(self, observation, noise, *, tau_start: float, num_steps: int = 10):
        remaining = round(tau_start * num_steps)
        if not 0 < tau_start <= 1 or abs(remaining / num_steps - tau_start) > 1e-7:
            raise ValueError("Warm start must lie on the original Euler grid")
        context = self.prepare(observation)
        dt = -1.0 / num_steps
        times = jnp.zeros((remaining,), dtype=jnp.float32)

        # Current actions/time/index -> one original native Euler update and its recorded time.
        def step(carry):
            x, tau, index, records = carry
            records = records.at[index].set(tau)
            return x + dt * self.velocity(x, tau, context), tau + dt, index + 1, records

        # Current actions/time/index -> original sampler endpoint predicate.
        def condition(carry):
            return carry[1] >= -dt / 2

        actions, _, _, times = jax.lax.while_loop(condition, step, (noise, tau_start, 0, times))
        return actions, times


class NoisePolicy(BasePolicy):
    # Official checkpoint -> unchanged policy and explicit native action/noise dimensions.
    def __init__(self, checkpoint: Path):
        config = get_config("pi05_libero")
        self.policy = policy_config.create_trained_policy(config, checkpoint)
        self.horizon, self.dimension = config.model.action_horizon, config.model.action_dim
        data = config.data.create(config.assets_dirs, config.model)
        if data.asset_id is None:
            raise ValueError("Expected LIBERO normalization asset")
        stats = checkpoints.load_norm_stats(checkpoint / "assets", data.asset_id)["actions"]
        self.warm_sampler = WarmStartSampler(self.policy._model, stats.q01, stats.q99)
        self.sample_warm = nnx_utils.module_jit(
            self.warm_sampler.sample_from_tau, static_argnames=("tau_start", "num_steps")
        )
        self.metadata = {
            "checkpoint": str(checkpoint.resolve()), "config": "pi05_libero",
            "action_horizon": self.horizon, "action_dim": self.dimension, "physical_action_dim": 7,
            "weights": "unchanged", "sampler": "unchanged_official_sample_actions",
            "modified_noise_channels": "first_7_only_padding_retains_standard_gaussian",
            "warmstart": {"mode": "pose_warmstart", "tau_start": 0.3, "dt": -0.1,
                          "denoise_steps": 3, "initialization": "0.7*normalized_pose_sketch+0.3*epsilon",
                          "padding": "zero_clean_action_plus_0.3_epsilon", "weights": "unchanged"},
            "standardization": {"mode": "pose_standardized", "input": "normalized_pose_sketch+0.3*epsilon",
                                "axis": "time_per_action_channel", "ddof": 0,
                                "target_mean": 0.0, "target_variance": 1.0,
                                "padding": "unchanged_standard_gaussian", "tau_start": 1.0,
                                "denoise_steps": 10, "sampler": "unchanged_official_sample_actions"},
        }

    # RGB/state observation plus experiment and optional native sketch [H,7] -> native actions [H,7].
    def infer(self, obs: dict) -> dict:
        observation = dict(obs)
        experiment = observation.pop("experiment")
        sketch = np.asarray(observation.pop("sketch"), dtype=np.float32)
        mode, seed, sigma = experiment["mode"], experiment["seed"], float(experiment["sigma"])
        if mode not in ("original", "native_keypoints", "pose_keypoints", "pose_warmstart", "pose_standardized"):
            raise ValueError(f"Unknown noise mode: {mode}")
        if sketch.shape != (self.horizon, 7) or not np.isfinite(sketch).all():
            raise ValueError("Sketch must be a finite native-action tensor [H,7]")
        epsilon = np.random.default_rng(seed).standard_normal((self.horizon, self.dimension)).astype(np.float32)
        noise = epsilon.copy()
        normalized = np.zeros_like(noise)
        roundtrip_error = 0.0
        if mode != "original":
            inputs = self.policy._input_transform({**observation, "actions": sketch.copy()})
            normalized = np.asarray(inputs["actions"], dtype=np.float32)
            recovered = self.policy._output_transform({"state": inputs["state"], "actions": normalized.copy()})
            roundtrip_error = float(np.max(np.abs(recovered["actions"] - sketch)))
            if roundtrip_error > 1e-5:
                raise ValueError(f"Native action normalization roundtrip failed: {roundtrip_error}")
            noise[:, :7] = normalized[:, :7] + sigma * epsilon[:, :7]
        warm_details = {}
        if mode == "pose_standardized":
            noise, statistics = standardize_noise(noise)
            warm_details["standardization"] = statistics
        if mode == "pose_warmstart":
            if sigma != 0.3:
                raise ValueError("This experiment fixes tau_start and noise scale to 0.3")
            if np.any(normalized[:, 7:] != 0):
                raise ValueError("Expected zero clean-action padding")
            noise = (1 - sigma) * normalized + sigma * epsilon
            batched = jax.tree.map(lambda x: jnp.asarray(x)[None], inputs)
            model_observation = model_api.Observation.from_dict(batched)
            before = time.monotonic()
            sampled, times = jax.device_get(self.sample_warm(
                model_observation, jnp.asarray(noise[None]), tau_start=sigma
            ))
            if not np.isfinite(sampled).all():
                raise FloatingPointError("Nonfinite warm-start actions")
            result = self.policy._output_transform({"state": inputs["state"], "actions": sampled[0]})
            warm_details = {"tau_start": sigma, "dt": -0.1, "denoise_steps": len(times),
                            "native_times": times.tolist(), "sample_seconds": time.monotonic() - before,
                            "initialization": "(1-tau)*normalized_sketch+tau*epsilon_all_32_dimensions"}
            if experiment["verify_original"]:
                full, full_times = jax.device_get(self.sample_warm(
                    model_observation, jnp.asarray(epsilon[None]), tau_start=1.0
                ))
                decoded = self.policy._output_transform({"state": inputs["state"], "actions": full[0]})
                original = self.policy.infer(observation, noise=epsilon)
                error = float(np.max(np.abs(decoded["actions"] - original["actions"])))
                if error > 1e-5 or len(full_times) != 10:
                    raise ValueError(f"Warm-start tau=1 parity failed: {error}")
                warm_details["tau1_original_parity_max_error"] = error
        else:
            result = self.policy.infer(observation, noise=noise)
        result["experiment"] = {
            "mode": mode, "seed": seed, "sigma": sigma,
            "initial_noise": noise, "normalized_sketch": normalized,
            "normalization_roundtrip_max_error": roundtrip_error,
            **warm_details,
        }
        return result


# Checkpoint path and port -> websocket server exposing the unchanged official sampling implementation.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18401)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy = NoisePolicy(args.checkpoint)
    WebsocketPolicyServer(policy, host="127.0.0.1", port=args.port, metadata=policy.metadata).serve_forever()


if __name__ == "__main__":
    main()

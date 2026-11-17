import argparse
import hashlib
import json
import logging
import pickle
import time
from pathlib import Path

import flax.nnx as nnx
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
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.geometry import (
    FIRST_CONTROL_MODE,
    POSE_TOLERANCES,
    REDUCTIONS,
    evaluate_final,
)
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.sampler import (
    DynamicSampler,
)

ROOT = Path(__file__).resolve().parent
LOG_COLUMNS = ["matched_index_0based", "selected_loss", "position_loss", "rotation_loss", "gripper_loss",
               "gradient_rms", "native_velocity_rms", "correction_rms", "native_tau", "guidance_loss"]
SUBSPACE_BASE = "soft_time_control_track_pose025_fm_time_gripper_expectation"
SUBSPACE_MODES = (SUBSPACE_BASE + "_arm_actions", SUBSPACE_BASE + "_gripper_action")
CAPPED_MODE = SUBSPACE_BASE + "_cap1"
NATIVE_FINISH_CAPS = {REDUCTIONS[27] + "_cap03": 0.3, REDUCTIONS[27] + "_cap1": 1.0}
GRIPPER_FINISH_MODE = REDUCTIONS[27] + "_cap03_gripper_finish"



def prepare_geometry(request):
    supported = {"soft_time", "soft_time_control_track_pose025_fm_time_gripper_expectation",
                 "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half",
                 "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03"}
    if request.get("reduction", "soft_time") not in supported:
        raise ValueError("Unsupported guidance mode")
    if request["coordinate_frame"] != "world" or request["codec"] != "libero_panda_delta_osc_v1":
        raise ValueError("Invalid controller frame or codec")
    shapes = {"current_position": (3,), "current_rotation": (3, 3), "current_gripper_command": (),
              "target_position": (3,), "target_rotation": (3, 3), "target_width": (),
              "input_min": (6,), "input_max": (6,), "output_min": (6,), "output_max": (6,),
              "gripper_increment": (), "gripper_width_scale": ()}
    result = {}
    for name, shape in shapes.items():
        value = np.asarray(request[name], dtype=np.float32)
        if value.shape != shape or not np.isfinite(value).all():
            raise ValueError(f"Invalid geometry field: {name}")
        if "rotation" in name and (not np.allclose(value@value.T, np.eye(3), atol=1e-5) or
                                   not np.isclose(np.linalg.det(value), 1, atol=1e-5)):
            raise ValueError("Invalid rotation")
        result[name] = jnp.asarray(value[None] if name.startswith(("current_", "target_")) else value)
    if (not -1 <= request["current_gripper_command"] <= 1 or request["gripper_width_scale"] <= 0
            or not 0 <= request["target_width"] <= 2*request["gripper_width_scale"]):
        raise ValueError("Invalid gripper calibration or target")
    if np.any(np.asarray(request["input_max"]) <= request["input_min"]):
        raise ValueError("Invalid controller input bounds")
    if not np.isclose(request["gripper_increment"], .25):
        raise ValueError("The controller must use quarter-step gripper commands")
    result["reduction"] = jnp.asarray(2, dtype=jnp.int32)
    result["guidance_mask"] = jnp.ones(7)
    return result


class GuidedPolicy(BasePolicy):

    def __init__(self, checkpoint, orthogonal_guidance=False, temporal_arm_bias=False):
        config = get_config("pi05_libero")
        if not config.model.pi05:
            raise ValueError("Expected pi05=True")
        data = config.data.create(config.assets_dirs, config.model)
        if not data.use_quantile_norm or data.asset_id is None:
            raise ValueError("Expected original quantile normalization")
        stats = checkpoints.load_norm_stats(checkpoint / "assets", data.asset_id)["actions"]
        self.policy = policy_config.create_trained_policy(config, checkpoint)
        self.sampler = DynamicSampler(self.policy._model, stats.q01, stats.q99, orthogonal_guidance,
                                      temporal_arm_bias)
        self.sample = self.sampler.sample_dynamic
        self.final_errors = jax.jit(evaluate_final)
        self.metadata = {
            "checkpoint": str(checkpoint.resolve()), "config": "pi05_libero", "action_horizon": 10,
            "action_dim": 32, "num_steps": 10, "weights": "frozen", "index_base": 0,
            "keypoint_guidance": {"enabled": False, "guidance_scale": 1.0},
            "matching": "shared_complete_pose_soft_time", "active_canonical_times": [.5, .6, .7],
            "compatibility_canonical_times": [.6, .7], "relative_rms_cap": .3,
            "compatibility_residual_scale": 1., "compatibility_demand_scale": 3.,
            "compatibility_threshold": float(np.exp(-1)), "gripper_states": 9,
            "position_scale_m": .05, "rotation_scale": float(np.sqrt(8)*np.sin(np.pi/12)),
            "codec": "libero_panda_delta_osc_v1", "coordinate_frame": "world"
        }


    def model_inputs(self, observation):
        inputs = self.policy._input_transform(dict(observation))
        batched = jax.tree.map(lambda value: jnp.asarray(value)[None], inputs)
        return inputs, model_api.Observation.from_dict(batched)


    def infer(self, obs):
        observation = dict(obs)
        config = observation.pop("keypoint_guidance", {"enabled": False, "guidance_scale": 0.0})
        request = observation.pop("geometry", None)
        experiment = observation.pop("experiment")
        scale = float(config["guidance_scale"])
        if not np.isfinite(scale) or scale < 0:
            raise ValueError("guidance_scale must be finite and nonnegative")
        guided = bool(config["enabled"]) and scale > 0
        epsilon = np.random.default_rng(int(experiment["seed"])).standard_normal((10, 32)).astype(np.float32)
        geometry = prepare_geometry(request) if request is not None else None
        before = time.monotonic()
        if guided:
            if geometry is None:
                raise ValueError("Enabled guidance requires one complete goal")
            inputs, model_observation = self.model_inputs(observation)
            normalized, logs, gradients, matching_weights, proposal = jax.device_get(self.sample(
                model_observation, jnp.asarray(epsilon[None]), geometry, jnp.asarray(scale, dtype=jnp.float32)
            ))
            if proposal["status"] != "accepted":
                return {"status": proposal["status"], "reason": proposal["reason"], "executable": False,
                        "proposal_actions": normalized[0], "experiment": proposal}
            if not all(np.isfinite(value).all() for value in (normalized, logs, gradients, matching_weights)):
                raise FloatingPointError("Nonfinite guided sampling")
            result = self.policy._output_transform({"state": inputs["state"], "actions": np.clip(normalized[0], -1, 1)})
            decoder_error = float(np.max(np.abs(np.asarray(self.sampler.unnormalize(normalized))[0]
                                                - result["actions"])))
            if decoder_error > 1e-5:
                raise ValueError(f"Output codec mismatch: {decoder_error}")
        else:
            result = self.policy.infer(observation, noise=epsilon)
            logs, gradients, decoder_error = np.zeros((0, 1, 10)), np.zeros((0, 1, 10)), 0.0
            matching_weights = np.zeros((0, 1, 10))
        seconds = time.monotonic() - before
        final = None if geometry is None else jax.device_get(self.final_errors(
            jnp.asarray(result["actions"][None]), geometry
        ))
        if final is not None and not all(np.isfinite(value).all() for value in final.values()):
            raise FloatingPointError("Nonfinite final command geometry")
        backward_calls = proposal["backward_evaluations"] if guided else 0
        result["status"] = "accepted"
        result["experiment"] = {
            "keypoint_guidance": config, "seed": int(experiment["seed"]), "initial_noise": epsilon,
            "sample_seconds": seconds, "log_columns": LOG_COLUMNS, "logs": logs,
            "gradient_norm_by_action_tick": gradients, "final_command_errors": final,
            "matching_weights": matching_weights,
            "decoder_max_error": decoder_error, "expert_forward_calls": proposal["forward_evaluations"] if guided else 10,
            "temporal_arm_bias": self.sampler.temporal_arm_bias,
            "gripper_finish": geometry is not None and "gripper_finish" in geometry,
            "expert_input_backward_calls": backward_calls, "full_sampling_calls": 1,
        }
        return result



def parameter_digest(model):
    digest = hashlib.sha256()
    for value in jax.tree.leaves(nnx.state(model, nnx.Param)):
        digest.update(np.asarray(value).tobytes())
    return digest.hexdigest()



def validate_model(policy, fixture):
    with fixture.open("rb") as stream:
        payload = pickle.load(stream)
    before = parameter_digest(policy.policy._model)
    request = dict(payload)
    request["keypoint_guidance"] = {"enabled": True, "guidance_scale": 1.0}
    request["geometry"] = {**request["geometry"], "reduction": "soft_time"}
    result = policy.infer(request)
    after = parameter_digest(policy.policy._model)
    if before != after:
        raise AssertionError("Model parameters changed during generation")
    if result["status"] == "accepted" and not np.isfinite(result["actions"]).all():
        raise AssertionError("Nonfinite executable actions")
    if result["status"] != "accepted" and "actions" in result:
        raise AssertionError("Rejected proposals must not expose executable actions")
    print(json.dumps({"status": "passed", "model_unchanged": True,
                      "proposal_status": result["status"]}), flush=True)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18403)
    parser.add_argument("--validate-fixture", type=Path)
    parser.add_argument("--orthogonal-guidance", action="store_true")
    parser.add_argument("--temporal-arm-bias", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    policy = GuidedPolicy(args.checkpoint, args.orthogonal_guidance, args.temporal_arm_bias)
    if args.validate_fixture is not None:
        validate_model(policy, args.validate_fixture)
    WebsocketPolicyServer(policy, host="127.0.0.1", port=args.port, metadata=policy.metadata).serve_forever()


if __name__ == "__main__":
    main()

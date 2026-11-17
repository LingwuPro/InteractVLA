import jax.numpy as jnp
import numpy as np

from interactvla.jax_backend import sample
from interactvla.sampling import GuidanceConfig
from work_dirs.keypoint_flow.evaluation.pi05_dynamic_guidance.geometry import pose_loss
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.sampler import NaiveSampler


class DynamicSampler(NaiveSampler):
    def __init__(self, model, q01, q99, orthogonal_guidance=False, temporal_arm_bias=False):
        if orthogonal_guidance or temporal_arm_bias:
            raise ValueError("Projected input gradients are not supported")
        super().__init__(model, q01, q99)
        self.orthogonal_guidance = False
        self.temporal_arm_bias = False

    def unnormalize(self, actions):
        clipped = jnp.clip(actions[..., :7], -1, 1)
        return (clipped+1)/2*(self.q99.value-self.q01.value)+self.q01.value

    def sample_dynamic(self, observation, noise, geometry, guidance_scale, *, num_steps=10):
        context = self.prepare(observation)
        objective = lambda x, t: pose_loss(self.unnormalize(x), geometry, 1-t)[1]["complete_pose_errors"]
        mask = jnp.zeros(noise.shape[-1], dtype=bool).at[:7].set(geometry.get("guidance_mask", jnp.ones(7)).astype(bool))
        config = GuidanceConfig(steps=num_steps, gain=float(guidance_scale))
        result = sample(lambda x, t: -self.velocity(x, 1-t, context), noise, objective, mask, config)
        actions, status, reason, trace, forwards, backwards = result
        logs = np.zeros((len(trace), noise.shape[0], 10), dtype=np.float32)
        weights = np.zeros((len(trace), noise.shape[0], noise.shape[1]), dtype=np.float32)
        gradients = np.zeros_like(weights)
        for i, row in enumerate(trace):
            logs[i, :, 8] = 1-row["t"]
            for column, key in ((0, "matched_index"), (1, "minimum_pose_error"), (5, "gradient_rms"),
                                (6, "velocity_rms"), (7, "correction_rms"), (9, "loss")):
                if key in row:
                    logs[i, :, column] = row[key]
            if "matching_weights" in row:
                weights[i] = row["matching_weights"]
            if "gradient_norm_by_action" in row:
                gradients[i] = row["gradient_norm_by_action"]
        return actions, logs, gradients, weights, {"status": status, "reason": reason, "trace": trace,
                                                   "forward_evaluations": forwards, "backward_evaluations": backwards}

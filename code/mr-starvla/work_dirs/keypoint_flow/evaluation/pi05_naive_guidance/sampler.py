"""Original pi05 time grid with an optional, uncapped input-gradient correction."""

import einops
import flax.nnx as nnx
import jax
import jax.numpy as jnp
from openpi.models import model as model_api
from openpi.models.pi0 import make_attn_mask
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.geometry import pose_loss


class NaiveSampler(nnx.Module):
    # Official model and checkpoint action quantiles [7] -> immutable sampling wrapper.
    def __init__(self, model, q01, q99):
        self.model = model
        self.q01 = nnx.Variable(jnp.asarray(q01, dtype=jnp.float32))
        self.q99 = nnx.Variable(jnp.asarray(q99, dtype=jnp.float32))

    # Normalized model actions [B,H,D] -> native LIBERO actions [B,H,7].
    def unnormalize(self, actions):
        return (actions[..., :7] + 1) / 2 * (self.q99.value - self.q01.value + 1e-6) + self.q01.value

    # Original observation -> processed observation and one cached visual/language prefix; no geometry input.
    def prepare(self, observation):
        observation = model_api.preprocess_observation(None, observation, train=False)
        tokens, mask, ar_mask = self.model.embed_prefix(observation)
        positions = jnp.cumsum(mask, axis=1) - 1
        _, cache = self.model.PaliGemma.llm(
            [tokens, None], mask=make_attn_mask(mask, ar_mask), positions=positions
        )
        return observation, mask, cache

    # Sampling state [B,H,D], native time scalar and original context -> native velocity [B,H,D].
    def velocity(self, x, tau, context):
        observation, prefix_mask, cache = context
        tokens, mask, ar_mask, adarms = self.model.embed_suffix(
            observation, x, jnp.broadcast_to(tau, x.shape[0])
        )
        prefix_attention = einops.repeat(prefix_mask, "b p -> b s p", s=tokens.shape[1])
        full_mask = jnp.concatenate((prefix_attention, make_attn_mask(mask, ar_mask)), axis=-1)
        positions = jnp.sum(prefix_mask, axis=-1)[:, None] + jnp.cumsum(mask, axis=-1) - 1
        (_, output), _ = self.model.PaliGemma.llm(
            [None, tokens], mask=full_mask, positions=positions, kv_cache=cache, adarms_cond=[None, adarms]
        )
        return self.model.action_out_proj(output[:, -self.model.action_horizon :])

    # Current x [B,H,D] -> scalar batch-summed loss and reused native velocity/diagnostics.
    def objective(self, x, tau, context, geometry):
        velocity = self.velocity(x, tau, context)
        clean = x - tau * velocity
        losses, diagnostics = pose_loss(self.unnormalize(clean), geometry)
        return losses.sum(), (velocity, diagnostics["components"])

    # Original observation/noise [B,H,D] and external geometry -> actions [B,H,D], detached step logs.
    def sample(self, observation, noise, geometry, alpha, *, guided: bool, num_steps: int = 10):
        context = self.prepare(observation)
        dt = -1.0 / num_steps
        logs = jnp.zeros((num_steps, noise.shape[0], 7), dtype=jnp.float32)
        gradient_by_tick = jnp.zeros((num_steps, noise.shape[0], noise.shape[1]), dtype=jnp.float32)

        # Current x/time/index/log arrays -> one original Euler step with optional full input VJP.
        def step(carry):
            x, tau, n, records, gradients = carry
            if guided:
                (_, (velocity, components)), grad = jax.value_and_grad(self.objective, has_aux=True)(
                    x, tau, context, geometry
                )
                grad = grad.at[..., 7:].set(0)
                grad_rms = jnp.sqrt(jnp.mean(grad[..., :7] ** 2, axis=(1, 2)))
                velocity_rms = jnp.sqrt(jnp.mean(velocity[..., :7] ** 2, axis=(1, 2)))
                row = jnp.concatenate((components, grad_rms[:, None], velocity_rms[:, None],
                                       (alpha * grad_rms)[:, None], jnp.full((x.shape[0], 1), tau)), axis=-1)
                records = records.at[n].set(row)
                gradients = gradients.at[n].set(jnp.linalg.norm(grad[..., :7], axis=-1))
                x = x + dt * (velocity + alpha * grad)
            else:
                x = x + dt * self.velocity(x, tau, context)
            return jax.lax.stop_gradient(x), tau + dt, n + 1, records, gradients

        # Original native-time loop condition, including its floating-point endpoint convention.
        def condition(carry):
            return carry[1] >= -dt / 2

        actions, _, _, logs, gradient_by_tick = jax.lax.while_loop(
            condition, step, (noise, 1.0, 0, logs, gradient_by_tick)
        )
        return actions, logs, gradient_by_tick

    # One observation and noise [B,H,D] -> full versus detached-expert gradients at native tau=0.5.
    def probe(self, observation, noise, geometry):
        context = self.prepare(observation)
        tau = jnp.asarray(0.5, dtype=jnp.float32)
        (_, (velocity, _)), full = jax.value_and_grad(self.objective, has_aux=True)(noise, tau, context, geometry)

        # Current x [B,H,D] -> loss with the expert Jacobian intentionally removed for this diagnostic only.
        def direct_objective(x):
            losses, _ = pose_loss(self.unnormalize(x - tau * jax.lax.stop_gradient(velocity)), geometry)
            return losses.sum()

        direct = jax.grad(direct_objective)(noise)
        return full, direct

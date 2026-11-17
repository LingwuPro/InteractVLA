"""Differentiable ideal-tracking OSC codec; gripper loss uses native commands, not jaw width."""

import jax
import jax.numpy as jnp


# Axis-angle [...,3] -> valid rotation matrices [...,3,3], including differentiable zero rotations.
def rotation_matrix(vector):
    squared = jnp.sum(vector * vector, axis=-1)
    angle = jnp.sqrt(jnp.maximum(squared, 1e-8))
    small = squared < 1e-8
    a = jnp.where(small, 1 - squared / 6 + squared**2 / 120, jnp.sin(angle) / angle)
    b = jnp.where(small, 0.5 - squared / 24 + squared**2 / 720, 2 * jnp.sin(angle / 2) ** 2 / angle**2)
    x, y, z = jnp.moveaxis(vector, -1, 0)
    zero = jnp.zeros_like(x)
    skew = jnp.stack((zero, -z, y, z, zero, -x, -y, x, zero), axis=-1)
    skew = skew.reshape((*vector.shape[:-1], 3, 3))
    return jnp.eye(3) + a[..., None, None] * skew + b[..., None, None] * (skew @ skew)


# Native actions [B,H,7] and current world pose -> ideal command positions/rotations [B,H,3], [B,H,3,3].
def decode(actions, geometry):
    clipped = jnp.clip(actions[..., :6], geometry["input_min"], geometry["input_max"])
    scale = (geometry["output_max"] - geometry["output_min"]) / (
        geometry["input_max"] - geometry["input_min"]
    )
    delta = (clipped - (geometry["input_max"] + geometry["input_min"]) / 2) * scale
    delta = delta + (geometry["output_max"] + geometry["output_min"]) / 2
    positions = geometry["current_position"][:, None] + jnp.cumsum(delta[..., :3], axis=1)
    increments = rotation_matrix(delta[..., 3:6])

    # Current orientations [B,3,3] and one delta [B,3,3] -> next orientations twice for scan.
    def compose(previous, increment):
        result = increment @ previous
        return result, result

    _, rotations = jax.lax.scan(compose, geometry["current_rotation"], jnp.swapaxes(increments, 0, 1))
    return positions, jnp.swapaxes(rotations, 0, 1)


# Native actions [B,H,7], geometry with fixed one-based steps [K] -> per-sample loss [B], diagnostics.
def pose_loss(actions, geometry):
    positions, rotations = decode(actions, geometry)
    indices = geometry["steps"] - 1
    position_delta = positions[:, indices] - geometry["target_position"]
    rotation_delta = rotations[:, indices] - geometry["target_rotation"]
    gripper_delta = actions[:, indices, 6] - geometry["target_gripper"]
    position_squared = jnp.sum(position_delta**2, axis=-1)
    rotation_squared = jnp.sum(rotation_delta**2, axis=(-1, -2)) / 2
    gripper_squared = gripper_delta**2
    components = jnp.stack((position_squared, rotation_squared, gripper_squared), axis=-1) / 2
    losses = jnp.mean(jnp.sum(components, axis=-1), axis=-1)
    diagnostics = {"components": jnp.mean(components, axis=1),
                   "position_error_m": jnp.sqrt(position_squared),
                   "rotation_chordal_squared": rotation_squared,
                   "gripper_command_error": jnp.abs(gripper_delta)}
    return losses, diagnostics


# Native candidate [B,H,7] after original postprocessing -> geometric command errors, not execution predictions.
def evaluate_final(actions, geometry):
    losses, diagnostics = pose_loss(actions, geometry)
    cosine = 1 - diagnostics["rotation_chordal_squared"] / 2
    diagnostics["orientation_error_rad"] = jnp.arccos(jnp.clip(cosine, -1, 1))
    diagnostics["loss"] = losses
    return diagnostics

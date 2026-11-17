"""CPU checks for OSC geometry, input gradients, cross-time response and native-time update sign."""

import json
from pathlib import Path

import jax
import jax.numpy as jnp
import numpy as np
from scipy.spatial.transform import Rotation
from work_dirs.keypoint_flow.evaluation.pi05_naive_guidance.geometry import (
    decode,
    pose_loss,
    rotation_matrix,
)


# Deterministic analytic/finite-difference cases -> validation report, raising on any failed check.
def main():
    jax.config.update("jax_enable_x64", True)
    rng = np.random.default_rng(7)
    actions = rng.uniform(-0.25, 0.25, (1, 10, 7))
    start = Rotation.from_rotvec([0.2, -0.4, 0.1]).as_matrix()
    geometry = {"current_position": jnp.array([[0.1, -0.2, 0.3]]),
                "current_rotation": jnp.asarray(start[None]), "steps": jnp.array([2, 5, 8, 10]),
                "input_min": -jnp.ones(6), "input_max": jnp.ones(6),
                "output_min": -jnp.array([0.05] * 3 + [0.5] * 3),
                "output_max": jnp.array([0.05] * 3 + [0.5] * 3),
                "target_position": jnp.asarray(rng.uniform(-0.2, 0.2, (1, 4, 3))),
                "target_rotation": jnp.asarray(Rotation.from_rotvec(rng.normal(0, 0.2, (4, 3))).as_matrix()[None]),
                "target_gripper": jnp.array([[1.0, -1.0, 1.0, -1.0]])}
    positions, rotations = decode(jnp.asarray(actions), geometry)
    expected_positions = np.array([0.1, -0.2, 0.3]) + np.cumsum(actions[0, :, :3] * 0.05, axis=0)
    expected_rotations = []
    previous = start
    for action in actions[0]:
        previous = Rotation.from_rotvec(action[3:6] * 0.5).as_matrix() @ previous
        expected_rotations.append(previous)
    np.testing.assert_allclose(positions[0], expected_positions, atol=1e-12)
    np.testing.assert_allclose(rotations[0], expected_rotations, atol=1e-12)
    zero_jacobian = jax.jacfwd(rotation_matrix)(jnp.zeros(3))
    assert np.isfinite(zero_jacobian).all()

    # Actions [1,10,7] -> scalar loss for a central directional derivative check.
    def objective(value):
        return pose_loss(value, geometry)[0].sum()

    gradient = jax.grad(objective)(jnp.asarray(actions))
    direction = rng.normal(size=actions.shape)
    direction /= np.linalg.norm(direction)
    epsilon = 1e-5
    difference = float((objective(actions + epsilon * direction) - objective(actions - epsilon * direction)) /
                       (2 * epsilon))
    automatic = float(jnp.sum(gradient * direction))
    np.testing.assert_allclose(automatic, difference, rtol=1e-6, atol=1e-9)

    matrix = jnp.array([[0.2, 0.3, -0.1], [0.4, -0.2, 0.1], [0.1, 0.2, 0.3]])
    x, tau = jnp.array([0.3, -0.7, 0.2]), 0.6
    target, bias = 0.4, jnp.array([0.1, 0.2, -0.1])

    # Toy temporal action vector [3] -> keypoint-only loss with a known nonlocal expert Jacobian.
    def toy_loss(value):
        native_velocity = -(matrix @ value + bias)
        clean = value - tau * native_velocity
        return 0.5 * (clean[1] - target) ** 2

    full_gradient = jax.grad(toy_loss)(x)
    clean = x + tau * (matrix @ x + bias)
    analytic = (jnp.eye(3) + tau * matrix)[1] * (clean[1] - target)
    np.testing.assert_allclose(full_gradient, analytic, atol=1e-12)
    assert abs(float(full_gradient[0])) > 0 and abs(float(full_gradient[2])) > 0
    dt = -0.01
    native_velocity = -(matrix @ x + bias)
    native_next = x + dt * (native_velocity + full_gradient)
    canonical_next = x + (-dt) * (-native_velocity - full_gradient)
    np.testing.assert_allclose(native_next, canonical_next, atol=1e-12)
    assert float(toy_loss(x + dt * full_gradient)) < float(toy_loss(x))
    report = {"status": "passed", "geometry_rotation_max_error": float(np.max(np.abs(
        np.asarray(rotations[0]) - expected_rotations))), "directional_autograd": automatic,
        "directional_finite_difference": difference, "zero_rotation_derivative_finite": True,
        "analytic_expert_jacobian": True, "native_canonical_update_sign": True,
        "nonlocal_gradient": np.asarray(full_gradient).tolist()}
    (Path(__file__).parent / "geometry-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

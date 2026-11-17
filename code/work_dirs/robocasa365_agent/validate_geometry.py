import json
from pathlib import Path

import numpy as np
import torch
from guidance import decode, gripper_errors, pose_loss, reduce_errors, rotation_matrix
from scipy.spatial.transform import Rotation


# Input: independent numerical fixtures; output: rotation, integration and gradient validation evidence.
def main() -> None:
    torch.manual_seed(7)
    vector = torch.randn(11, 3, dtype=torch.float64) * 0.1
    matrix = rotation_matrix(vector).numpy()
    np.testing.assert_allclose(matrix, Rotation.from_rotvec(vector.numpy()).as_matrix(), atol=1e-12)
    zero = torch.zeros((1, 3), dtype=torch.float64, requires_grad=True)
    rotation_matrix(zero).sum().backward()
    assert torch.isfinite(zero.grad).all()
    actions = (torch.randn(1, 50, 12, dtype=torch.float64) * 0.1).requires_grad_(True)
    geometry = {
        'action_min': -torch.ones(12, dtype=torch.float64),
        'action_max': torch.ones(12, dtype=torch.float64),
        'input_min': -torch.ones(6, dtype=torch.float64),
        'input_max': torch.ones(6, dtype=torch.float64),
        'output_min': torch.tensor([-0.05] * 3 + [-0.5] * 3, dtype=torch.float64),
        'output_max': torch.tensor([0.05] * 3 + [0.5] * 3, dtype=torch.float64),
        'current_position': torch.tensor([[0.4, 0.0, 0.9]], dtype=torch.float64),
        'current_rotation': torch.eye(3, dtype=torch.float64)[None],
        'current_gripper_command': torch.zeros(1, dtype=torch.float64),
        'gripper_width_scale': torch.tensor(0.04, dtype=torch.float64),
        'target_position': torch.tensor([[0.45, 0.02, 0.92]], dtype=torch.float64),
        'target_rotation': torch.eye(3, dtype=torch.float64)[None],
        'target_width': torch.zeros(1, dtype=torch.float64),
    }
    positions, rotations = decode(actions, geometry)
    native = actions.detach().numpy()[0]
    expected_position = np.array([0.4, 0.0, 0.9]) + (0.25 * (np.cumsum(native[:, :3], axis=0) - native[:, :3]) + native[:, :3]) * 0.05
    np.testing.assert_allclose(positions.detach().numpy()[0], expected_position, atol=1e-12)
    running = np.eye(3)
    for index in range(50):
        command = Rotation.from_rotvec(native[index, 3:6] * 0.5).as_matrix() @ running
        np.testing.assert_allclose(rotations.detach().numpy()[0, index], command, atol=1e-12)
        running = Rotation.from_rotvec(native[index, 3:6] * 0.125).as_matrix() @ running
    loss, _ = pose_loss(actions, geometry)
    grad = torch.autograd.grad(loss.sum(), actions)[0]
    discrepancies = []
    for tick, channel in [(0, 0), (7, 4), (9, 6), (24, 6), (39, 2), (49, 5)]:
        plus, minus = actions.detach().clone(), actions.detach().clone()
        plus[0, tick, channel] += 1e-5
        minus[0, tick, channel] -= 1e-5
        numerical = (pose_loss(plus, geometry)[0] - pose_loss(minus, geometry)[0]) / 2e-5
        discrepancies.append(abs(float(numerical) - float(grad[0, tick, channel])))
    assert max(discrepancies) < 1e-8, discrepancies
    errors = torch.tensor([[1., 4., 9.]], dtype=torch.float64, requires_grad=True)
    reduced, weights = reduce_errors(errors, 0.6)
    derivative = torch.autograd.grad(reduced.sum(), errors)[0]
    torch.testing.assert_close(derivative, 0.6 * 0.4 * weights)
    closed = torch.ones(1, 10, 12, dtype=torch.float64)
    widths = gripper_errors(closed, geometry, 1).sqrt()
    torch.testing.assert_close(widths[0, :5], torch.tensor([0.032, 0.024, 0.016, 0.008, 0.], dtype=torch.float64))
    assert widths[0, 5:].max() == 0
    record = {'rotation_scipy_parity': True, 'sequential_pose_parity': True,
              'zero_rotation_gradient_finite': True, 'gradient_max_error': max(discrepancies),
              'soft_time_fm_gradient_identity': True, 'native_gripper_step02': True,
              'scope': 'stationary-frame track_pose025 surrogate; not physical tracking or model inference'}
    Path(__file__).with_name('geometry-validation.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()

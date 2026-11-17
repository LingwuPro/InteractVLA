import math
from typing import Any

import torch

from interactvla.geometry import binary_width_moments
from interactvla.sampling import GuidanceConfig, ProposalRejected, sample as guided_sample



def rotation_matrix(vector: torch.Tensor) -> torch.Tensor:
    squared = vector.square().sum(-1)
    angle = squared.clamp_min(1e-8).sqrt()
    a = torch.where(squared < 1e-8, 1 - squared / 6 + squared.square() / 120, angle.sin() / angle)
    b = torch.where(squared < 1e-8, 0.5 - squared / 24 + squared.square() / 720,
                    2 * (angle / 2).sin().square() / angle.square())
    x, y, z = vector.unbind(-1)
    zero = torch.zeros_like(x)
    skew = torch.stack((zero, -z, y, z, zero, -x, -y, x, zero), -1).reshape(*vector.shape[:-1], 3, 3)
    return torch.eye(3, device=vector.device, dtype=vector.dtype) + a[..., None, None] * skew + b[..., None, None] * (skew @ skew)



def decode(actions: torch.Tensor, geometry: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
    physical = (actions[..., :6] + 1) * 0.5 * (geometry['action_max'][:6] - geometry['action_min'][:6]) + geometry['action_min'][:6]
    clipped = physical.clamp(geometry['input_min'], geometry['input_max'])
    delta = (clipped - (geometry['input_max'] + geometry['input_min']) / 2)
    delta = delta * (geometry['output_max'] - geometry['output_min']) / (geometry['input_max'] - geometry['input_min'])
    delta = delta + (geometry['output_max'] + geometry['output_min']) / 2
    positions = geometry['current_position'][:, None] + 0.25 * (delta[..., :3].cumsum(1) - delta[..., :3]) + delta[..., :3]
    increments = rotation_matrix(delta[..., 3:6])
    tracking = rotation_matrix(0.25 * delta[..., 3:6])
    rotation = geometry['current_rotation']
    rotations = []
    for index in range(actions.shape[1]):
        rotations.append(increments[:, index] @ rotation)
        rotation = tracking[:, index] @ rotation
    return positions, torch.stack(rotations, 1)



def gripper_errors(actions, geometry, t):
    lower = 0.0
    upper = 2 * geometry['gripper_width_scale']
    initial = geometry['gripper_width_scale'] * (geometry['current_gripper_command'] + 1)
    mean, second = binary_width_moments(2 * actions[..., 6] - 1, initial, lower, upper, t)
    target = geometry['target_width'][:, None]
    return (second - 2 * mean * target + target.square()).clamp_min(0)



def reduce_errors(errors: torch.Tensor, t: float) -> tuple[torch.Tensor, torch.Tensor]:
    if t == 1:
        indices = errors.detach().argmin(1)
        weights = torch.nn.functional.one_hot(indices, errors.shape[1]).to(errors.dtype)
        return (weights * errors).sum(1), weights
    logits = -(t / (1 - t)) ** 2 * errors
    weights = logits.softmax(1)
    soft = -(1 - t) ** 2 * (torch.logsumexp(logits, 1) - math.log(errors.shape[1]))
    return soft * ((1 - t) / t if t > 0 else 0), weights



def pose_loss(actions: torch.Tensor, geometry: dict[str, torch.Tensor], t: float = 0.5) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    positions, rotations = decode(actions, geometry)
    dp = positions - geometry['target_position'][:, None]
    dr2 = (rotations - geometry['target_rotation'][:, None]).square().sum((-1, -2))
    dw2 = gripper_errors(actions, geometry, t)
    errors = dp.square().sum(-1) / 0.05**2
    errors = errors + dr2 / (8 * math.sin(math.pi / 12)**2)
    errors = errors + dw2 / (2 * geometry['gripper_width_scale']).square()
    loss, weights = reduce_errors(errors, t)
    indices = errors.detach().argmin(1)
    batch = torch.arange(actions.shape[0], device=actions.device)
    return loss, {'complete_pose_errors': errors, 'matching_weights': weights, 'matched_index': indices,
                  'position_error_m': dp.square().sum(-1)[batch, indices].sqrt(),
                  'orientation_error_rad': (1 - dr2[batch, indices] / 4).clamp(-1, 1).acos(),
                  'gripper_error_m': dw2[batch, indices].sqrt()}



def velocity(head: Any, actions: torch.Tensor, t: float, obs: torch.Tensor, mask: Any, clock_dtype: torch.dtype) -> torch.Tensor:
    time = torch.full((actions.shape[0],), t, device=actions.device, dtype=clock_dtype)
    temb = head.time_embed(time * head.num_timestep_buckets)
    hidden = head.action_proj_in(actions)
    context = obs.clone()
    for block in head.transformer_blocks:
        hidden, context = block(action_hidden_states=hidden, obs_hidden_states=context, temb=temb, obs_attention_mask=mask)
    output = head.action_proj_out(hidden, temb).float()
    return (output - actions) / (1 - t) if head.prediction_type == 'sample' else output



def sample(head, vl, state, mask, noise, geometry, scale):
    head.eval().requires_grad_(False)
    with torch.inference_mode(False), torch.no_grad():
        obs = head.obs_proj(vl, state).detach()
        obs_mask = head._build_obs_attention_mask(mask, obs, state)
    active = torch.zeros(noise.shape[-1], dtype=torch.bool, device=noise.device)
    active[:7] = True
    if geometry is not None and 'guidance_mask' in geometry:
        active[:7] &= geometry['guidance_mask'].bool()
    objective = None if geometry is None else lambda x, t: pose_loss(x, geometry, t)[1]['complete_pose_errors']
    result = guided_sample(lambda x, t: velocity(head, x, t, obs, obs_mask, vl.dtype), noise,
                           objective, active, GuidanceConfig(gain=float(scale)))
    if not result.executable:
        raise ProposalRejected(result)
    return result.actions, result.traces

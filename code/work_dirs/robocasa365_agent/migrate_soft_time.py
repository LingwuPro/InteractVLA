from pathlib import Path

root = Path(__file__).resolve().parent
path = root / 'guidance.py'
s = path.read_text()
s = s.replace('from typing import Any', 'import math\nfrom typing import Any')
s = s.replace('ideal fixed-base command poses.', '25% tracking command poses in the current controller frame.')
s = s.replace("positions = geometry['current_position'][:, None] + delta[..., :3].cumsum(1)", "positions = geometry['current_position'][:, None] + 0.25 * (delta[..., :3].cumsum(1) - delta[..., :3]) + delta[..., :3]")
s = s.replace("increments = rotation_matrix(delta[..., 3:6])", "increments = rotation_matrix(delta[..., 3:6])\n    tracking = rotation_matrix(0.25 * delta[..., 3:6])")
s = s.replace('rotation = increments[:, index] @ rotation\n        rotations.append(rotation)', 'rotations.append(increments[:, index] @ rotation)\n        rotation = tracking[:, index] @ rotation')
start = s.index('# Input: normalized [B,H,12], four requested poses;')
end = s.index('# Input: native action head', start)
s = s[:start] + '''# Input: native normalized gripper logits [B,H], actuator state; output: expected squared width errors.
def gripper_errors(actions: torch.Tensor, geometry: dict[str, torch.Tensor], t: float) -> torch.Tensor:
    grid = torch.linspace(-1, 1, 11, device=actions.device, dtype=actions.dtype)
    initial = ((geometry['current_gripper_command'] + 1) / 0.2).round().long().clamp(0, 10)
    probabilities = torch.nn.functional.one_hot(initial, 11).to(actions.dtype)
    widths = geometry['gripper_width_scale'] * (grid + 1)
    squared_errors = (widths[None] - geometry['target_width'][:, None]).square()
    signed = 2 * actions[..., 6] - 1
    close = (signed > 0).to(actions.dtype) if t == 1 else torch.special.ndtr(signed * t / (1 - t))
    errors = []
    for index in range(actions.shape[1]):
        zero = torch.zeros_like(probabilities[:, :1])
        closed = torch.cat((probabilities[:, :2].sum(1, keepdim=True), probabilities[:, 2:], zero), 1)
        opened = torch.cat((zero, probabilities[:, :-2], probabilities[:, -2:].sum(1, keepdim=True)), 1)
        probability = close[:, index, None]
        probabilities = probability * closed + (1 - probability) * opened
        errors.append((probabilities * squared_errors).sum(1))
    return torch.stack(errors, 1)


# Input: complete-pose errors [B,H], canonical t; output: soft-time loss [B] and shared weights [B,H].
def reduce_errors(errors: torch.Tensor, t: float) -> tuple[torch.Tensor, torch.Tensor]:
    if t == 1:
        indices = errors.detach().argmin(1)
        weights = torch.nn.functional.one_hot(indices, errors.shape[1]).to(errors.dtype)
        return (weights * errors).sum(1), weights
    logits = -(t / (1 - t)) ** 2 * errors
    weights = logits.softmax(1)
    soft = -(1 - t) ** 2 * (torch.logsumexp(logits, 1) - math.log(errors.shape[1]))
    return soft * ((1 - t) / t if t > 0 else 0), weights


# Input: normalized [B,H,12], one full target and canonical t; output: soft-time loss and command diagnostics.
def pose_loss(actions: torch.Tensor, geometry: dict[str, torch.Tensor], t: float = 0.5) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    positions, rotations = decode(actions, geometry)
    dp = positions - geometry['target_position'][:, None]
    dr2 = (rotations - geometry['target_rotation'][:, None]).square().sum((-1, -2))
    dw2 = gripper_errors(actions, geometry, t)
    control_scale = (geometry['output_max'] - geometry['output_min']) / 2
    errors = 0.5 * (dp / control_scale[:3]).square().sum(-1)
    errors = errors + 0.25 * dr2 / control_scale[3:].mean().square()
    errors = errors + 0.5 * dw2 / geometry['gripper_width_scale'].square()
    loss, weights = reduce_errors(errors, t)
    indices = errors.detach().argmin(1)
    batch = torch.arange(actions.shape[0], device=actions.device)
    return loss, {'matching_weights': weights, 'matched_index': indices,
                  'position_error_m': dp.square().sum(-1)[batch, indices].sqrt(),
                  'orientation_error_rad': (1 - dr2[batch, indices] / 4).clamp(-1, 1).acos(),
                  'gripper_error_m': dw2[batch, indices].sqrt()}


''' + s[end:]
start=s.index('# Input: frozen head, original noise')
s=s[:start]+'''# Input: frozen head, original noise [B,H,12], optional RGB-D target; output: candidate and sampling trace.
def sample(head: Any, vl: torch.Tensor, state: torch.Tensor, mask: Any, noise: torch.Tensor,
           geometry: dict[str, torch.Tensor] | None, scale: float) -> tuple[torch.Tensor, list[dict[str, Any]]]:
    assert head.state_placement == 'condition'
    with torch.no_grad():
        obs = head.obs_proj(vl, state)
        obs_mask = head._build_obs_attention_mask(mask, obs, state)
    actions = noise.clone().float()
    records = []
    for index in range(head.num_inference_timesteps):
        t = index / head.num_inference_timesteps
        active = geometry is not None and scale > 0 and 0.5 <= t < 0.75
        actions = actions.detach().requires_grad_(active)
        with torch.set_grad_enabled(active):
            v = velocity(head, actions, t, obs, obs_mask)
            correction = torch.zeros_like(v)
            record: dict[str, Any] = {'step': index, 't': t, 'active': active}
            if active:
                assert geometry is not None
                loss, diagnostics = pose_loss(actions + (1 - t) * v, geometry, t)
                gradient = torch.autograd.grad(loss.sum(), actions)[0]
                raw = scale * gradient
                vrms = v.detach().square().mean((1, 2), keepdim=True).sqrt()
                crms = raw.square().mean((1, 2), keepdim=True).sqrt()
                factor = (0.3 * vrms / crms.clamp_min(1e-12)).clamp(max=1)
                correction = raw * factor
                assert torch.isfinite(correction).all(), 'Nonfinite guidance correction'
                record.update({'loss': loss.detach().cpu().tolist(),
                               'gradient_rms': gradient.square().mean().sqrt().item(),
                               'velocity_rms': vrms.mean().item(), 'effective_scale': (scale * factor).mean().item(),
                               'correction_rms': correction.square().mean().sqrt().item(),
                               **{k: value.detach().cpu().tolist() for k, value in diagnostics.items()}})
            records.append(record)
        actions = (actions + (v - correction) / head.num_inference_timesteps).detach()
        assert torch.isfinite(actions).all(), 'Nonfinite candidate'
    return actions, records
'''
path.write_text(s)
p=root/'policy_server.py'
s=p.read_text().replace('import logging','import logging\nimport time')
s=s.replace('observation-derived four poses','observation-derived complete pose')
s=s.replace("            assert geometry['steps'] == [10, 25, 40, 50]\n",'')
s=s.replace("'fixed_base_arm_guidance'","'soft_time_track_pose025'")
s=s.replace('        actions, trace = sample', '        started = time.monotonic()\n        actions, trace = sample')
s=s.replace("'geometry_model': 'ideal_fixed_base_command_poses; binary_gripper_surrogate',", "'geometry_model': 'track_pose025_current_base_frame; native_gripper_expectation_11_states',\n                                 'mode': 'soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03',\n                                 'target_source': 'current_rgbd_and_robot_state', 'H': 50, 'N': 10, 'E': 16,\n                                 'rms_channels': 'all_12_native_channels', 'elapsed_seconds': time.monotonic() - started,")
s=s.replace('pose_loss(actions, tensors)','pose_loss(actions, tensors, t=1)')
s=s.replace("'anchors': [10, 25, 40, 50]", "'guidance': 'soft_time_track_pose025_fm_time_gripper_expectation_late_half_native_finish_cap03'")
p.write_text(s)
p=root/'sim_tool.py'
s=p.read_text()
s=s.replace("'gripper_qpos': self.obs['state.gripper_qpos'],", "'gripper_qpos': self.obs['state.gripper_qpos'],\n                'gripper_command': self.native.robots[0].gripper['right'].current_action.copy(),")
start=s.index('    # Input: four observation-grounded world poses;')
end=s.index('    # Input: short world-space full-pose',start)
s=s[:start]+'''    # Input: one observed full world pose; output: stationary-frame command-space geometry.
    def geometry(self, goal: dict[str, Any]) -> dict[str, Any]:
        state = self.robot_state()
        origin = state['controller_origin_position']
        rotation = state['controller_origin_rotation']
        return {'mode': 'soft_time_track_pose025',
                'current_position': [(state['position_world'] - origin) @ rotation],
                'current_rotation': [rotation.T @ state['rotation_world']],
                'current_gripper_command': [state['gripper_command'][0]], 'gripper_width_scale': 0.04,
                'target_position': [(np.asarray(goal['position_world']) - origin) @ rotation],
                'target_rotation': [rotation.T @ Rotation.from_quat(goal['quaternion_xyzw']).as_matrix()],
                'target_width': [goal['width_m']],
                **{k: state[k] for k in ('input_min', 'input_max', 'output_min', 'output_max')}}

    # Input: original observation and optional complete goal; output: native E16 controls or explicit rejection.
    def vla(self, chunks: int, goal: dict[str, Any] | None = None, scale: float = 0,
            verify: bool = False) -> dict[str, Any]:
        assert 1 <= chunks <= 256
        if goal is not None:
            assert chunks == 1 and scale > 0, 'Reobserve before another guided chunk'
            assert 0 <= goal['width_m'] <= 0.08
            if np.linalg.norm(np.asarray(goal['position_world']) - self.robot_state()['position_world']) > 0.25:
                return {'rejected': 'goal_too_far', 'steps': self.steps}
        for _ in range(chunks):
            if self.terminal:
                break
            seed = int.from_bytes(hashlib.sha256(f'{self.args.task}:{self.args.seed}:{self.attempt}:{self.chunk}'.encode()).digest()[:4], 'little')
            payload: dict[str, Any] = {'examples': [self.codec.build_model_example(self.obs)], 'seed': seed,
                                      'scale': scale, 'verify': verify}
            if goal is not None:
                payload['geometry'] = self.geometry(goal)
            response = self.client.predict_action(payload)
            assert response['ok'], response
            actions = self.codec.decode_actions(response['data']['normalized_actions'])
            self.chunk += 1
            audit = response['data']['audit']
            rejection = None
            if goal is not None:
                errors = audit['final_command_errors']
                if np.max(np.abs(actions['action.base_motion'][:16])) > 0.05 or np.any(actions['action.control_mode'][:16] > 0.5):
                    rejection = 'moving_base_codec_unsupported'
                elif errors['position_error_m'][0] > 0.10 or errors['orientation_error_rad'][0] > 1.2 or errors['gripper_error_m'][0] > 0.04:
                    rejection = 'guidance_unresolved'
            write_json(self.folder / f'inference-{self.chunk:05d}.json',
                       {'seed': seed, 'scale': scale, 'goal': goal, 'audit': audit,
                        'steps_before': self.steps, 'rejection': rejection, 'execution': 'unchanged_native12'})
            np.save(self.folder / f'candidate-{self.chunk:05d}.npy', response['data']['normalized_actions'])
            if rejection is not None:
                return {'rejected': rejection, 'steps': self.steps, 'audit': audit}
            self.execute({k: v[:16] for k, v in actions.items()}, 'guided_vla' if goal is not None else 'native_vla')
        return self.observe()

    # Input: robot/observation-derived full goal; output: numerical parity and guided diagnostics, zero execution.
    def probe(self, goal: dict[str, Any], scale: float = 30) -> dict[str, Any]:
        payload = {'examples': [self.codec.build_model_example(self.obs)], 'seed': 123,
                   'scale': scale, 'verify': True, 'geometry': self.geometry(goal)}
        response = self.client.predict_action(payload)
        assert response['ok'], response
        write_json(self.folder / f'probe-{self.command_id:05d}.json', response['data']['audit'])
        return response['data']['audit']

''' + s[end:]
s=s.replace("if k not in {'type', 'reason'}", "if k != 'type' and (k != 'reason' or kind == 'finish')")
p.write_text(s)

from copy import deepcopy
import math

import jax
import jax.numpy as jnp
import numpy as np
import pytest
import torch

from interactvla.feedback import backproject, bounded_edit, motion_feedback, project, shift_gripper_transition, target_frame
from interactvla.geometry import NativeGeometry, binary_width_moments, quaternion_matrix, rotation_matrix
from interactvla.jax_backend import JaxGeometry, sample as jax_sample
from interactvla.runtime import InteractiveTool, VERSION
from interactvla.sampling import GuidanceConfig, Proposal, correction_and_compatibility, masked_rms, sample, temporal_objective


def manifest(effectors=1, mode="delta", binary=True):
    items = []
    for e in range(effectors):
        arm = {"id": f"arm{e}", "mode": mode, "motion_channels": list(range(e*7, e*7+6)),
               "base_transform": np.eye(4).tolist(), "input_min": [-2]*6, "input_max": [2]*6,
               "output_min": [-2]*6, "output_max": [2]*6,
               "gripper": {"channel": e*7+6, "mode": "binary" if binary else "continuous",
                           "width_min": 0, "width_max": .08, "closing_polarity": 1, "scale": .04, "offset": .04}}
        if mode == "delta":
            arm.update(delta_frame="world", tracking=.25)
        elif mode == "absolute":
            arm["rotation_format"] = "rotvec"
        else:
            arm.update(joint_min=[-2]*6, joint_max=[2]*6,
                       joints=[{"type": "revolute", "axis": [0, 0, 1], "origin": np.eye(4).tolist()} for _ in range(6)])
        items.append(arm)
    dim = effectors*7+2
    return {"action_dim": dim, "channel_names": [f"a{i}" for i in range(dim)],
            "valid_channels": [True]*(dim-2)+[False]*2, "normalization": [{"kind": "identity"} for _ in range(dim)],
            "horizon": 10, "execute_prefix": 3, "control_rate_hz": 20, "frame_id": "base",
            "flow_convention": "increasing_velocity", "effectors": items}


def state(effectors=1):
    return {f"arm{e}": {"position": [0, 0, 0], "rotation": np.eye(3).tolist(), "width": .04} for e in range(effectors)}


def target(effectors=1):
    return {"frame_id": "base", "observation_id": "obs0", "effectors": {
        f"arm{e}": {"position": [.02, 0, 0], "rotation": np.eye(3).tolist(), "gripper": .5}
        for e in range(effectors)}}


def test_temporal_gradient_and_shared_assignment():
    energy = torch.tensor([[1., 0., 3.]], requires_grad=True)
    loss, weights = temporal_objective(energy, .6)
    gradient, = torch.autograd.grad(loss.sum(), energy)
    torch.testing.assert_close(gradient, .6*.4*weights)
    assert weights.argmax() == 1


def test_error_terms_share_one_arrival():
    components = torch.tensor([[[0., 1.], [1., 0.]]])
    _, weights = temporal_objective(components.sum(-1), .7)
    torch.testing.assert_close(weights, torch.full_like(weights, .5))


def test_masked_cap_excludes_padding_and_inactive_channels():
    g = torch.ones(1, 3, 4)
    v = torch.tensor([[[2., 1000., 1000., 1000.]]]).expand_as(g)
    mask = torch.tensor([True, False, False, False])
    correction, _, _ = correction_and_compatibility(g, v, torch.ones(1, 3), mask, GuidanceConfig())
    assert torch.count_nonzero(correction[..., 1:]) == 0
    assert float(masked_rms(correction, mask)) <= .6
    torch.testing.assert_close(masked_rms(v, mask), torch.tensor([2.]))


def test_zero_native_velocity_has_zero_guidance_budget():
    correction, _, _ = correction_and_compatibility(torch.ones(1, 2, 1), torch.zeros(1, 2, 1),
                                                     torch.zeros(1, 2), torch.tensor([True]), GuidanceConfig())
    assert torch.count_nonzero(correction) == 0


def test_gate_accepts_equality_and_rejects_before_update():
    config = GuidanceConfig()
    _, rejected, _ = correction_and_compatibility(torch.zeros(1, 2, 1), torch.ones(1, 2, 1),
                                                   torch.ones(1, 2), torch.tensor([True]), config)
    assert not rejected.any()
    result = sample(lambda x, t: torch.ones_like(x), torch.zeros(1, 2, 1),
                    lambda clean, t: 2+clean[..., 0]*0, config=config)
    assert result.status == "rejected"
    assert (result.forward_evaluations, result.backward_evaluations) == (7, 2)
    assert result.traces[-1]["t"] == .6


def test_current_expert_jacobian_and_detached_steps():
    parameter = torch.nn.Parameter(torch.tensor(2.), requires_grad=False)
    inputs = []
    def velocity(x, t):
        inputs.append(x)
        return parameter*x.flip(1)+.2
    result = sample(velocity, torch.ones(1, 2, 1)*.1,
                    lambda clean, t: clean[..., 0].square(), config=GuidanceConfig(threshold=0))
    assert result.status == "accepted" and result.backward_evaluations == 3
    assert all(value.grad_fn is None for value in inputs)
    assert parameter.grad is None and parameter.item() == 2
    x = torch.tensor([[[.2], [.4]]], requires_grad=True)
    v = velocity(x, .6)
    full = torch.autograd.grad(temporal_objective((x+.4*v)[..., 0].square(), .6)[0].sum(), x)[0]
    detached = torch.autograd.grad(temporal_objective((x+.4*v.detach())[..., 0].square(), .6)[0].sum(), x)[0]
    assert not torch.allclose(full, detached)


def test_no_target_matches_native_inference():
    noise = torch.randn(1, 10, 9)
    native = noise.clone()
    for i in range(10):
        native += .1*(native*.1+i/10)
    result = sample(lambda x, t: x*.1+t, noise)
    torch.testing.assert_close(result.actions, native)
    assert result.backward_evaluations == 0


def test_inference_mode_is_exited_for_guidance():
    with torch.inference_mode():
        result = sample(lambda x, t: .1*x, torch.zeros(1, 2, 1),
                        lambda clean, t: clean[..., 0].square(), config=GuidanceConfig(threshold=0))
    assert result.backward_evaluations == 3


def test_nonfinite_proposal_is_not_executable():
    result = sample(lambda x, t: x*float("nan"), torch.zeros(1, 2, 1))
    assert result.status == "numerical_failure" and not result.executable


def test_rotation_gradients_are_finite_at_zero():
    x = torch.zeros(2, 3, requires_grad=True)
    r = rotation_matrix(x)
    torch.testing.assert_close(r, torch.eye(3).expand(2, 3, 3))
    assert torch.isfinite(torch.autograd.grad(r.sum(), x)[0]).all()


def test_quaternion_normalization_and_sign():
    q = torch.tensor([1., 2., 3., 4.])
    torch.testing.assert_close(quaternion_matrix(q), quaternion_matrix(-2*q))
    with pytest.raises(ValueError):
        quaternion_matrix(torch.zeros(4))


def test_gripper_interpolates_initial_bins_and_retains_variance():
    command = torch.zeros(1, 1, requires_grad=True)
    mean, second = binary_width_moments(command, torch.tensor([.035]), 0, .08, .5)
    torch.testing.assert_close(mean, torch.tensor([[.035]]))
    assert float((second-mean.square()).detach()) > 0
    assert torch.autograd.grad(mean.sum(), command)[0].item() < 0


def test_binary_gripper_uses_clean_action_over_remaining_time():
    command = torch.tensor([[.4]])
    mean, _ = binary_width_moments(command, torch.tensor([.04]), 0, .08, .5)
    probability = torch.special.ndtr(torch.tensor(.8))
    torch.testing.assert_close(mean, (.05-.02*probability).reshape(1, 1))


@pytest.mark.parametrize("polarity", [-1, 1])
def test_binary_endpoint_and_saturation(polarity):
    mean, second = binary_width_moments(torch.ones(1, 10)*polarity, torch.tensor([.08]), 0, .08, 1, polarity)
    torch.testing.assert_close(mean[0, :8], torch.arange(7, -1, -1)*.01)
    assert mean[0, -1] == 0
    torch.testing.assert_close(second, mean.square())


def test_delta_tracking_and_body_rotation():
    spec = manifest()
    codec = NativeGeometry(spec)
    actions = torch.zeros(1, 3, 9)
    actions[..., 0] = .1
    pose = codec.decode(actions, state())
    torch.testing.assert_close(pose.position[0, :, 0, 0], torch.tensor([.1, .125, .15]))
    spec["effectors"][0]["delta_frame"] = "body"
    codec = NativeGeometry(spec)
    measured = state()
    measured["arm0"]["rotation"] = rotation_matrix(torch.tensor([0., 0., math.pi/2])).tolist()
    pose = codec.decode(actions, measured)
    torch.testing.assert_close(pose.position[0, :, 0, 1], torch.tensor([.1, .125, .15]))


def test_quantile_clipping_excludes_padding_from_gradients():
    spec = manifest()
    spec["normalization"][0] = {"kind": "quantile", "lower": -2, "upper": 2}
    codec = NativeGeometry(spec)
    x = torch.ones(1, 1, 9, requires_grad=True)*2
    gradient = torch.autograd.grad(codec.physical(x).sum(), x)[0]
    assert gradient[..., 0] == 0 and not gradient[..., -2:].any()


def test_unit_pose_scales():
    codec = NativeGeometry(manifest(mode="absolute", binary=False))
    raw = target()
    raw["effectors"]["arm0"].update(position=[0, 0, 0], gripper=.5)
    x = torch.zeros(1, 1, 9)
    x[..., 0] = .05
    x[..., 3] = math.pi/6
    torch.testing.assert_close(codec.errors(x, state(), codec.target(raw), .6), torch.tensor([[2.]]))


def test_bimanual_mask_and_base_mount():
    spec = manifest(2, "joint", False)
    spec["effectors"][1]["base_transform"][0][3] = 1
    codec = NativeGeometry(spec)
    raw = target(1)
    converted = codec.target(raw)
    assert codec.action_mask(converted).tolist() == [True]*7+[False]*9
    poses = codec.decode(torch.zeros(1, 2, 16), state(2))
    torch.testing.assert_close(poses.position[0, :, 1, 0], torch.ones(2))


@pytest.mark.parametrize("mode", ["delta", "absolute", "joint"])
@pytest.mark.parametrize("binary", [True, False])
def test_torch_jax_geometry_and_input_gradient_agree(mode, binary):
    codec = NativeGeometry(manifest(2, mode, binary))
    jcodec = JaxGeometry(codec)
    measured = state(2)
    goal = codec.target(target(2))
    jgoal = {k: jnp.asarray(v.numpy()) for k, v in goal.items()}
    rng = np.random.default_rng(123)
    x = rng.normal(0, .02, (1, 3, 16)).astype(np.float32)
    tx = torch.tensor(x, requires_grad=True)
    energy = codec.errors(tx, measured, goal, .6)
    gradient = torch.autograd.grad(energy.sum(), tx)[0]
    je, jg = jax.value_and_grad(lambda v: jcodec.errors(v, measured, jgoal, .6).sum())(jnp.asarray(x))
    np.testing.assert_allclose(float(je), float(energy.sum().detach()), rtol=2e-4, atol=1e-5)
    np.testing.assert_allclose(np.asarray(jg), gradient.numpy(), rtol=2e-4, atol=5e-5)


def test_torch_jax_sampler_agree():
    noise = np.random.default_rng(42).normal(0, .1, (1, 3, 2)).astype(np.float32)
    config = GuidanceConfig(threshold=0)
    tresult = sample(lambda x, t: .2*x+.1, torch.tensor(noise), lambda x, t: x.square().sum(-1), config=config)
    jresult = jax_sample(lambda x, t: .2*x+.1, jnp.asarray(noise), lambda x, t: jnp.sum(x*x, -1), config=config)
    np.testing.assert_allclose(np.asarray(jresult[0]), tresult.actions.numpy(), rtol=1e-5, atol=1e-6)
    assert jresult[-2:] == (10, 3)


def test_bounded_edit_has_fixed_endpoints_and_inactive_arm():
    codec = NativeGeometry(manifest(2, "absolute", False))
    x = torch.zeros(1, 10, 16)
    edited = bounded_edit(codec, x, state(2), 2, 6, [.01, 0, 0], [0, 0, .1], ["arm0"])
    torch.testing.assert_close(edited[:, [2, 6]], x[:, [2, 6]])
    torch.testing.assert_close(edited[..., 7:], x[..., 7:])
    assert edited[0, 4, 0] > 0
    with pytest.raises(ValueError):
        bounded_edit(codec, x, state(2), 2, 6, [.021, 0, 0], [0, 0, 0], ["arm0"])


def camera():
    return {"intrinsics": [[100, 0, 2], [0, 100, 2], [0, 0, 1]], "camera_from_base": np.eye(4).tolist(),
            "image_size": [5, 5], "calibration_id": "c0"}


def test_projection_depth_and_grounding():
    c = camera()
    depth = np.ones((5, 5))
    np.testing.assert_allclose(backproject([2, 2], depth, c), [0, 0, 1])
    projection = project([[0, 0, 1.03], [0, 0, -1]], c, depth)
    assert projection["occluded"] == [True, False] and projection["positive_depth"] == [True, False]
    with pytest.raises(ValueError):
        backproject([2, 2], np.zeros((5, 5)), c)
    r = np.asarray(target_frame([0, 0, 1], [0, 0, 1]))
    np.testing.assert_allclose(r@r.T, np.eye(3))


class FakeModel:
    def __init__(self, rejected=False):
        self.rejected = rejected
        self.calls = 0
    def generate(self, observation, target, seed, config):
        self.calls += 1
        return Proposal(torch.zeros(1, 10, 9), "rejected" if self.rejected else "accepted", "pose_mismatch" if self.rejected else None)


class FakeController:
    def __init__(self):
        self.version, self.commands = 0, []
    def observe(self):
        return {"id": f"obs{self.version}", "state_version": self.version, "frame_id": "base",
                "timestamp_s": self.version/20, "state": state(), "instruction": "move", "images": []}
    def execute(self, command, expected_state_version):
        assert expected_state_version == self.version
        self.commands.append(command)
        self.version += 1
        return {"status": "completed", "state_version": self.version, "measured_state": state()}
    def stop(self):
        pass


def make_tool(rejected=False):
    controller, model = FakeController(), FakeModel(rejected)
    tool = InteractiveTool(model, NativeGeometry(manifest()), controller, clock=lambda: 0)
    return tool, model, controller


def call(tool, name, request_id, **arguments):
    return tool.call({"schema_version": VERSION, "request_id": request_id, "tool": name,
                      "state_version": tool.state_version,
                      "observation_id": None if tool.observation is None else tool.observation["id"],
                      "arguments": arguments})


def test_proposal_never_executes_and_retries_are_idempotent():
    tool, model, controller = make_tool()
    call(tool, "observe", "o")
    first = call(tool, "guide", "g", target=target(), seed=1)
    second = call(tool, "guide", "g", target=target(), seed=1)
    assert first == second and model.calls == 1 and controller.commands == []
    message = {"schema_version": VERSION, "request_id": "x", "tool": "execute", "state_version": 0,
               "observation_id": "obs0", "arguments": {"proposal_id": first["proposal_id"], "prefix_length": 2}}
    execution = tool.call(message)
    assert execution["executed_action_count"] == 2
    assert tool.call(message) == execution and len(controller.commands) == 2


def test_rejection_budget_survives_observation_refresh():
    tool, _, controller = make_tool(True)
    call(tool, "observe", "o")
    for i in range(6):
        if i >= 3:
            call(tool, "observe", f"o{i}")
        result = call(tool, "guide", f"g{i}", target=target(), seed=1)
    assert tool.terminated and tool.rejections == 6
    rejected = call(tool, "execute", "x", proposal_id=result["proposal_id"], prefix_length=1)
    assert rejected["status"] == "rejected" and controller.commands == []


def test_stale_state_is_checked_at_execution():
    tool, _, controller = make_tool()
    call(tool, "observe", "o")
    proposal = call(tool, "propose", "p", seed=0)
    controller.version += 1
    result = call(tool, "execute", "x", proposal_id=proposal["proposal_id"], prefix_length=2)
    assert result["status"] == "stale" and controller.commands == []


def test_execution_prefix_limit():
    tool, _, controller = make_tool()
    call(tool, "observe", "o")
    proposal = call(tool, "propose", "p", seed=0)
    result = call(tool, "execute", "x", proposal_id=proposal["proposal_id"], prefix_length=4)
    assert result["status"] == "rejected" and controller.commands == []


def test_gripper_transition_limits():
    x = torch.zeros(1, 10, 9)
    x[:, :5, 6] = -1
    x[:, 5:, 6] = 1
    shifted = shift_gripper_transition(x, 6, 5, 2)
    assert bool((shifted[0, :7, 6] == -1).all())
    assert bool((shifted[0, 7:, 6] == 1).all())
    with pytest.raises(ValueError):
        shift_gripper_transition(x, 6, 5, 3)
    with pytest.raises(ValueError):
        shift_gripper_transition(x[:, :7], 6, 5, 1)


def test_feedback_glyphs_and_original_observation():
    codec = NativeGeometry(manifest())
    poses = codec.decode(torch.zeros(1, 10, 9), state())
    feedback = motion_feedback(poses, camera(), 2, 3, 7, target())
    assert 7 in feedback["glyph_indices"] and 2 in feedback["glyph_indices"]
    assert np.asarray(feedback["orientation_axes"]["pixels"]).shape[-2:] == (3, 2)
    assert feedback["executed"][:3] == [True, True, False]


def test_delta_edits_preserve_complete_continuation_geometry():
    codec = NativeGeometry(manifest(binary=False))
    x = torch.zeros(1, 10, 9)
    x[..., 0] = .005
    original = codec.decode(x, state())
    edited = bounded_edit(codec, x, state(), 2, 6, [.01, 0, 0], [0, 0, .1], ["arm0"])
    decoded = codec.decode(edited, state())
    torch.testing.assert_close(decoded.position[:, [0, 1, 2, 6, 7, 8, 9]], original.position[:, [0, 1, 2, 6, 7, 8, 9]], atol=1e-6, rtol=1e-5)
    assert decoded.position[0, 4, 0, 0] > original.position[0, 4, 0, 0]


def test_bounded_joint_edit_converges_and_failure_rejects():
    spec = manifest(mode="joint", binary=False)
    arm = spec["effectors"][0]
    for joint, axis in zip(arm["joints"][:3], np.eye(3)):
        joint["type"], joint["axis"] = "prismatic", axis.tolist()
    codec = NativeGeometry(spec)
    x = torch.zeros(1, 10, 9)
    edited = bounded_edit(codec, x, state(), 2, 6, [.01, 0, 0], [0, 0, 0], ["arm0"])
    assert abs(float(codec.decode(edited, state()).position[0, 4, 0, 0])-.01) < .002
    for joint in arm["joints"]:
        joint["type"] = "revolute"
    codec = NativeGeometry(spec)
    with pytest.raises(ValueError, match="converge"):
        bounded_edit(codec, x, state(), 2, 6, [.01, 0, 0], [0, 0, 0], ["arm0"])


def test_nonidentity_delta_base_transform_torch_jax():
    spec = manifest()
    spec["effectors"][0]["base_transform"] = np.eye(4).tolist()
    spec["effectors"][0]["base_transform"][0][:3] = [0, -1, 0]
    spec["effectors"][0]["base_transform"][1][:3] = [1, 0, 0]
    codec = NativeGeometry(spec)
    x = torch.zeros(1, 3, 9)
    x[..., 0] = .1
    p = codec.decode(x, state()).position
    np.testing.assert_allclose(p.numpy(), np.asarray(JaxGeometry(codec).decode(jnp.asarray(x.numpy()), state(), .6)[0]), atol=1e-6)
    torch.testing.assert_close(p[0, :, 0, 1], torch.tensor([.1, .125, .15]))


def test_unsteered_native_channels_are_preserved():
    spec = manifest()
    spec["valid_channels"] = [True]*9
    spec["unsteered_channels"] = [7, 8]
    codec = NativeGeometry(spec)
    assert codec.action_mask(codec.target(target())).tolist() == [True]*7+[False]*2
    x = torch.zeros(1, 10, 9)
    x[..., 7:] = .3
    torch.testing.assert_close(codec.execution_actions(x)[..., 7:], x[..., 7:])


def test_continuous_execution_width_and_joint_limits():
    spec = manifest(mode="joint", binary=False)
    spec["effectors"][0]["joint_min"] = [-.5]*6
    spec["effectors"][0]["joint_max"] = [.5]*6
    codec = NativeGeometry(spec)
    commands = codec.execution_actions(torch.ones(1, 10, 9)*2)
    assert float(commands[..., :6].max()) == .5
    assert float(commands[..., 6].max()) == 1


def load_module(relative, name):
    import importlib.util
    from pathlib import Path
    import sys
    path = Path(__file__).resolve().parents[1]/relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_robocasa_adapter_uses_same_complete_pose_energy():
    module = load_module("code/work_dirs/robocasa365_agent/guidance.py", "casa_guidance_test")
    g = {"action_min": -torch.ones(12), "action_max": torch.ones(12),
         "input_min": -torch.ones(6), "input_max": torch.ones(6),
         "output_min": -torch.tensor([.05]*3+[.5]*3), "output_max": torch.tensor([.05]*3+[.5]*3),
         "current_position": torch.zeros(1, 3), "current_rotation": torch.eye(3)[None],
         "current_gripper_command": torch.zeros(1), "gripper_width_scale": torch.tensor(.04),
         "target_position": torch.zeros(1, 3), "target_rotation": torch.eye(3)[None], "target_width": torch.tensor([.04])}
    x = torch.zeros(1, 3, 12, requires_grad=True)
    loss, diagnostics = module.pose_loss(x, g, .6)
    expected, weights = temporal_objective(diagnostics["complete_pose_errors"], .6)
    torch.testing.assert_close(loss, expected)
    torch.testing.assert_close(diagnostics["matching_weights"], weights)
    assert torch.isfinite(torch.autograd.grad(loss.sum(), x)[0]).all()


def test_robodojo_adapter_uses_shared_temporal_envelope():
    module = load_module("skills/SandboxRSI/sources/dependencies/guidance.py", "dojo_guidance_test")
    energy = torch.tensor([[.5, 1., 3.]], requires_grad=True)
    loss = module.reduce_errors(energy, .7, module.Setting())
    expected = temporal_objective(energy, .7)[0].sum()
    torch.testing.assert_close(loss, expected)
    codec = module.Geometry({"q01": [-1]*14, "q99": [1]*14})
    assert len(codec.origins) == 6


def test_libero_adapter_uses_shared_complete_pose_energy():
    module = load_module("code/mr-starvla/work_dirs/keypoint_flow/evaluation/pi05_dynamic_guidance/geometry.py", "libero_geometry_test")
    energy = jnp.array([[.1, 1., 2.]])
    loss, _, weights = module.reduce_errors(energy, .4)
    expected, expected_weights = temporal_objective(torch.tensor(np.asarray(energy)), .6)
    np.testing.assert_allclose(np.asarray(loss), expected.numpy(), rtol=1e-5)
    np.testing.assert_allclose(np.asarray(weights), expected_weights.numpy(), rtol=1e-5)


def test_native_head_is_frozen_and_context_is_cached_once():
    from interactvla.models import StarVLAModel
    from types import SimpleNamespace
    class Head(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.tensor(.1))
            self.action_dim, self.action_horizon = 9, 10
        def denoise(self, prefix, x, state, clock, repeat_factor, readout_kv):
            return self.weight*x.flip(1)
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.action_model = Head()
    spec = manifest()
    spec["flow_convention"] = "clean_action"
    codec = NativeGeometry(spec)
    source = Model()
    original = {k: v.detach().clone() for k, v in source.state_dict().items()}
    adapter = StarVLAModel(source, codec)
    prepared = []
    def prepare(observation):
        prepared.append(observation)
        return {"prefix": SimpleNamespace(), "state": torch.zeros(1, 1, 9), "readout": None}
    adapter.prepare = prepare
    result = adapter.generate({"state": state()}, codec.target(target()), 1, GuidanceConfig(threshold=0))
    assert result.status == "accepted" and result.backward_evaluations == 3
    assert len(prepared) == 1
    for name, value in source.state_dict().items():
        torch.testing.assert_close(original[name], value)
    assert not any(p.requires_grad for p in source.parameters())


def test_public_compatibility_entrypoints_use_native_runtime():
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"code/mr-starvla"))
    from starVLA.keypoint_flow.sampler import sample as public_sample
    from starVLA.keypoint_flow.runtime import InteractiveTool as PublicTool
    assert public_sample is sample and PublicTool is InteractiveTool


def test_replaying_stop_does_not_cancel_a_new_observation():
    tool, _, _ = make_tool()
    call(tool, "observe", "o")
    message = {"schema_version": VERSION, "request_id": "stop0", "tool": "stop", "arguments": {}}
    stopped = tool.call(message)
    call(tool, "observe", "o1")
    assert not tool.stop_event.is_set()
    assert tool.call(message) == stopped
    assert not tool.stop_event.is_set() and not tool.needs_observation

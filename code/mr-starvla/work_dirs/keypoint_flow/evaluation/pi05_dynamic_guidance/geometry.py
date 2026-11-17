import jax
import jax.numpy as jnp
from jax.scipy.special import logsumexp
from interactvla.jax_backend import binary_width_moments, rotation_matrix, strict_clip


REDUCTIONS = ("hard", "fixed", "soft_time", "hard_t2", "mean", "hard_control", "soft_time_control",
              "soft_time_control_cap01", "soft_time_control_cap03", "fixed_control", "mean_control",
              "hard_t2_control", "fixed_t2_control", "soft_time_control_track_position025",
              "soft_time_control_track_pose025", "soft_time_control_track_pose025_fm_time",
              "soft_time_control_track_pose025_fm_time_log1p",
              "soft_time_control_track_pose025_fm_time_no_t2",
              "soft_time_control_track_pose025_fm_time_gripper_expectation",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_arm_third",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_rotation_third",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half",
              "hard_t2_control_track_pose025_fm_time_gripper_expectation",
              "fixed_t2_control_track_pose025_fm_time_gripper_expectation",
              "mean_t2_control_track_pose025_fm_time_gripper_expectation",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_tolerance",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_deadzone",
              "soft_time_control_track_pose025_fm_time_gripper_expectation_late_half_native_finish")

POSE_TOLERANCES = {"position_m": 0.05, "orientation_rad": 0.5235987755982988, "gripper_m": 0.08}
FIRST_CONTROL_MODE = "first_control_exact_fm_time_gripper_expectation"


def reduce_errors(errors, tau, reduction=2):
    t = 1-tau
    beta = t*t/jnp.maximum(tau*tau, 1e-12)
    logits = -beta*errors
    weights = jax.nn.softmax(logits, -1)
    loss = -tau**3/jnp.maximum(t, 1e-12)*(logsumexp(logits, -1)-jnp.log(errors.shape[-1]))
    indices = errors.argmin(-1)
    minimum = errors.min(-1)
    tied = (errors == minimum[:, None]).astype(jnp.float32)
    tied = tied/tied.sum(-1, keepdims=True)
    weights = jnp.where(tau == 0, tied, weights)
    loss = jnp.where(tau == 0, minimum, jnp.where(t == 0, 0, loss))
    return loss, indices, weights


def decode_tracking(actions, geometry):
    clipped = strict_clip(actions[..., :6], geometry["input_min"], geometry["input_max"])
    delta = geometry["output_min"]+(clipped-geometry["input_min"])*(geometry["output_max"]-geometry["output_min"])/(geometry["input_max"]-geometry["input_min"])
    positions = geometry["current_position"][:, None]+.25*(jnp.cumsum(delta[..., :3], 1)-delta[..., :3])+delta[..., :3]
    def advance(previous, dr):
        return rotation_matrix(.25*dr)@previous, rotation_matrix(dr)@previous
    _, rotations = jax.lax.scan(advance, geometry["current_rotation"], jnp.swapaxes(delta[..., 3:6], 0, 1))
    return positions, jnp.swapaxes(rotations, 0, 1)


def decode_selected(actions, geometry, reduction=2):
    return decode_tracking(actions, geometry)


def command_width(actions, geometry):
    initial = geometry["gripper_width_scale"]*(geometry["current_gripper_command"]+1)
    return binary_width_moments(actions[..., 6], initial, 0, 2*geometry["gripper_width_scale"], 1., 1)[0]


def expected_gripper_error(actions, geometry, tau, use_tolerance=False, preserve_exterior=False):
    initial = geometry["gripper_width_scale"]*(geometry["current_gripper_command"]+1)
    mean, second = binary_width_moments(actions[..., 6], initial, 0, 2*geometry["gripper_width_scale"], 1-tau, 1)
    target = geometry["target_width"][:, None]
    return jnp.maximum(second-2*mean*target+target*target, 0)


def match_pose(positions, rotations, widths, geometry, tau=.5, reduction=2, width_squared_errors=None,
               arm_weight=1., rotation_weight=None):
    dp2 = jnp.sum((positions-geometry["target_position"][:, None])**2, -1)
    dr2 = jnp.sum((rotations-geometry["target_rotation"][:, None])**2, (-1, -2))
    dw2 = (widths-geometry["target_width"][:, None])**2 if width_squared_errors is None else width_squared_errors
    components = jnp.stack((dp2/.05**2, dr2/(8*jnp.sin(jnp.pi/12)**2), dw2/(2*geometry["gripper_width_scale"])**2), -1)
    components = components*geometry.get("component_mask", jnp.ones(3))
    errors = components.sum(-1)
    loss, indices, weights = reduce_errors(errors, tau, reduction)
    batch = jnp.arange(errors.shape[0])
    return loss, {"complete_pose_errors": errors, "matched_index": indices, "components": components[batch, indices],
                  "matching_weights": weights, "guidance_loss": loss,
                  "position_error_m": jnp.sqrt(dp2[batch, indices]),
                  "orientation_error_rad": jnp.arccos(jnp.clip(1-dr2[batch, indices]/4, -1, 1)),
                  "gripper_error_m": jnp.sqrt(dw2[batch, indices])}


def pose_loss(actions, geometry, tau=.5, reduction=2):
    positions, rotations = decode_tracking(actions, geometry)
    widths = command_width(actions, geometry)
    expected = expected_gripper_error(actions, geometry, tau)
    return match_pose(positions, rotations, widths, geometry, tau, reduction, expected)


def evaluate_final(actions, geometry):
    loss, diagnostics = pose_loss(actions, geometry, tau=0.)
    return {**diagnostics, "loss": loss}

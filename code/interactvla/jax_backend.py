import math

import jax
import jax.numpy as jnp
import numpy as np
from jax.scipy.special import logsumexp, ndtr

from .sampling import GuidanceConfig


def rotation_matrix(vector):
    vector = vector.astype(jnp.float32)
    squared = jnp.sum(vector**2, -1)
    angle = jnp.sqrt(jnp.maximum(squared, 1e-8))
    a = jnp.where(squared < 1e-8, 1-squared/6+squared**2/120, jnp.sin(angle)/angle)
    b = jnp.where(squared < 1e-8, .5-squared/24+squared**2/720, 2*jnp.sin(angle/2)**2/angle**2)
    x, y, z = jnp.moveaxis(vector, -1, 0)
    zero = jnp.zeros_like(x)
    skew = jnp.stack((zero, -z, y, z, zero, -x, -y, x, zero), -1).reshape(*vector.shape[:-1], 3, 3)
    return jnp.eye(3) + a[..., None, None]*skew + b[..., None, None]*(skew@skew)


def quaternion_matrix(q):
    norm = jnp.linalg.norm(q, axis=-1, keepdims=True)
    q = q / norm
    x, y, z, w = jnp.moveaxis(q, -1, 0)
    return jnp.stack((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w),
                       2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w),
                       2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)), -1).reshape(*q.shape[:-1], 3, 3)


def strict_clip(x, lo, hi):
    return jnp.where(x <= lo, lo, jnp.where(x >= hi, hi, x))


def binary_width_moments(command, initial, lo, hi, t, polarity):
    grid = jnp.arange(9, dtype=jnp.float32)
    initial = jnp.clip(8*(hi-initial)/(hi-lo), 0, 8)
    probabilities = jnp.maximum(1-jnp.abs(grid-initial[..., None]), 0)
    widths = hi-grid/8*(hi-lo)
    signed = command*polarity
    close = jnp.where(t == 1, (signed > 0).astype(jnp.float32), ndtr(signed/jnp.maximum(1-t, 1e-8)))
    hold = jnp.where(t == 1, (signed == 0).astype(jnp.float32), 0)

    def advance(previous, chances):
        c, stay = chances
        zero = jnp.zeros_like(previous[:, :1])
        closed = jnp.concatenate((zero, previous[:, :-2], previous[:, -2:].sum(-1, keepdims=True)), -1)
        opened = jnp.concatenate((previous[:, :2].sum(-1, keepdims=True), previous[:, 2:], zero), -1)
        p = c[:, None]*closed + (1-c-stay)[:, None]*opened + stay[:, None]*previous
        return p, ((p*widths).sum(-1), (p*widths**2).sum(-1))

    _, (mean, second) = jax.lax.scan(advance, probabilities, (close.T, hold.T))
    return mean.T, second.T


class JaxGeometry:
    def __init__(self, geometry):
        self.manifest = geometry.manifest
        for name in ("kind", "lower", "upper", "mean", "std", "valid"):
            setattr(self, name, jnp.asarray(getattr(geometry, name).cpu().numpy()))

    def physical(self, x):
        q = self.lower+(strict_clip(x, -1, 1)+1)*(self.upper-self.lower)/2
        return jnp.where(self.valid, jnp.where(self.kind == 1, q, jnp.where(self.kind == 2, x*self.std+self.mean, x)), 0)

    def decode(self, x, state, t):
        physical = self.physical(x.astype(jnp.float32))
        positions, rotations, widths, seconds = [], [], [], []
        for effector in self.manifest["effectors"]:
            lo, hi, olo, ohi = (jnp.asarray(effector[k]) for k in ("input_min", "input_max", "output_min", "output_max"))
            motion = olo+(strict_clip(physical[..., jnp.asarray(effector["motion_channels"])], lo, hi)-lo)*(ohi-olo)/(hi-lo)
            transform = jnp.asarray(effector["base_transform"], dtype=jnp.float32)
            if effector["mode"] == "joint":
                motion = strict_clip(motion, jnp.asarray(effector["joint_min"]), jnp.asarray(effector["joint_max"]))
                transform = jnp.broadcast_to(transform, (*x.shape[:2], 4, 4))
                for i, joint in enumerate(effector["joints"]):
                    axis = jnp.asarray(joint["axis"])
                    joint_transform = jnp.broadcast_to(jnp.eye(4), transform.shape)
                    if joint["type"] == "revolute":
                        joint_transform = joint_transform.at[..., :3, :3].set(rotation_matrix(motion[..., i, None]*axis))
                    else:
                        joint_transform = joint_transform.at[..., :3, 3].set(motion[..., i, None]*axis)
                    transform = transform @ jnp.asarray(joint["origin"]) @ joint_transform
                transform = transform @ jnp.asarray(effector.get("tool_transform", np.eye(4)))
                p, r = transform[..., :3, 3], transform[..., :3, :3]
            elif effector["mode"] == "absolute":
                p = (transform[:3, :3]@motion[..., :3, None])[..., 0]+transform[:3, 3]
                r = quaternion_matrix(motion[..., 3:7]) if effector["rotation_format"] == "xyzw" else rotation_matrix(motion[..., 3:6])
                r = transform[:3, :3]@r
            else:
                current = state[effector["id"]]
                initial = (jnp.broadcast_to(jnp.asarray(current["position"], dtype=jnp.float32), (x.shape[0], 3)),
                           jnp.broadcast_to(jnp.asarray(current["rotation"], dtype=jnp.float32), (x.shape[0], 3, 3)))
                eta = effector.get("tracking", .25)
                body = effector["delta_frame"] == "body"

                def advance(previous, delta):
                    pbar, rbar = previous
                    dp, dr = delta[..., :3], delta[..., 3:]
                    if body:
                        dp = (rbar@dp[..., None])[..., 0]
                        rotation = rbar@rotation_matrix(dr)
                        next_rotation = rbar@rotation_matrix(eta*dr)
                    else:
                        base_rotation = jnp.asarray(effector["base_transform"], dtype=jnp.float32)[:3, :3]
                        dp = (base_rotation@dp[..., None])[..., 0]
                        dr = (base_rotation@dr[..., None])[..., 0]
                        rotation = rotation_matrix(dr)@rbar
                        next_rotation = rotation_matrix(eta*dr)@rbar
                    return (pbar+eta*dp, next_rotation), (pbar+dp, rotation)

                _, (p, r) = jax.lax.scan(advance, initial, jnp.swapaxes(motion, 0, 1))
                p, r = jnp.swapaxes(p, 0, 1), jnp.swapaxes(r, 0, 1)
            grip = effector["gripper"]
            command = physical[..., grip["channel"]]
            lo, hi = grip["width_min"], grip["width_max"]
            if grip["mode"] == "binary":
                initial = jnp.full((x.shape[0],), state[effector["id"]]["width"])
                command = grip.get("command_scale", 1)*command+grip.get("command_offset", 0)
                w, w2 = binary_width_moments(command, initial, lo, hi, t, grip["closing_polarity"])
            else:
                w = strict_clip(grip["scale"]*command+grip["offset"], lo, hi)
                w2 = w**2
            positions.append(p); rotations.append(r); widths.append(w); seconds.append(w2)
        return tuple(jnp.stack(values, 2) for values in (positions, rotations, widths, seconds))

    def errors(self, x, state, target, t):
        p, r, w, w2 = self.decode(x, state, t)
        dp = jnp.sum((p-target["position"])**2, -1)/.05**2
        dr = jnp.sum((r-target["rotation"])**2, (-1, -2))/(8*math.sin(math.pi/12)**2)
        ranges = jnp.asarray([e["gripper"]["width_max"]-e["gripper"]["width_min"] for e in self.manifest["effectors"]])
        dw = jnp.maximum(w2-2*w*target["width"]+target["width"]**2, 0)/ranges**2
        return jnp.where(target["mask"], jnp.stack((dp, dr, dw), -1), 0).sum((-1, -2))


def sample(velocity, noise, errors=None, action_mask=None, config=None):
    config = config or GuidanceConfig()
    x = jnp.asarray(noise, dtype=jnp.float32)
    mask = jnp.ones_like(x, dtype=bool) if action_mask is None else jnp.broadcast_to(action_mask, x.shape)
    if errors is not None and np.any(np.asarray(mask.sum((-1, -2))) == 0):
        raise ValueError("The active action mask is empty")
    traces, forwards, backwards = [], 0, 0
    count = mask.sum((-1, -2))
    rms = lambda value: jnp.sqrt(jnp.sum(jnp.where(mask, value, 0).astype(jnp.float32)**2, (-1, -2))/count)
    candidate = x
    for i in range(config.steps):
        t = i/config.steps
        active = errors is not None and config.gain > 0 and config.active_start <= t < config.active_end
        record = {"step": i, "t": t, "active": active}
        if active:
            def objective(value):
                v = velocity(value, t).astype(jnp.float32)
                clean = value+(1-t)*v
                energy = errors(clean, t)
                logits = -(t/(1-t))**2*energy
                loss = -(1-t)**3/t*(logsumexp(logits, -1)-math.log(energy.shape[-1]))
                return loss.sum(), (v, clean, energy, jax.nn.softmax(logits, -1), loss)
            (loss, (v, candidate, energy, weights, losses)), gradient = jax.value_and_grad(objective, has_aux=True)(x)
            backwards += 1
            grms, vrms = rms(gradient), rms(v)
            scale = jnp.minimum(config.gain, config.relative_cap*vrms/(grms+config.rms_epsilon))
            correction = scale[:, None, None]*jnp.where(mask, gradient, 0)
            residual = energy.min(-1)
            demand = config.gain*grms/(vrms+config.velocity_epsilon)
            severity = jnp.maximum(residual/config.residual_scale, demand/config.demand_scale)
            values = (loss, energy, gradient, correction)
            valid = all(np.isfinite(np.asarray(value)).all() for value in values)
            if valid:
                record.update({key: np.asarray(value).tolist() for key, value in {
                    "minimum_pose_error": residual, "matched_index": energy.argmin(-1),
                    "relative_correction_demand": demand, "compatibility": jnp.exp(-severity),
                    "gradient_rms": grms, "velocity_rms": vrms, "effective_gain": scale,
                    "correction_rms": rms(correction), "matching_weights": weights, "loss": losses}.items()})
                record["gradient_norm_by_action"] = np.asarray(jnp.linalg.norm(jnp.where(mask, gradient, 0), axis=-1)).tolist()
            checkpoint = any(abs(t-c) < 1e-8 for c in config.checkpoints)
            record["compatibility_checked"] = checkpoint
        else:
            v = velocity(x, t).astype(jnp.float32)
            candidate = x+(1-t)*v
            correction = jnp.zeros_like(x)
            valid = True
        forwards += 1
        if not valid or not np.isfinite(np.asarray(candidate)).all():
            traces.append({"step": i, "t": t, "operation": "generation", "error": "Nonfinite proposal"})
            return candidate, "numerical_failure", "generation", traces, forwards, backwards
        traces.append(record)
        if active and checkpoint and config.threshold > 0 and bool(np.any(np.asarray(severity) > -math.log(config.threshold))):
            limit = -math.log(config.threshold)
            bp = bool(np.any(np.asarray(residual)/config.residual_scale > limit))
            bd = bool(np.any(np.asarray(demand)/config.demand_scale > limit))
            reason = "pose_and_correction_mismatch" if bp and bd else "pose_mismatch" if bp else "excessive_correction_demand"
            return candidate, "rejected", reason, traces, forwards, backwards
        x = jax.lax.stop_gradient(x+(v-correction)/config.steps)
        if not np.isfinite(np.asarray(x)).all():
            return candidate, "numerical_failure", "state_update", traces, forwards, backwards
    return x, "accepted", None, traces, forwards, backwards

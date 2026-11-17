from dataclasses import dataclass
import math

import numpy as np
import torch


def rotation_matrix(vector):
    vector = vector.float()
    squared = vector.square().sum(-1)
    angle = squared.clamp_min(1e-8).sqrt()
    a = torch.where(squared < 1e-8, 1 - squared / 6 + squared.square() / 120, angle.sin() / angle)
    b = torch.where(squared < 1e-8, 0.5 - squared / 24 + squared.square() / 720,
                    2 * (angle / 2).sin().square() / angle.square())
    x, y, z = vector.unbind(-1)
    zero = torch.zeros_like(x)
    skew = torch.stack((zero, -z, y, z, zero, -x, -y, x, zero), -1).reshape(*vector.shape[:-1], 3, 3)
    return torch.eye(3, device=vector.device) + a[..., None, None] * skew + b[..., None, None] * (skew @ skew)


def quaternion_matrix(quaternion):
    quaternion = quaternion.float()
    norm = quaternion.norm(dim=-1, keepdim=True)
    if not bool(torch.isfinite(quaternion).all()) or bool((norm <= 1e-12).any()):
        raise ValueError("Invalid quaternion")
    x, y, z, w = (quaternion / norm).unbind(-1)
    return torch.stack((1 - 2 * (y*y + z*z), 2*(x*y-z*w), 2*(x*z+y*w),
                        2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w),
                        2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)), -1).reshape(*quaternion.shape[:-1], 3, 3)


def validate_rotation(value):
    r = np.asarray(value, dtype=np.float64)
    if r.shape[-2:] != (3, 3) or not np.isfinite(r).all():
        raise ValueError("Invalid rotation matrix")
    if not np.allclose(r @ np.swapaxes(r, -1, -2), np.eye(3), atol=1e-5) or not np.allclose(np.linalg.det(r), 1, atol=1e-5):
        raise ValueError("Rotation must be in SO(3)")


def strict_clip(value, lower, upper):
    return torch.where(value <= lower, lower, torch.where(value >= upper, upper, value))


def binary_width_moments(command, initial_width, lower, upper, t, polarity=1):
    command = command.float() * polarity
    initial = (8 * (upper - initial_width) / (upper - lower)).clamp(0, 8)
    grid = torch.arange(9, device=command.device, dtype=torch.float32)
    probabilities = (1 - (grid - initial[..., None]).abs()).clamp_min(0)
    widths = upper - grid / 8 * (upper - lower)
    close = (command > 0).float() if t == 1 else torch.special.ndtr(command / (1 - t))
    hold = (command == 0).float() if t == 1 else torch.zeros_like(command)
    means, seconds = [], []
    for h in range(command.shape[1]):
        zero = torch.zeros_like(probabilities[:, :1])
        closed = torch.cat((zero, probabilities[:, :-2], probabilities[:, -2:].sum(-1, keepdim=True)), -1)
        opened = torch.cat((probabilities[:, :2].sum(-1, keepdim=True), probabilities[:, 2:], zero), -1)
        c, stay = close[:, h, None], hold[:, h, None]
        probabilities = c * closed + (1 - c - stay) * opened + stay * probabilities
        means.append((probabilities * widths).sum(-1))
        seconds.append((probabilities * widths.square()).sum(-1))
    return torch.stack(means, 1), torch.stack(seconds, 1)


@dataclass
class CommandPoses:
    position: torch.Tensor
    rotation: torch.Tensor
    width: torch.Tensor
    width_second_moment: torch.Tensor

    def to_dict(self):
        return {name: getattr(self, name).detach().cpu().tolist()
                for name in ("position", "rotation", "width", "width_second_moment")}


class NativeGeometry:
    def __init__(self, manifest, device="cpu"):
        self.manifest = manifest
        self.device = torch.device(device)
        self.action_dim = int(manifest["action_dim"])
        self.horizon = int(manifest["horizon"])
        self.execute_prefix = int(manifest["execute_prefix"])
        self.frame = manifest["frame_id"]
        self.effectors = manifest["effectors"]
        self.rate = float(manifest["control_rate_hz"])
        if manifest["flow_convention"] not in ("clean_action", "decreasing_velocity", "increasing_velocity"):
            raise ValueError("Unknown flow convention")
        if not self.frame or self.action_dim < 1 or not 0 < self.execute_prefix <= self.horizon or not math.isfinite(self.rate) or self.rate <= 0:
            raise ValueError("Invalid native action contract")
        names = manifest["channel_names"]
        if len(names) != self.action_dim or len(set(names)) != len(names):
            raise ValueError("Action channel names must be unique and cover the model output")
        if any(type(value) is not bool for value in manifest["valid_channels"]):
            raise ValueError("Action validity must use Boolean entries")
        self.valid = self.tensor(manifest["valid_channels"], torch.bool)
        if self.valid.shape != (self.action_dim,) or not bool(self.valid.any()):
            raise ValueError("Invalid action validity mask")
        norms = manifest["normalization"]
        if len(norms) != self.action_dim:
            raise ValueError("Every action channel needs one normalization transform")
        self.kind, self.lower, self.upper, self.mean, self.std = [], [], [], [], []
        for n in norms:
            kind = n["kind"]
            if kind not in ("identity", "quantile", "mean_std"):
                raise ValueError("Unknown normalization transform")
            lo, hi, mean, std = n.get("lower", -1), n.get("upper", 1), n.get("mean", 0), n.get("std", 1)
            if not np.isfinite([lo, hi, mean, std]).all() or hi <= lo or std <= 0:
                raise ValueError("Invalid normalization statistics")
            self.kind.append({"identity": 0, "quantile": 1, "mean_std": 2}[kind])
            self.lower.append(lo)
            self.upper.append(hi)
            self.mean.append(mean)
            self.std.append(std)
        self.kind = self.tensor(self.kind, torch.long)
        for name in ("lower", "upper", "mean", "std"):
            setattr(self, name, self.tensor(getattr(self, name)))
        covered, ids = [], []
        for effector in self.effectors:
            ids.append(effector["id"])
            mode = effector["mode"]
            channels = effector["motion_channels"]
            if any(type(channel) is not int or not 0 <= channel < self.action_dim for channel in channels):
                raise ValueError("Invalid action channel index")
            if mode not in ("joint", "delta", "absolute") or not channels:
                raise ValueError("Invalid effector action format")
            if mode == "delta" and (len(channels) != 6 or effector["delta_frame"] not in ("world", "body")):
                raise ValueError("Delta commands require six channels and a frame convention")
            if mode == "delta" and not 0 < effector.get("tracking", .25) <= 1:
                raise ValueError("Invalid tracking coefficient")
            if mode == "absolute" and (effector["rotation_format"] not in ("rotvec", "xyzw") or
                                      len(channels) != (7 if effector["rotation_format"] == "xyzw" else 6)):
                raise ValueError("Invalid absolute-pose layout")
            for key in ("input_min", "input_max", "output_min", "output_max"):
                if len(effector[key]) != len(channels) or not np.isfinite(effector[key]).all():
                    raise ValueError("Controller bounds do not match motion channels")
            if np.any(np.array(effector["input_max"]) <= effector["input_min"]) or np.any(np.array(effector["output_max"]) <= effector["output_min"]):
                raise ValueError("Controller ranges must be increasing")
            transform = np.asarray(effector["base_transform"])
            if transform.shape != (4, 4) or not np.isfinite(transform).all() or not np.allclose(transform[3], [0, 0, 0, 1]):
                raise ValueError("Invalid base transform")
            validate_rotation(transform[:3, :3])
            tool = np.asarray(effector.get("tool_transform", np.eye(4)))
            if tool.shape != (4, 4) or not np.isfinite(tool).all() or not np.allclose(tool[3], [0, 0, 0, 1]):
                raise ValueError("Invalid tool transform")
            validate_rotation(tool[:3, :3])
            if mode == "joint":
                if len(effector["joints"]) != len(channels):
                    raise ValueError("Joint ordering does not match action channels")
                for joint in effector["joints"]:
                    origin = np.asarray(joint["origin"])
                    axis = np.asarray(joint["axis"])
                    if origin.shape != (4, 4) or not np.isfinite(origin).all() or not np.allclose(origin[3], [0, 0, 0, 1]):
                        raise ValueError("Invalid joint origin")
                    validate_rotation(origin[:3, :3])
                    if axis.shape != (3,) or not np.isfinite(axis).all() or not np.isclose(np.linalg.norm(axis), 1):
                        raise ValueError("Joint axes must be unit vectors")
                    if joint["type"] not in ("revolute", "prismatic"):
                        raise ValueError("Unsupported joint type")
                for key in ("joint_min", "joint_max"):
                    if len(effector[key]) != len(channels) or not np.isfinite(effector[key]).all():
                        raise ValueError("Joint limits are required")
                if np.any(np.array(effector["joint_max"]) <= effector["joint_min"]):
                    raise ValueError("Invalid joint limits")
            grip = effector["gripper"]
            if not np.isfinite([grip["width_min"], grip["width_max"]]).all() or not 0 <= grip["width_min"] < grip["width_max"] or grip["mode"] not in ("binary", "continuous"):
                raise ValueError("Invalid gripper calibration")
            if grip["mode"] == "binary" and grip["closing_polarity"] not in (-1, 1):
                raise ValueError("Invalid gripper polarity")
            if grip["mode"] == "continuous" and (not np.isfinite([grip["scale"], grip["offset"]]).all() or grip["scale"] == 0):
                raise ValueError("Invalid continuous gripper transform")
            covered.extend([*channels, grip["channel"]])
        covered.extend(manifest.get("unsteered_channels", []))
        if not ids or len(set(ids)) != len(ids) or len(covered) != len(set(covered)):
            raise ValueError("Effector names and action channels must be unique")
        if sorted(covered) != torch.where(self.valid)[0].tolist():
            raise ValueError("Effector layouts must cover exactly the valid native channels")
        if "max_command_rate" in manifest:
            limits = np.asarray(manifest["max_command_rate"])
            if limits.shape != (int(self.valid.sum()),) or not np.isfinite(limits).all() or np.any(limits <= 0):
                raise ValueError("Invalid command-rate limits")

    def tensor(self, value, dtype=torch.float32):
        return torch.as_tensor(value, dtype=dtype, device=self.device)

    def physical(self, actions):
        if actions.shape[-1] != self.action_dim:
            raise ValueError("Checkpoint action width differs from manifest")
        x = actions.float()
        quantile = self.lower + (strict_clip(x, self.tensor(-1), self.tensor(1)) + 1) * (self.upper - self.lower) / 2
        physical = torch.where(self.kind == 1, quantile, torch.where(self.kind == 2, x * self.std + self.mean, x))
        return torch.where(self.valid, physical, 0)

    def normalize(self, physical):
        quantile = 2 * (physical - self.lower) / (self.upper - self.lower) - 1
        return torch.where(self.valid, torch.where(self.kind == 1, quantile,
                           torch.where(self.kind == 2, (physical - self.mean) / self.std, physical)), 0)

    def controller_motion(self, physical, effector):
        lo, hi, out_lo, out_hi = (self.tensor(effector[k]) for k in ("input_min", "input_max", "output_min", "output_max"))
        x = strict_clip(physical[..., effector["motion_channels"]], lo, hi)
        return out_lo + (x - lo) * (out_hi - out_lo) / (hi - lo)

    def joint_pose(self, q, effector):
        transform = self.tensor(effector["base_transform"]).expand(*q.shape[:-1], 4, 4)
        for i, joint in enumerate(effector["joints"]):
            axis = self.tensor(joint["axis"])
            motion = torch.eye(4, device=q.device).expand(*q.shape[:-1], 4, 4).clone()
            if joint["type"] == "revolute":
                motion[..., :3, :3] = rotation_matrix(q[..., i, None] * axis)
            else:
                motion[..., :3, 3] = q[..., i, None] * axis
            transform = transform @ self.tensor(joint["origin"]) @ motion
        tool = self.tensor(effector.get("tool_transform", np.eye(4)))
        transform = transform @ tool
        return transform[..., :3, 3], transform[..., :3, :3]

    def validate_state(self, state):
        for effector in self.effectors:
            measured = state[effector["id"]]
            if np.asarray(measured["position"]).shape != (3,) or not np.isfinite(measured["position"]).all():
                raise ValueError("Invalid measured position")
            validate_rotation(measured["rotation"])
            if np.asarray(measured["rotation"]).shape != (3, 3):
                raise ValueError("Expected one measured rotation per effector")
            grip = effector["gripper"]
            if not grip["width_min"] <= measured["width"] <= grip["width_max"]:
                raise ValueError("Measured width is outside the calibrated range")

    def target(self, raw):
        if raw["frame_id"] != self.frame:
            raise ValueError("Target frame does not match the native adapter")
        positions, rotations, widths, masks = [], [], [], []
        known = {e["id"] for e in self.effectors}
        if set(raw["effectors"]) - known:
            raise ValueError("Unknown target effector")
        for effector in self.effectors:
            entry = raw["effectors"].get(effector["id"])
            if entry is None:
                positions.append([0, 0, 0]); rotations.append(np.eye(3)); widths.append(0); masks.append([False]*3)
                continue
            position = np.asarray(entry["position"], dtype=np.float32)
            if position.shape != (3,) or not np.isfinite(position).all():
                raise ValueError("Invalid target position")
            if "quaternion_xyzw" in entry:
                q = self.tensor(entry["quaternion_xyzw"])
                if q.shape != (4,):
                    raise ValueError("Expected an xyzw quaternion")
                rotation = quaternion_matrix(q).cpu().numpy()
            else:
                rotation = np.asarray(entry["rotation"], dtype=np.float32)
                if rotation.shape != (3, 3):
                    raise ValueError("Expected a 3 by 3 target rotation")
                validate_rotation(rotation)
            gripper = float(entry["gripper"])
            if not 0 <= gripper <= 1:
                raise ValueError("Target gripper must be in [0,1]")
            mask = entry.get("components", [True, True, True])
            if len(mask) != 3 or any(type(v) is not bool for v in mask):
                raise ValueError("Expected Boolean position, rotation, gripper masks")
            grip = effector["gripper"]
            positions.append(position); rotations.append(rotation)
            widths.append(grip["width_min"] + gripper * (grip["width_max"] - grip["width_min"]))
            masks.append(mask)
        if not np.asarray(masks).any():
            raise ValueError("Target must constrain at least one pose component")
        return {"position": self.tensor(np.asarray(positions)), "rotation": self.tensor(np.asarray(rotations)),
                "width": self.tensor(widths), "mask": self.tensor(masks, torch.bool)}

    def action_mask(self, target):
        mask = torch.zeros(self.action_dim, dtype=torch.bool, device=self.device)
        for e, effector in enumerate(self.effectors):
            if bool(target["mask"][e].any()):
                mask[[*effector["motion_channels"], effector["gripper"]["channel"]]] = True
        return mask & self.valid

    def decode(self, actions, state, t=1):
        physical = self.physical(actions)
        positions, rotations, widths, seconds = [], [], [], []
        batch = actions.shape[0]
        for effector in self.effectors:
            motion = self.controller_motion(physical, effector)
            mode = effector["mode"]
            if mode == "joint":
                motion = strict_clip(motion, self.tensor(effector["joint_min"]), self.tensor(effector["joint_max"]))
                p, r = self.joint_pose(motion, effector)
            elif mode == "absolute":
                transform = self.tensor(effector["base_transform"])
                p = (transform[:3, :3] @ motion[..., :3, None]).squeeze(-1) + transform[:3, 3]
                r = quaternion_matrix(motion[..., 3:7]) if effector["rotation_format"] == "xyzw" else rotation_matrix(motion[..., 3:6])
                r = transform[:3, :3] @ r
            else:
                current = state[effector["id"]]
                pbar = self.tensor(current["position"]).expand(batch, 3)
                rbar = self.tensor(current["rotation"]).expand(batch, 3, 3)
                eta = float(effector.get("tracking", 0.25))
                ps, rs = [], []
                for h in range(actions.shape[1]):
                    dp, dr = motion[:, h, :3], motion[:, h, 3:]
                    if effector["delta_frame"] == "body":
                        dp = (rbar @ dp[..., None]).squeeze(-1)
                        rs.append(rbar @ rotation_matrix(dr))
                        rbar = rbar @ rotation_matrix(eta * dr)
                    else:
                        base_rotation = self.tensor(effector["base_transform"])[:3, :3]
                        dp = (base_rotation @ dp[..., None]).squeeze(-1)
                        dr = (base_rotation @ dr[..., None]).squeeze(-1)
                        rs.append(rotation_matrix(dr) @ rbar)
                        rbar = rotation_matrix(eta * dr) @ rbar
                    ps.append(pbar + dp)
                    pbar = pbar + eta * dp
                p, r = torch.stack(ps, 1), torch.stack(rs, 1)
            grip = effector["gripper"]
            command = physical[..., grip["channel"]]
            lo, hi = grip["width_min"], grip["width_max"]
            if grip["mode"] == "binary":
                initial = self.tensor(state[effector["id"]]["width"]).expand(batch)
                command = grip.get("command_scale", 1) * command + grip.get("command_offset", 0)
                w, w2 = binary_width_moments(command, initial, lo, hi, t, grip["closing_polarity"])
            else:
                w = strict_clip(grip["scale"] * command + grip["offset"], self.tensor(lo), self.tensor(hi))
                w2 = w.square()
            positions.append(p); rotations.append(r); widths.append(w); seconds.append(w2)
        return CommandPoses(torch.stack(positions, 2), torch.stack(rotations, 2),
                            torch.stack(widths, 2), torch.stack(seconds, 2))

    def errors(self, actions, state, target, t):
        try:
            poses = self.decode(actions, state, t)
        except ValueError as error:
            raise FloatingPointError(str(error)) from error
        if not all(bool(torch.isfinite(value).all()) for value in (poses.position, poses.rotation, poses.width, poses.width_second_moment)):
            raise FloatingPointError("Nonfinite command geometry")
        dp = (poses.position - target["position"]).square().sum(-1) / 0.05**2
        dr = (poses.rotation - target["rotation"]).square().sum((-1, -2)) / (8 * math.sin(math.pi / 12)**2)
        ranges = self.tensor([e["gripper"]["width_max"] - e["gripper"]["width_min"] for e in self.effectors])
        dw = (poses.width_second_moment - 2 * poses.width * target["width"] + target["width"].square()).clamp_min(0) / ranges.square()
        components = torch.stack((dp, dr, dw), -1)
        return torch.where(target["mask"], components, 0).sum((-1, -2))

    def execution_actions(self, actions):
        physical = self.physical(actions).clone()
        for effector in self.effectors:
            channels = effector["motion_channels"]
            physical[..., channels] = strict_clip(physical[..., channels], self.tensor(effector["input_min"]), self.tensor(effector["input_max"]))
            if effector["mode"] == "joint":
                motion = self.controller_motion(physical, effector)
                motion = strict_clip(motion, self.tensor(effector["joint_min"]), self.tensor(effector["joint_max"]))
                lo, hi, olo, ohi = (self.tensor(effector[k]) for k in ("input_min", "input_max", "output_min", "output_max"))
                physical[..., channels] = lo + (motion-olo)*(hi-lo)/(ohi-olo)
            grip = effector["gripper"]
            if grip["mode"] == "binary":
                command = grip.get("command_scale", 1)*physical[..., grip["channel"]]+grip.get("command_offset", 0)
                signed = command*grip["closing_polarity"]
                physical[..., grip["channel"]] = torch.where(signed > 0, grip.get("closed_command", grip["closing_polarity"]),
                    torch.where(signed < 0, grip.get("open_command", -grip["closing_polarity"]), grip.get("hold_command", 0)))
            else:
                width = strict_clip(grip["scale"]*physical[..., grip["channel"]]+grip["offset"], self.tensor(grip["width_min"]), self.tensor(grip["width_max"]))
                physical[..., grip["channel"]] = (width-grip["offset"])/grip["scale"]
        return physical[..., self.valid]

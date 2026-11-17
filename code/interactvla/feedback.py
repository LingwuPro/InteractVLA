import numpy as np
from scipy.spatial.transform import Rotation
import torch

from .geometry import CommandPoses, rotation_matrix


def project(points, camera, depth=None):
    points = np.asarray(points, dtype=np.float64)
    intrinsic = np.asarray(camera["intrinsics"], dtype=np.float64)
    extrinsic = np.asarray(camera["camera_from_base"], dtype=np.float64)
    homogeneous = np.concatenate((points, np.ones((*points.shape[:-1], 1))), -1)
    optical = (extrinsic @ homogeneous[..., None])[..., :3, 0]
    pixels_h = (intrinsic @ optical[..., None])[..., 0]
    front = optical[..., 2] > 0
    pixels = pixels_h[..., :2] / np.where(front, pixels_h[..., 2], 1)[..., None]
    height, width = camera["image_size"]
    inside = front & (pixels[..., 0] >= 0) & (pixels[..., 0] < width) & (pixels[..., 1] >= 0) & (pixels[..., 1] < height)
    occluded = np.zeros_like(front)
    if depth is not None:
        depth = np.asarray(depth)
        if depth.shape != (height, width):
            raise ValueError("Depth and calibration resolutions differ")
        xy = np.rint(pixels).astype(int)
        sampled = depth[np.clip(xy[..., 1], 0, height-1), np.clip(xy[..., 0], 0, width-1)]
        occluded = inside & np.isfinite(sampled) & (sampled > 0) & (optical[..., 2] > sampled+.02)
    resize = np.asarray(camera.get("display_transform", np.eye(3)), dtype=float)
    displayed = (resize @ np.concatenate((pixels, np.ones((*pixels.shape[:-1], 1))), -1)[..., None])[..., 0]
    pixels = displayed[..., :2]/displayed[..., 2, None]
    return {"pixels": pixels.tolist(), "positive_depth": front.tolist(), "in_image": inside.tolist(),
            "occluded": occluded.tolist(), "calibration_id": camera["calibration_id"]}


def backproject(pixel, depth, camera, region=None):
    depth = np.asarray(depth, dtype=float)
    if depth.shape != tuple(camera["image_size"]):
        raise ValueError("Depth and calibration resolutions differ")
    u, v = np.asarray(pixel, dtype=float)
    if not np.isfinite([u, v]).all() or not (0 <= u < depth.shape[1] and 0 <= v < depth.shape[0]):
        raise ValueError("Pixel lies outside the image")
    x, y = int(round(u)), int(round(v))
    y0, y1, x0, x1 = max(0, y-2), min(depth.shape[0], y+3), max(0, x-2), min(depth.shape[1], x+3)
    local = depth[y0:y1, x0:x1]
    valid = np.isfinite(local) & (local > 0)
    if region is not None:
        region = np.asarray(region, dtype=bool)
        if region.shape != depth.shape:
            raise ValueError("Region and depth resolutions differ")
        valid &= region[y0:y1, x0:x1]
    if valid.sum() < 5:
        raise ValueError("Insufficient valid depth support")
    ray = np.linalg.solve(np.asarray(camera["intrinsics"]), [u, v, 1])
    optical = ray * np.median(local[valid]) / ray[2]
    base = np.linalg.solve(np.asarray(camera["camera_from_base"]), np.r_[optical, 1])
    return base[:3].tolist()


def target_frame(approach, tangent):
    z = np.asarray(approach, dtype=float)
    tangent = np.asarray(tangent, dtype=float)
    if z.shape != (3,) or tangent.shape != (3,) or not np.isfinite([z, tangent]).all() or np.linalg.norm(z) < 1e-12:
        raise ValueError("Invalid approach frame")
    z /= np.linalg.norm(z)
    tangent /= max(np.linalg.norm(tangent), 1e-12)
    x = tangent - np.dot(tangent, z)*z
    if np.linalg.norm(x) < 1e-3:
        tangent = np.eye(3)[np.argmin(np.abs(z))]
        x = tangent - np.dot(tangent, z)*z
    x /= np.linalg.norm(x)
    return np.stack((x, np.cross(z, x), z), -1).tolist()


def encode_edited(geometry, original, desired, state, active_effectors):
    physical = geometry.physical(original).clone()
    for e, effector in enumerate(geometry.effectors):
        if effector["id"] not in active_effectors:
            continue
        original_motion = geometry.controller_motion(physical, effector)
        motion = original_motion.clone()
        if effector["mode"] == "joint":
            for h in range(original.shape[1]):
                q = original_motion[0, h].detach().clone()
                target_p, target_r = desired.position[0, h, e], desired.rotation[0, h, e]
                for _ in range(30):
                    q = q.detach().requires_grad_(True)
                    def residual(value):
                        p, r = geometry.joint_pose(value, effector)
                        return torch.cat((p-target_p, (r-target_r).reshape(-1)))
                    error = residual(q)
                    jacobian = torch.autograd.functional.jacobian(residual, q)
                    update = -jacobian.T @ torch.linalg.solve(jacobian @ jacobian.T + 1e-3*torch.eye(12, device=q.device), error)
                    q = (q + update.clamp(-.05, .05)).detach()
                    q = torch.maximum(torch.minimum(q, geometry.tensor(effector["joint_max"])), geometry.tensor(effector["joint_min"]))
                    p, r = geometry.joint_pose(q, effector)
                    angle = torch.acos(((torch.trace(r.T@target_r)-1)/2).clamp(-1, 1))
                    if float((p-target_p).norm()) < .002 and float(angle) < np.deg2rad(2):
                        break
                else:
                    raise ValueError("Inverse kinematics did not converge")
                motion[0, h] = q
        elif effector["mode"] == "absolute":
            transform = geometry.tensor(effector["base_transform"])
            motion[..., :3] = (transform[:3, :3].T @ (desired.position[..., e, :]-transform[:3, 3])[..., None])[..., 0]
            rotations = (transform[:3, :3].T @ desired.rotation[..., e, :, :]).cpu().numpy()
            converted = Rotation.from_matrix(rotations.reshape(-1, 3, 3))
            orientation = converted.as_quat() if effector["rotation_format"] == "xyzw" else converted.as_rotvec()
            motion[..., 3:] = geometry.tensor(orientation.reshape(*motion.shape[:2], -1))
        else:
            pbar = geometry.tensor(state[effector["id"]]["position"])
            rbar = geometry.tensor(state[effector["id"]]["rotation"])
            eta = float(effector.get("tracking", .25))
            for h in range(original.shape[1]):
                dp = desired.position[0, h, e]-pbar
                target_r = desired.rotation[0, h, e]
                body = effector["delta_frame"] == "body"
                dr_matrix = rbar.T@target_r if body else target_r@rbar.T
                dr = geometry.tensor(Rotation.from_matrix(dr_matrix.cpu().numpy()).as_rotvec())
                base_rotation = geometry.tensor(effector["base_transform"])[:3, :3]
                motion[0, h, :3] = rbar.T@dp if body else base_rotation.T@dp
                motion[0, h, 3:] = dr if body else base_rotation.T@dr
                pbar = pbar+eta*dp
                rbar = rbar@rotation_matrix(eta*dr) if body else rotation_matrix(eta*dr)@rbar
        lo, hi, olo, ohi = (geometry.tensor(effector[k]) for k in ("input_min", "input_max", "output_min", "output_max"))
        if bool(((motion < olo-1e-6) | (motion > ohi+1e-6)).any()):
            raise ValueError("Edited commands exceed controller bounds")
        physical[..., effector["motion_channels"]] = lo+(motion-olo)*(hi-lo)/(ohi-olo)
    edited = geometry.normalize(physical)
    for effector in geometry.effectors:
        if effector["id"] not in active_effectors:
            channels = [*effector["motion_channels"], effector["gripper"]["channel"]]
            edited[..., channels] = original[..., channels]
    edited[..., ~geometry.valid] = original[..., ~geometry.valid]
    return edited.detach()


def bounded_edit(geometry, actions, state, first, last, translation, rotation, active_effectors):
    if not 0 <= first < last < actions.shape[1] or last-first+1 < 3:
        raise ValueError("Pose edits need at least three unexecuted commands")
    dp, dr = np.asarray(translation, dtype=float), np.asarray(rotation, dtype=float)
    if dp.shape != (3,) or dr.shape != (3,) or not np.isfinite([dp, dr]).all():
        raise ValueError("Invalid edit offsets")
    if np.linalg.norm(dp) > .02+1e-10 or np.linalg.norm(dr) > np.deg2rad(10)+1e-10:
        raise ValueError("Edit exceeds the displacement envelope")
    ids = {e["id"] for e in geometry.effectors}
    if not active_effectors or not set(active_effectors) <= ids:
        raise ValueError("Invalid edit effector mask")
    poses = geometry.decode(actions, state)
    p, r = poses.position.clone(), poses.rotation.clone()
    s = torch.linspace(0, 1, last-first+1, device=actions.device)
    bump = 16*s.square()*(1-s).square()
    for e, effector in enumerate(geometry.effectors):
        if effector["id"] in active_effectors:
            p[:, first:last+1, e] += bump[:, None]*geometry.tensor(dp)
            r[:, first:last+1, e] = rotation_matrix(bump[:, None]*geometry.tensor(dr)) @ r[:, first:last+1, e]
    desired = CommandPoses(p, r, poses.width, poses.width_second_moment)
    edited = encode_edited(geometry, actions, desired, state, active_effectors)
    rate_limits = geometry.manifest.get("max_command_rate")
    if rate_limits is not None:
        commands = geometry.execution_actions(edited)
        if bool((commands.diff(dim=1).abs()*geometry.rate > geometry.tensor(rate_limits)).any()):
            raise ValueError("Edited commands exceed command-rate limits")
    return edited


def shift_gripper_transition(actions, channel, index, shift):
    if type(index) is not int or type(shift) is not int or abs(shift) > 2 or not 0 < index < actions.shape[1]:
        raise ValueError("Invalid gripper-transition edit")
    values = actions[0, :, channel]
    before, after = values[index-1], values[index]
    if bool(before == after):
        raise ValueError("There is no gripper transition at the requested index")
    start, stop = index-1, index+1
    while start > 0 and bool(values[start-1] == before):
        start -= 1
    while stop < len(values) and bool(values[stop] == after):
        stop += 1
    new_index = index+shift
    if new_index-start < 2 or stop-new_index < 2:
        raise ValueError("A gripper-transition edit must retain two commands in each state")
    result = actions.clone()
    result[:, start:new_index, channel] = before
    result[:, new_index:stop, channel] = after
    return result


def motion_feedback(poses, camera, executed_count, prefix_length, matched_index=None, target=None):
    p = poses.position[0].detach().cpu().numpy()
    r = poses.rotation[0].detach().cpu().numpy()
    width = poses.width[0].detach().cpu().numpy()
    count = len(p)
    indices = set(np.linspace(0, count-1, min(12, count)).round().astype(int).tolist())
    indices.add(min(prefix_length, count)-1)
    if matched_index is not None and 0 <= matched_index < count:
        indices.add(matched_index)
    indices = sorted(indices)
    origins = p[indices]
    axes = origins[..., None, :] + .025*np.swapaxes(r[indices], -1, -2)
    jaws = origins[..., None, :] + np.array([-1, 1])[None, None, :, None]*width[indices, ..., None, None]/2*r[indices, ..., :, 1][..., None, :]
    result = {"trajectory": project(p, camera, camera.get("depth")), "glyph_indices": indices,
              "orientation_axes": project(axes, camera, camera.get("depth")),
              "gripper_glyphs": project(jaws, camera, camera.get("depth")),
              "executed": (np.arange(count) < executed_count).tolist(),
              "prefix_boundary": min(prefix_length, count)-1, "arrival_index": matched_index}
    if target is not None:
        positions = [entry["position"] for entry in target["effectors"].values()]
        result["target"] = project(positions, camera, camera.get("depth"))
    return result

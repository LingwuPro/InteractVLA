import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage
from scipy.spatial.transform import Rotation


# Input: calibrated depth [H,W]; output: visible world points [H,W,3].
def world_points(camera: dict[str, Any]) -> np.ndarray:
    depth = np.load(camera['depth'])
    rows, columns = np.indices(depth.shape)
    pixels = np.stack([columns, rows, np.ones_like(rows)], axis=-1)
    intrinsic = np.asarray(camera['intrinsic'])
    transform = np.asarray(camera['world_from_camera'])
    optical = (pixels @ np.linalg.inv(intrinsic).T) * depth[..., None]
    return optical @ transform[:3, :3].T + transform[:3, 3]


# Input: visible points [H,W,3], selected plane pixels [N,2] and ROI [4]; output: measured plane/circle geometry.
def measure(points: np.ndarray, pixels: np.ndarray, seed: list[int], roi: list[int], tolerance: float) -> dict[str, Any]:
    samples = points[pixels[:, 0], pixels[:, 1]]
    origin = samples.mean(axis=0)
    _, singular, vectors = np.linalg.svd(samples - origin)
    assert len(samples) >= 3 and singular[1] > 1e-5, 'Select separated points on the visible planar cap'
    normal = vectors[-1]
    normal *= 1 if normal[2] >= 0 else -1
    axis_u = vectors[0]
    axis_v = np.cross(normal, axis_u)
    row_min, row_max, col_min, col_max = roi
    rows, columns = np.indices(points.shape[:2])
    mask = np.abs((points - origin) @ normal) < tolerance
    mask &= (rows >= row_min) & (rows < row_max) & (columns >= col_min) & (columns < col_max)
    labels, _ = ndimage.label(mask)
    label = int(labels[seed[0], seed[1]])
    assert label != 0, 'The selected component seed is outside the visible plane'
    component = labels == label
    boundary = component & ~ndimage.binary_erosion(component)
    boundary &= (rows > row_min + 1) & (rows < row_max - 2)
    boundary &= (columns > col_min + 1) & (columns < col_max - 2)
    edge = points[boundary] - origin
    assert len(edge) >= 20, 'Not enough uncut visible circle boundary'
    planar = np.column_stack([edge @ axis_u, edge @ axis_v])
    matrix = np.column_stack([2 * planar, np.ones(len(planar))])
    cx, cy, offset = np.linalg.lstsq(matrix, np.square(planar).sum(axis=1), rcond=None)[0]
    radius = float(np.sqrt(offset + cx * cx + cy * cy))
    center = origin + cx * axis_u + cy * axis_v
    residual = np.linalg.norm(planar - [cx, cy], axis=1) - radius
    return {'visible_plane_points_world': samples.tolist(), 'plane_normal_world': normal.tolist(),
            'tilt_from_up_degrees': float(np.degrees(np.arccos(np.clip(normal[2], -1, 1)))),
            'circle_center_world': center.tolist(), 'radius_m': radius,
            'circle_boundary_rms_m': float(np.sqrt(np.mean(residual**2))),
            'boundary_pixels': int(len(edge)), 'component_pixels': int(component.sum())}


# Input: explicit operator-selected pixels and ROI; output: auditable geometry from current RGB-D only, with no robot execution.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('observation', type=Path)
    parser.add_argument('--camera', default='robot0_eye_in_hand')
    parser.add_argument('--pixels', required=True)
    parser.add_argument('--seed', required=True)
    parser.add_argument('--roi', required=True)
    parser.add_argument('--tolerance', type=float, default=0.0006)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    observation = json.loads(args.observation.read_text())
    pixels = np.asarray(json.loads(args.pixels), dtype=int)
    seed, roi = json.loads(args.seed), json.loads(args.roi)
    geometry = measure(world_points(observation['cameras'][args.camera]), pixels, seed, roi, args.tolerance)
    normal = geometry['plane_normal_world']
    rotation = Rotation.from_matrix(observation['robot']['rotation_world'])
    correction, _ = Rotation.align_vectors([[0, 0, 1]], [normal])
    record = {'observation': str(args.observation), 'camera': args.camera,
              'selected_plane_pixels_row_column': pixels.tolist(), 'component_seed_row_column': seed,
              'roi_row_min_max_column_min_max': roi, 'plane_tolerance_m': args.tolerance, **geometry,
              'leveling_gripper_quaternion_xyzw': (correction * rotation).as_quat().tolist(),
              'measured_gripper_position_world': observation['robot']['position_world'],
              'limitation': 'Operator-selected visible planar circular patch; fit residual is not semantic confidence or task success.'}
    args.output.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()

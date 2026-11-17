import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from control import unproject


# Input: RGB-D observation and selected pixels [N,2]; output: camera-facing plane fit from visible points [N,3].
def fit_plane(observation: Path, camera: str, pixels: list[list[int]]) -> dict[str, Any]:
    samples = unproject(observation, camera, pixels)
    points = np.asarray(samples['points_world'])
    assert len(points) >= 3, 'Select at least three visible non-collinear surface points'
    center = points.mean(axis=0)
    _, singular, axes = np.linalg.svd(points - center, full_matrices=False)
    assert singular[1] > 0.001, 'Selected surface samples are too close or collinear'
    normal = axes[-1]
    record = json.loads(observation.read_text())
    transform = np.asarray(record['cameras'][camera]['world_from_camera'])
    if np.dot(normal, transform[:3, 3] - center) < 0:
        normal = -normal
    residual = (points - center) @ normal
    return {**samples, 'center_world': center.tolist(), 'normal_world': normal.tolist(),
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'projection_tool_sha256': hashlib.sha256(Path(__file__).with_name('control.py').read_bytes()).hexdigest(),
            'normal_orientation': 'toward the observing camera',
            'plane_offset_m': float(-normal @ center),
            'rms_residual_m': float(np.sqrt(np.mean(residual**2))),
            'singular_values_m': singular.tolist(),
            'semantics': 'Operator-selected visible surface plane; no object identity or task success inference'}


# Input: saved observation, camera, explicit pixels and optional output path; output: measured plane JSON, with zero actions.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('observation', type=Path)
    parser.add_argument('camera')
    parser.add_argument('pixels')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = fit_plane(args.observation, args.camera, json.loads(args.pixels))
    content = json.dumps(result, indent=2) + '\n'
    if args.output is not None:
        with args.output.open('x') as handle:
            handle.write(content)
        args.output.chmod(0o644)
    print(content)


if __name__ == '__main__':
    main()

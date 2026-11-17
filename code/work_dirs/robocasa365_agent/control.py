import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from attempt_budget import ensure_next_attempt


# Input: saved RGB-D observation and pixel samples [N,2]; output: observed world surface points [N,3].
def unproject(observation: Path, camera: str, pixels: list[list[int]]) -> dict[str, Any]:
    record = json.loads(observation.read_text())
    spec = record['cameras'][camera]
    depth = np.load(spec['depth']).squeeze()
    intrinsic = np.asarray(spec['intrinsic'])
    extrinsic = np.asarray(spec['world_from_camera'])
    points = []
    for row, column in pixels:
        assert 0 <= row < depth.shape[0] and 0 <= column < depth.shape[1]
        z = float(depth[row, column])
        assert np.isfinite(z) and z > 0
        optical = np.linalg.solve(intrinsic, [column, row, 1]) * z
        points.append(extrinsic[:3, :3] @ optical + extrinsic[:3, 3])
    return {'points_world': np.asarray(points).tolist(), 'median_world': np.median(points, axis=0).tolist(),
            'pixels_row_column': pixels, 'camera': camera, 'observation': str(observation),
            'semantics': 'visible surface samples; not object centers or verified grasp poses'}


# Input: live case directory and one tool command; output: atomically submitted next numbered request.
def submit(folder: Path, request: dict[str, Any]) -> Path:
    if request['type'] == 'reset':
        ensure_next_attempt(folder)
    if request['type'] == 'base':
        motion = np.asarray(request['motion'], dtype=float)
        assert motion.shape == (4,) and np.isfinite(motion).all(), 'Base motion needs four finite components'
        assert np.all(np.abs(motion) <= [1.0, 1.0, 0.5, 0.0]), 'Base motion exceeds native bounds'
        assert 1 <= request['steps'] <= 16 and 0 <= request['gripper_close'] <= 1
    commands = sorted(folder.glob('command-*.json'))
    number = len(commands) + 1
    if commands:
        assert (folder / f'response-{number - 1:05d}.json').exists(), 'Previous command has no response; do not resend'
    assert not (folder / 'result.json').exists(), 'Case is terminal'
    if request['type'] == 'servo':
        if commands:
            for command in reversed(commands):
                response = command.with_name(command.name.replace('command-', 'response-'))
                observation = json.loads(response.read_text())
                if 'robot' in observation:
                    break
            else:
                raise AssertionError('No completed robot observation is available')
        else:
            observation = json.loads((folder / 'observation.json').read_text())
        assert not observation['terminal'], 'Attempt is terminal'
        position = np.asarray(request['position_world'], dtype=float)
        assert position.shape == (3,) and np.isfinite(position).all(), 'Servo position needs three finite components'
        distance = np.linalg.norm(position - observation['robot']['position_world'])
        assert distance <= 0.3, f'Servo target exceeds 0.3 m from the latest observation: {distance}'
    target = folder / f'command-{number:05d}.json'
    tmp = target.with_suffix('.tmp')
    with tmp.open('x') as handle:
        handle.write(json.dumps(request, indent=2) + '\n')
    tmp.chmod(0o644)
    tmp.replace(target)
    return target


# Input: explicit request or RGB-D pixels; output: command path or observed world coordinates.
def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='operation', required=True)
    command = sub.add_parser('submit')
    command.add_argument('folder', type=Path)
    command.add_argument('request', help='JSON tool request')
    project = sub.add_parser('unproject')
    project.add_argument('observation', type=Path)
    project.add_argument('camera')
    project.add_argument('pixels', help='JSON [[row,column], ...]')
    args = parser.parse_args()
    if args.operation == 'submit':
        print(submit(args.folder, json.loads(args.request)))
    else:
        print(json.dumps(unproject(args.observation, args.camera, json.loads(args.pixels))))


if __name__ == '__main__':
    main()

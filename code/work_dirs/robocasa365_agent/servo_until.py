import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from operator_step import step
from scipy.spatial.transform import Rotation


# Input: measured robot pose and explicit full-pose request; output: position error in metres and rotation error in radians.
def errors(record: dict[str, Any], request: dict[str, Any]) -> tuple[float, float]:
    robot = record['robot']
    position = float(np.linalg.norm(np.asarray(request['position_world']) - robot['position_world']))
    desired = Rotation.from_quat(request['quaternion_xyzw']).as_matrix()
    rotation = float(Rotation.from_matrix(desired @ np.asarray(robot['rotation_world']).T).magnitude())
    return position, rotation


# Input: one explicit servo target and at most three segments; output: actual trace, stopping on success, terminal state or stalled measured progress.
def move(folder: Path, request: dict[str, Any], segments: int) -> dict[str, Any]:
    assert request['type'] == 'servo' and 1 <= segments <= 3
    assert 1 <= request['steps'] <= 16
    assert not (folder / 'result.json').exists(), 'Case is terminal'
    latest = sorted(folder.glob('response-*.json'))[-1]
    record = json.loads(latest.read_text())
    assert 'robot' in record and not record['terminal'], 'Observe the live attempt before moving'
    source = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    start_attempt, start_steps = record['attempt'], record['steps']
    trace = []
    stop = 'segment_limit'
    for index in range(segments):
        before_position, before_rotation = errors(record, request)
        if before_position > 0.3:
            stop = 'target_too_far'
            break
        command = {**request, 'reason': f"{request['reason']} | bounded servo {source[:12]}, segment {index + 1}/{segments}"}
        response = step(folder, command)
        record = json.loads(Path(response['response']).read_text())
        trace.append(response)
        if 'rejected' in record:
            stop = 'rejected'
            break
        assert record['attempt'] == start_attempt
        if record['success'] or record['terminal']:
            stop = 'native_success' if record['success'] else 'native_terminal'
            break
        position, rotation = errors(record, request)
        if position <= 0.008 and rotation <= 0.06:
            stop = 'pose_reached'
            break
        if before_position - position < 0.003 and before_rotation - rotation < 0.03:
            stop = 'measured_progress_stalled'
            break
    return {'case': str(folder), 'attempt': start_attempt, 'start_steps': start_steps,
            'stop_reason': stop, 'source_sha256': source, 'requested_target': request, 'trace': trace,
            'task_success': bool(record['success']) if 'success' in record else False,
            'semantics': 'Explicit operator target only; no target generation, reset or success inference'}


# Input: live case, explicit servo request and segment bound; output: saved actual execution report and concise JSON.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('request')
    parser.add_argument('--segments', type=int, default=3)
    args = parser.parse_args()
    result = move(args.folder, json.loads(args.request), args.segments)
    output = args.folder / f"attempt-{result['attempt']:02d}" / f"bounded-servo-{result['start_steps']:06d}-{time.time_ns()}.json"
    with output.open('x') as handle:
        handle.write(json.dumps(result, indent=2) + '\n')
    output.chmod(0o644)
    print(json.dumps({**result, 'report': str(output)}), flush=True)


if __name__ == '__main__':
    main()

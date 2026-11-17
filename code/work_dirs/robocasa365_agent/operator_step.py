import argparse
import json
import time
from pathlib import Path
from typing import Any

from control import submit


# Input: case directory and one operator request; output: actual response after execution, without planning extra actions.
def step(folder: Path, request: dict[str, Any]) -> dict[str, Any]:
    command = submit(folder, request)
    response = command.with_name(command.name.replace('command-', 'response-'))
    while not response.exists():
        time.sleep(0.25)
    record = json.loads(response.read_text())
    result = {'response': str(response), **{key: record[key] for key in
              ('attempt', 'steps', 'success', 'terminal', 'rejected') if key in record}}
    if 'robot' in record:
        result['position'] = record['robot']['position_world']
        result['gripper'] = record['robot']['gripper_qpos']
        result['base_position'] = record['robot']['base_position']
        result['base_rotation'] = record['robot']['base_rotation']
        if 'arm_joint_state' in record['robot']:
            result['arm_joint_state'] = record['robot']['arm_joint_state']
        if 'base_controller_state' in record['robot']:
            result['base_controller_state'] = record['robot']['base_controller_state']
        result['images'] = {name: spec['rgb'] for name, spec in record['cameras'].items()}
    return result


# Input: explicit operator command as JSON; output: concise measured state and original saved image paths.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('request')
    args = parser.parse_args()
    print(json.dumps(step(args.folder, json.loads(args.request))), flush=True)


if __name__ == '__main__':
    main()

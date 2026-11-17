import argparse
import json
from pathlib import Path

import numpy as np
from deployment.model_server.tools.websocket_policy_client import WebsocketClientPolicy
from examples.RoboCasa365.eval.native12_policy import Native12Codec
from scipy.spatial.transform import Rotation


# Input: saved sensor-only initial observation and one endpoint; output: numerical inference audit, zero robot controls.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--port', type=int, default=11393)
    args = parser.parse_args()
    initial = json.loads((args.folder / 'attempt-01/initial.json').read_text())
    record = json.loads((args.folder / 'observation.json').read_text())
    robot = record['robot']
    data = dict(np.load(args.folder / 'attempt-01/initial-policy-observation.npz', allow_pickle=False))
    data['annotation.human.task_description'] = str(data['annotation.human.task_description'])
    codec = Native12Codec.from_checkpoint(initial['checkpoint'])
    origin = np.array(robot['controller_origin_position'])
    rotation = np.array(robot['controller_origin_rotation'])
    position = (np.array(robot['position_world']) - origin) @ rotation
    orientation = rotation.T @ np.array(robot['rotation_world'])
    geometry = {'mode': 'soft_time_track_pose025', 'current_position': [position],
                'current_rotation': [orientation], 'current_gripper_command': [robot['gripper_command'][0]],
                'gripper_width_scale': 0.04, 'target_position': [position], 'target_rotation': [orientation],
                'target_width': [0.04], **{key: robot[key] for key in ('input_min', 'input_max', 'output_min', 'output_max')}}
    client = WebsocketClientPolicy(port=args.port)
    payload = {'examples': [codec.build_model_example(data)], 'seed': 123, 'scale': 30, 'verify': True, 'geometry': geometry}
    result = client.predict_action(payload)
    client.close()
    if result['ok']:
        print(json.dumps(result['data']['audit'], indent=2))
    else:
        print(json.dumps(result, indent=2))
    assert result['ok'], result
    assert Rotation.from_matrix(robot['rotation_world']).as_quat().shape == (4,)


if __name__ == '__main__':
    main()

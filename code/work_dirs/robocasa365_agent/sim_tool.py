import argparse
import gc
import hashlib
import json
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
import robocasa
import robosuite.utils.camera_utils as camera_utils
from deployment.model_server.tools.websocket_policy_client import WebsocketClientPolicy
from examples.RoboCasa365.eval.native12_policy import (
    ACTION_KEYS,
    STATE_KEYS,
    VIDEO_KEYS,
    Native12Codec,
)
from PIL import Image
from reset_state import SOURCE_SHA256 as RESET_SOURCE_SHA256
from reset_state import restore_initial, save_initial
from robocasa.utils.dataset_registry_utils import get_task_horizon
from scipy.spatial.transform import Rotation

RUNTIME_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CAMERAS = ('robot0_agentview_left', 'robot0_agentview_right', 'robot0_eye_in_hand')



def serial(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value



def write_json(path: Path, record: Any) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(serial(record), indent=2) + '\n')
    tmp.replace(path)


class Session:

    def __init__(self, args: argparse.Namespace) -> None:
        gc.disable()
        self.args = args
        self.root = args.output.resolve()
        if args.resume:
            assert self.root.is_dir()
        else:
            self.root.mkdir(parents=True, exist_ok=False)
        self.codec = Native12Codec.from_checkpoint(args.checkpoint)
        self.client = WebsocketClientPolicy(host='127.0.0.1', port=args.port)
        self.env: Any = None
        self.attempt = 0
        self.initial_hash: str | None = None
        self.initial_scene_hash: str | None = None
        self.steps = 0
        self.success = False
        self.terminal = False
        self.command_id = 0
        self.observation_suffix = ''
        self.chunk = 0
        self.max_steps = get_task_horizon(args.task)
        if args.resume:
            assert not (self.root / 'result.json').exists()
            attempts = sorted(self.root.glob('attempt-*'))
            assert attempts
            self.command_id = len(list(self.root.glob('command-*.json')))
            for index, folder in enumerate(attempts, 1):
                result = json.loads((folder / 'result.json').read_text())
                assert result['task'] == args.task and result['seed'] == args.seed
                assert result['attempt'] == index and not result['success']
            self.attempt = len(attempts)
            self.initial_hash = result['initial_state_sha256']
            self.initial_scene_hash = json.loads((attempts[0] / 'initial.json').read_text())['scene_structure_sha256']
        self.reset()


    def reset(self) -> dict[str, Any]:
        assert not self.success and self.attempt < 6, 'Success or six-attempt limit reached'
        if self.env is not None:
            assert self.terminal, 'Finish the current attempt before reset'
            self.env.close()
        self.attempt += 1
        self.folder = self.root / f'attempt-{self.attempt:02d}'
        self.folder.mkdir(exist_ok=False)
        random.seed(self.args.seed)
        np.random.seed(self.args.seed)
        self.env = gym.make(f'robocasa/{self.args.task}', split='pretrain', seed=self.args.seed, enable_render=True)
        self.native = self.env.unwrapped.env
        snapshot = Path(getattr(self.args, 'reset_snapshot', self.root / 'execution-reset'))
        self.obs, _ = self.env.reset(seed=self.args.seed)
        if snapshot.exists():
            restore_initial(self.native, snapshot)
            self.obs = self.env.unwrapped.get_observation(self.native._get_observations(force_update=True))
        self.arm = self.native.robots[0].composite_controller.part_controllers['right']
        assert self.arm.input_type == 'delta' and self.arm.input_ref_frame == 'base'
        assert self.arm.impedance_mode == 'fixed'
        digest = hashlib.sha256(self.native.sim.get_state().flatten().tobytes()).hexdigest()
        scene_hasher = hashlib.sha256()
        for field in ('body_pos', 'body_quat', 'body_mass', 'geom_pos', 'geom_quat', 'geom_size', 'geom_type', 'mesh_vert', 'mesh_face'):
            scene_hasher.update(np.ascontiguousarray(getattr(self.native.sim.model, field)).tobytes())
        scene_digest = scene_hasher.hexdigest()
        if self.initial_hash is None:
            self.initial_hash = digest
            self.initial_scene_hash = scene_digest
        assert scene_digest == self.initial_scene_hash, 'Retry changed the scene model'
        assert digest == self.initial_hash, 'Retry changed the initial simulator state'
        self.steps, self.chunk = 0, 0
        self.success, self.terminal = False, False
        self.language = self.obs['annotation.human.task_description']
        write_json(self.folder / 'initial.json', {'task': self.args.task, 'seed': self.args.seed,
                   'attempt': self.attempt, 'state_sha256': digest, 'scene_structure_sha256': scene_digest, 'task_language': self.language,
                   'budget': self.max_steps, 'checkpoint': self.args.checkpoint,
                   'model_metadata': self.client.get_server_metadata(),
                   'runtime_source_sha256': RUNTIME_SOURCE_SHA256,
                   'reset_source_sha256': RESET_SOURCE_SHA256,
                   'execution_reset_scene': getattr(self.native, '_execution_reset_scene', {'pose_source': 'fresh_native'}),
                   'python_hash_seed': os.environ.get('PYTHONHASHSEED'), 'automatic_gc_disabled': not gc.isenabled(),
                   'implementation_sha256': hashlib.sha256(Path(__file__).with_name('implementation.json').read_bytes()).hexdigest(),
                   'skill_sha256': hashlib.sha256(Path(self.args.skill).read_bytes()).hexdigest()})
        np.savez_compressed(self.folder / 'initial-policy-observation.npz',
                            **{key: self.obs[key] for key in (*STATE_KEYS, *VIDEO_KEYS, 'annotation.human.task_description')})
        observation = self.observe()
        if not snapshot.exists():
            save_initial(self.native, snapshot)
        return observation


    def robot_state(self) -> dict[str, Any]:
        self.arm.update(force=True)
        robot = self.native.robots[0]
        base = robot.composite_controller.part_controllers['base']
        actuators = robot._ref_actuators_indexes_dict['base']
        return {'position_world': self.arm.ref_pos.copy(), 'rotation_world': self.arm.ref_ori_mat.copy(),
                'controller_origin_position': self.arm.origin_pos.copy(),
                'controller_origin_rotation': self.arm.origin_ori.copy(),
                'gripper_qpos': self.obs['state.gripper_qpos'],
                'gripper_command': self.native.robots[0].gripper['right'].current_action.copy(),
                'base_position': self.obs['state.base_position'], 'base_rotation': self.obs['state.base_rotation'],
                'arm_joint_state': {'controller': type(self.arm).__name__,
                    'names': self.arm.joint_names,
                    'position': self.arm.joint_pos.copy(),
                    'velocity': self.native.sim.data.qvel[self.arm.qvel_index].copy(),
                    'range': self.native.sim.model.jnt_range[self.arm.joint_index].copy(),
                    'limited': self.native.sim.model.jnt_limited[self.arm.joint_index].copy()},
                'base_controller_state': {'controller': type(base).__name__,
                    'joint_velocity': self.native.sim.data.qvel[base.qvel_index].copy(),
                    'goal_joint_velocity': base.goal_qvel,
                    'actuator_control': self.native.sim.data.ctrl[actuators].copy(),
                    'actuator_force': self.native.sim.data.actuator_force[actuators].copy(),
                    'joint_frictionloss': self.native.sim.model.dof_frictionloss[base.qvel_index].copy(),
                    'input_min': base.input_min.copy(), 'input_max': base.input_max.copy(),
                    'actuator_min': base.actuator_min.copy(), 'actuator_max': base.actuator_max.copy()},
                'input_min': self.arm.input_min.copy(), 'input_max': self.arm.input_max.copy(),
                'output_min': self.arm.output_min.copy(), 'output_max': self.arm.output_max.copy()}


    def observe(self) -> dict[str, Any]:
        folder = self.folder / f'observation-{self.steps:06d}-{self.command_id:05d}{self.observation_suffix}'
        folder.mkdir(exist_ok=True)
        assert all(np.std(self.obs[key]) > 1 for key in VIDEO_KEYS), 'Invalid policy RGB: blank camera image'
        cameras = {}
        for camera in CAMERAS:
            rgb, depth = self.native.sim.render(width=256, height=256, camera_name=camera, depth=True)
            depth_range = [float(np.min(depth)), float(np.max(depth))]
            tolerance = 8 * np.finfo(depth.dtype).eps
            valid = np.isfinite(depth).all() and depth_range[0] >= -tolerance and depth_range[1] <= 1 + tolerance
            if not valid:
                np.save(folder / f'{camera}-invalid-raw-depth.npy', depth)
                write_json(folder / 'sensor-error.json', {'camera': camera, 'raw_depth_range': depth_range, 'roundoff_tolerance': tolerance})
                raise ValueError(f'Invalid depth sensor: {camera} range={depth_range}')
            depth = camera_utils.get_real_depth_map(self.native.sim, np.clip(depth, 0, 1))
            rgb, depth = np.ascontiguousarray(rgb[::-1]), np.ascontiguousarray(depth[::-1])
            assert rgb.std() > 1 and np.isfinite(depth).all() and np.all(depth > 0)
            intrinsic = camera_utils.get_camera_intrinsic_matrix(self.native.sim, camera, 256, 256)
            extrinsic = camera_utils.get_camera_extrinsic_matrix(self.native.sim, camera)
            Image.fromarray(rgb).save(folder / f'{camera}.png')
            np.save(folder / f'{camera}-depth.npy', depth)
            cameras[camera] = {'rgb': str(folder / f'{camera}.png'), 'depth': str(folder / f'{camera}-depth.npy'),
                               'intrinsic': intrinsic, 'world_from_camera': extrinsic, 'raw_depth_range': depth_range,
                               'roundoff_clipped': depth_range[0] < 0 or depth_range[1] > 1}
        result = {'task': self.args.task, 'seed': self.args.seed, 'attempt': self.attempt,
                  'steps': self.steps, 'budget': self.max_steps, 'success': self.success,
                  'terminal': self.terminal, 'task_language': self.language, 'robot': self.robot_state(),
                  'cameras': cameras, 'image_coordinates': 'OpenCV top-left row,column; metric optical-z depth',
                  'depth_noise': 'ideal_simulated_depth_without_sensor_noise'}
        write_json(folder / 'observation.json', result)
        write_json(self.root / 'observation.json', result)
        return result


    def execute(self, actions: dict[str, np.ndarray], mode: str) -> int:
        assert not self.terminal
        executed, state_records, action_records = 0, [], []
        for index in range(len(actions[ACTION_KEYS[0]])):
            if self.terminal:
                break
            action = {k: np.asarray(actions[k][index], dtype=np.float32) for k in ACTION_KEYS}
            assert all(np.isfinite(v).all() for v in action.values())
            self.obs, _, done, truncated, info = self.env.step(action)
            self.steps += 1
            executed += 1
            self.success = bool(info['success'])
            self.terminal = self.success or bool(done) or bool(truncated) or self.steps >= self.max_steps
            action_records.append(np.concatenate([action[k] for k in ACTION_KEYS]))
            state_records.append(self.obs['state.end_effector_position_relative'].copy())
        np.savez_compressed(self.folder / f'actions-{self.command_id:05d}-{self.chunk:05d}-{self.steps:06d}.npz',
                            actions=action_records, measured_relative_position=state_records)
        if self.terminal:
            self.finish(mode)
        return executed


    def finish(self, reason: str) -> dict[str, Any]:
        self.terminal = True
        result = {'task': self.args.task, 'seed': self.args.seed, 'attempt': self.attempt,
                  'success': self.success, 'steps': self.steps, 'budget': self.max_steps,
                  'reason': reason, 'initial_state_sha256': self.initial_hash, 'token_usage': None}
        path = self.folder / 'result.json'
        if not path.exists():
            write_json(path, result)
        if self.success or self.attempt == 6:
            write_json(self.root / 'result.json', {**result, 'attempts_used': self.attempt})
        return result


    def geometry(self, goal: dict[str, Any]) -> dict[str, Any]:
        state = self.robot_state()
        origin = state['controller_origin_position']
        rotation = state['controller_origin_rotation']
        return {'mode': 'soft_time_track_pose025',
                'current_position': [(state['position_world'] - origin) @ rotation],
                'current_rotation': [rotation.T @ state['rotation_world']],
                'current_gripper_command': [state['gripper_command'][0]], 'gripper_width_scale': 0.04,
                'target_position': [(np.asarray(goal['position_world']) - origin) @ rotation],
                'target_rotation': [rotation.T @ Rotation.from_quat(goal['quaternion_xyzw']).as_matrix()],
                'target_width': [goal['width_m']],
                **{k: state[k] for k in ('input_min', 'input_max', 'output_min', 'output_max')}}


    def vla(self, chunks: int, goal: dict[str, Any] | None = None, scale: float = 0,
            verify: bool = False) -> dict[str, Any]:
        assert 1 <= chunks <= 256
        if goal is not None:
            assert chunks == 1 and scale > 0, 'Reobserve before another guided chunk'
            assert 0 <= goal['width_m'] <= 0.08
            if np.linalg.norm(np.asarray(goal['position_world']) - self.robot_state()['position_world']) > 0.25:
                return {'rejected': 'goal_too_far', 'steps': self.steps}
        for _ in range(chunks):
            if self.terminal:
                break
            seed = int.from_bytes(hashlib.sha256(f'{self.args.task}:{self.args.seed}:{self.attempt}:{self.chunk}'.encode()).digest()[:4], 'little')
            payload: dict[str, Any] = {'examples': [self.codec.build_model_example(self.obs)], 'seed': seed,
                                      'scale': scale, 'verify': verify}
            if goal is not None:
                payload['geometry'] = self.geometry(goal)
            response = self.client.predict_action(payload)
            assert response['ok'], response
            if response['data'].get('status', 'accepted') != 'accepted':
                return {'rejected': response['data'].get('reason', 'proposal_rejected'), 'steps': self.steps, 'audit': response['data'].get('audit')}
            actions = self.codec.decode_actions(response['data']['normalized_actions'])
            self.chunk += 1
            audit = response['data']['audit']
            rejection = None
            if goal is not None:
                errors = audit['final_command_errors']
                if np.max(np.abs(actions['action.base_motion'][:16])) > 0.05 or np.any(actions['action.control_mode'][:16] > 0.5):
                    rejection = 'moving_base_codec_unsupported'
            write_json(self.folder / f'inference-{self.chunk:05d}.json',
                       {'seed': seed, 'scale': scale, 'goal': goal, 'audit': audit,
                        'steps_before': self.steps, 'rejection': rejection, 'execution': 'unchanged_native12'})
            np.save(self.folder / f'candidate-{self.chunk:05d}.npy', response['data']['normalized_actions'])
            if rejection is not None:
                return {'rejected': rejection, 'steps': self.steps, 'audit': audit}
            self.execute({k: v[:16] for k, v in actions.items()}, 'guided_vla' if goal is not None else 'native_vla')
        return self.observe()


    def probe(self, goal: dict[str, Any], scale: float = 30) -> dict[str, Any]:
        payload = {'examples': [self.codec.build_model_example(self.obs)], 'seed': 123,
                   'scale': scale, 'verify': True, 'geometry': self.geometry(goal)}
        response = self.client.predict_action(payload)
        assert response['ok'], response
        write_json(self.folder / f'probe-{self.command_id:05d}.json', response['data']['audit'])
        return response['data']['audit']


    def servo(self, position_world: list[float], quaternion_xyzw: list[float], gripper_close: float,
              steps: int, translation_limit_m: float = 0.02) -> dict[str, Any]:
        assert 1 <= steps <= 16 and 0 <= gripper_close <= 1
        assert np.isfinite(translation_limit_m) and 0 < translation_limit_m <= 0.05
        target = np.asarray(position_world)
        wanted = Rotation.from_quat(quaternion_xyzw).as_matrix()
        initial = self.robot_state()
        assert np.all(translation_limit_m <= np.asarray(initial['output_max'])[:3])
        assert np.all(-translation_limit_m >= np.asarray(initial['output_min'])[:3])
        assert np.linalg.norm(target - initial['position_world']) <= 0.3
        for _ in range(steps):
            if self.terminal:
                break
            state = self.robot_state()
            rotation = state['controller_origin_rotation']
            delta = np.clip((target - state['position_world']) @ rotation, -translation_limit_m, translation_limit_m)
            error_rot = rotation.T @ wanted @ state['rotation_world'].T @ rotation
            angular = np.clip(Rotation.from_matrix(error_rot).as_rotvec(), -0.1, 0.1)
            physical = np.concatenate([delta, angular])
            action = (physical - (state['output_max'] + state['output_min']) / 2)
            action *= (state['input_max'] - state['input_min']) / (state['output_max'] - state['output_min'])
            action += (state['input_max'] + state['input_min']) / 2
            controls = {'action.end_effector_position': action[None, :3],
                        'action.end_effector_rotation': action[None, 3:],
                        'action.gripper_close': np.array([[gripper_close]]),
                        'action.base_motion': np.zeros((1, 4)), 'action.control_mode': np.zeros((1, 1))}
            self.execute(controls, 'direct_servo')
        return self.observe()


    def base(self, motion: list[float], gripper_close: float, steps: int) -> dict[str, Any]:
        vector = np.asarray(motion)
        assert vector.shape == (4,) and np.isfinite(vector).all()
        assert np.all(np.abs(vector) <= np.array([1.0, 1.0, 0.5, 0.0])) and 1 <= steps <= 16
        assert 0 <= gripper_close <= 1
        actions = {'action.end_effector_position': np.zeros((steps, 3)),
                   'action.end_effector_rotation': np.zeros((steps, 3)),
                   'action.gripper_close': np.full((steps, 1), gripper_close),
                   'action.base_motion': np.repeat(vector[None], steps, axis=0),
                   'action.control_mode': np.ones((steps, 1))}
        self.execute(actions, 'direct_base')
        return self.observe()


    def command(self, request: dict[str, Any]) -> Any:
        kind = request['type']
        params = {k: v for k, v in request.items() if k != 'type' and (k != 'reason' or kind == 'finish')}
        methods: dict[str, Callable[..., Any]] = {'observe': self.observe, 'vla': self.vla, 'servo': self.servo, 'probe': self.probe,
                   'base': self.base, 'reset': self.reset, 'finish': self.finish}
        return methods[kind](**params)



def restore_session(args: argparse.Namespace) -> Session:
    root = args.output.resolve()
    folders = sorted(root.glob('attempt-*'))
    folder = folders[-1]
    assert not (folder / 'result.json').exists() and not (root / 'result.json').exists()
    original = json.loads((folder / 'initial.json').read_text())
    assert original['task'] == args.task and original['seed'] == args.seed
    restoration = root / f'restoration-{time.time_ns()}'
    options = argparse.Namespace(**vars(args))
    options.output, options.resume = restoration, False
    if (root / 'execution-reset').exists():
        options.reset_snapshot = root / 'execution-reset'
    session = Session(options)
    assert session.initial_hash == original['state_sha256']
    assert session.initial_scene_hash == original['scene_structure_sha256']
    assert session.language == original['task_language']
    evidence = []
    maximum_error = 0.0
    for path in sorted(folder.glob('actions-*.npz')):
        data = np.load(path, allow_pickle=False)
        for action, expected in zip(data['actions'], data['measured_relative_position'], strict=True):
            command = dict(zip(ACTION_KEYS, np.split(action.astype(np.float32), [3, 6, 7, 11]), strict=True))
            session.obs, _, done, truncated, info = session.env.step(command)
            assert not done and not truncated and not info['success'], 'Replay reached an unexpected terminal'
            measured = session.obs['state.end_effector_position_relative']
            error = float(np.max(np.abs(measured - expected)))
            maximum_error = max(maximum_error, error)
            assert np.array_equal(measured, expected), f'Replay physical state differs: {error}'
            session.steps += 1
        evidence.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'controls': len(data['actions'])})
    write_json(restoration / 'restoration.json', {'case': root.name, 'attempt': original['attempt'],
               'reason': 'Restore interrupted simulator from its own executed history; no new policy sampling',
               'replayed_controls': session.steps, 'extra_policy_attempts': 0,
               'maximum_recorded_state_error': maximum_error, 'executed_history': evidence,
               'initial_state_sha256': session.initial_hash, 'scene_structure_sha256': session.initial_scene_hash,
               'runtime_source_sha256': RUNTIME_SOURCE_SHA256})
    session.root, session.folder, session.args = root, folder, args
    session.attempt = original['attempt']
    session.chunk = len(list(folder.glob('inference-*.json')))
    completed_ids = [int(p.stem.split('-')[1]) for p in root.glob('response-*.json')]
    completed_ids += [int(p.stem.split('-')[1]) for p in folder.glob('actions-*.npz')]
    session.command_id = max(completed_ids, default=0)
    session.observation_suffix = f'-restored-{restoration.name}'
    observation = session.observe()
    if session.command_id and not (root / f'response-{session.command_id:05d}.json').exists():
        write_json(root / f'response-{session.command_id:05d}.json',
                   {**observation, 'engineering_restoration': str(restoration / 'restoration.json')})
    return session



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--skill', required=True)
    parser.add_argument('--port', type=int, default=11393)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    session = restore_session(args) if args.restore else Session(args)
    print('READY', args.output, flush=True)
    try:
        while not (session.root / 'result.json').exists():
            path = session.root / f'command-{session.command_id + 1:05d}.json'
            if not path.exists():
                time.sleep(0.5)
                continue
            request = json.loads(path.read_text())
            session.command_id += 1
            response = session.command(request)
            write_json(session.root / f'response-{session.command_id:05d}.json', response)
            print('COMMAND', session.command_id, request['type'], 'steps', session.steps, 'success', session.success, flush=True)
    finally:
        session.env.close()
        session.client.close()


if __name__ == '__main__':
    main()

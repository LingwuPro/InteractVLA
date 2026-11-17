import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from sim_tool import ACTION_KEYS, Session, write_json


# Input: recorded sensor responses; output: exact geometry checks and bounded 8-bit render differences.
def compare_observation(expected: dict[str, Any], actual: dict[str, Any], rgb_tolerance: int | None) -> list[dict[str, Any]]:
    for key in ('attempt', 'steps', 'success', 'terminal'):
        assert actual[key] == expected[key], key
    for key in ('position_world', 'rotation_world', 'gripper_qpos', 'base_position', 'base_rotation'):
        assert np.array_equal(actual['robot'][key], expected['robot'][key]), key
    render_differences = []
    for camera, spec in expected['cameras'].items():
        current = actual['cameras'][camera]
        for key in ('intrinsic', 'world_from_camera'):
            assert np.array_equal(spec[key], current[key]), (camera, key)
        delta = np.asarray(Image.open(spec['rgb']), dtype=int) - np.asarray(Image.open(current['rgb']), dtype=int)
        maximum = int(np.abs(delta).max())
        if rgb_tolerance is not None:
            assert maximum <= rgb_tolerance, (camera, 'rgb_quantization', maximum)
        render_differences.append({'camera': camera, 'different_channels': int(np.count_nonzero(delta)),
                                  'maximum_8bit_difference': maximum,
                                  'mean_absolute_8bit_difference': float(np.abs(delta).mean()),
                                  'pixels_over_two_units': int(np.count_nonzero(np.max(np.abs(delta), axis=2) > 2))})
        assert np.array_equal(np.load(spec['depth']), np.load(current['depth'])), (camera, 'depth')
    return render_differences


# Input: interrupted case and an explicit completed command interval; output: strictly verified live continuation.
def restore(args: argparse.Namespace) -> Session:
    root = args.output.resolve()
    folder = sorted(root.glob('attempt-*'))[-1]
    assert not (folder / 'result.json').exists() and not (root / 'result.json').exists()
    original = json.loads((folder / 'initial.json').read_text())
    assert original['task'] == args.task and original['seed'] == args.seed
    if args.record_render_differences:
        assert args.complete_native_stage and original['attempt'] <= 2
    rgb_tolerance = None if args.record_render_differences else args.rgb_tolerance
    repairs_path = root.parent.parent / 'initialization-repairs.json'
    repairs = json.loads(repairs_path.read_text()) if repairs_path.exists() else {}
    if root.name in repairs:
        for key in ('state_sha256', 'scene_structure_sha256'):
            assert original[key] == repairs[root.name][key], ('Frozen initialization expectation differs', key)
    destination = root / f'command-restoration-{time.time_ns()}'
    options = argparse.Namespace(**vars(args))
    options.output, options.resume = destination, False
    fresh_start = getattr(args, 'fresh_start_verify_first', False)
    if fresh_start:
        assert args.first_command == args.last_command + 1
        assert original['attempt'] == 3 and len(list(root.glob('attempt-*'))) == 3
        assert not list(folder.glob('actions-*.npz'))
        assert not (folder / 'registration-start.json').exists()
        completed_ids = [int(p.stem.split('-')[1]) for p in root.glob('response-*.json')]
        assert args.last_command == max(completed_ids, default=0)
        assert len(list(root.glob('command-*.json'))) == args.last_command
        for index in (1, 2):
            prior = root / f'attempt-{index:02d}'
            assert not json.loads((prior / 'result.json').read_text())['success']
            prior_initial = json.loads((prior / 'initial.json').read_text())
            assert prior_initial['task_language'] == original['task_language']
        previous_audit = json.loads((root / 'pre-recovery-attempt2-audit.json').read_text())
        assert previous_audit['physics_and_sensor_checks_passed'] and previous_audit['recorded_instruction_identical']
        assert not previous_audit['corrected_native_success']
        skill_hash = hashlib.sha256(Path(args.skill).read_bytes()).hexdigest()
        assert skill_hash == original['skill_sha256']
        assert (root.parent.parent / 'skill-history' / skill_hash / 'manifest.json').exists()
    elif (root / 'execution-reset').exists():
        options.reset_snapshot = root / 'execution-reset'
    session = Session(options)
    assert session.initial_hash == original['state_sha256']
    assert session.initial_scene_hash == original['scene_structure_sha256']
    assert session.language == original['task_language']
    fresh_checks: dict[str, Any] = {}
    if fresh_start:
        first = root / 'attempt-01'
        expected_initial = json.loads(next(first.glob('observation-000000-*/observation.json')).read_text())
        actual_initial = json.loads((session.root / 'observation.json').read_text())
        fresh_checks['rgb_differences_against_first'] = compare_observation(expected_initial, actual_initial, 1)
        with np.load(first / 'initial-policy-observation.npz') as expected_policy, \
                np.load(session.folder / 'initial-policy-observation.npz') as actual_policy:
            assert set(expected_policy.files) == set(actual_policy.files)
            assert all(np.array_equal(expected_policy[k], actual_policy[k]) for k in expected_policy.files)
        assert np.array_equal(np.load(root / 'execution-reset/integration-state.npy'),
                              np.load(destination / 'execution-reset/integration-state.npy'))
        second = root / 'attempt-02'
        second_initial = json.loads(next(second.glob('observation-000000-*/observation.json')).read_text())
        historical = {}
        with np.load(first / 'initial-policy-observation.npz') as first_policy, \
                np.load(second / 'initial-policy-observation.npz') as second_policy:
            for camera, spec in expected_initial['cameras'].items():
                key = f'video.{camera}'
                delta = np.abs(first_policy[key].astype(np.int16) - second_policy[key].astype(np.int16))
                depth_delta = np.abs(np.load(spec['depth']) - np.load(second_initial['cameras'][camera]['depth']))
                historical[camera] = {'rgb_max_8bit': int(delta.max()), 'rgb_channels_over_one': int((delta > 1).sum()),
                                      'depth_max_m': float(depth_delta.max()),
                                      'different_depth_pixels': int(np.count_nonzero(depth_delta))}
        fresh_checks.update({'first_policy_arrays_exact': True, 'first_depth_calibration_own_state_exact': True,
                             'integration_state_exact': True, 'attempt2_sensor_differences_against_first': historical,
                             'attempt2_sensor_comparability_held': True})
    session.attempt = original['attempt']
    observation = json.loads((session.root / 'observation.json').read_text())
    observation['attempt'] = session.attempt
    evidence = []
    for number in range(args.first_command, args.last_command + 1):
        session.command_id = number
        request_path = root / f'command-{number:05d}.json'
        request = json.loads(request_path.read_text())
        response_path = root / f'response-{number:05d}.json'
        expected = json.loads(response_path.read_text()) if response_path.exists() else None
        saved = sorted(folder.glob(f'actions-{number:05d}-*.npz'))
        assert expected is not None or (number == args.last_command and saved), 'Missing historical response'
        print('REPLAY', number, request['type'], session.steps, flush=True)
        render_differences = []
        if expected is not None and expected.get('origin') == 'operator_transport_repair_not_simulator_observation':
            assert expected['executed_steps'] == 0 and expected['steps'] == session.steps
            if expected.get('rejected') == 'servo_target_exceeds_distance_limit':
                assert request['type'] == 'servo' and not saved
                position = session.robot_state()['position_world']
                rejected_distance = float(np.linalg.norm(np.asarray(request['position_world']) - position))
                assert rejected_distance > 0.3 and rejected_distance == expected['target_distance_m']
        elif request['type'] == 'vla':
            assert 1 <= request['chunks'] <= 256
            goal = request.get('goal')
            rejection = expected.get('rejected') if expected is not None else None
            if goal is not None:
                assert request['chunks'] == 1
                distance = np.linalg.norm(np.asarray(goal['position_world']) - session.robot_state()['position_world'])
                assert (distance > 0.25) == (rejection == 'goal_too_far')
                if rejection != 'goal_too_far':
                    session.geometry(goal)
            if expected is not None and 'rejected' in expected:
                assert goal is not None and not saved
                assert expected['rejected'] in ('moving_base_codec_unsupported', 'guidance_unresolved', 'goal_too_far')
                assert expected['steps'] == session.steps
                if expected['rejected'] != 'goal_too_far':
                    session.chunk += 1
            else:
                assert 1 <= len(saved) <= request['chunks']
                for saved_chunk in saved:
                    session.chunk += 1
                    with np.load(saved_chunk) as record:
                        assert 1 <= len(record['actions']) <= 16
                        native_actions = dict(zip(ACTION_KEYS, np.split(record['actions'].astype(np.float32), [3, 6, 7, 11], axis=1), strict=True))
                    session.execute(native_actions, 'engineering_saved_action_replay')
                    assert not session.terminal, 'Replay reached an unexpected terminal'
                actual = session.observe()
                observation = actual
                if expected is not None:
                    render_differences = compare_observation(expected, actual, rgb_tolerance)
        else:
            assert expected is not None
            assert request['type'] in ('observe', 'servo', 'base')
            actual = session.command(request)
            observation = actual
            render_differences = compare_observation(expected, actual, rgb_tolerance)
        replayed = sorted(session.folder.glob(f'actions-{number:05d}-*.npz'))
        assert [p.name for p in saved] == [p.name for p in replayed], number
        controls = 0
        for before, after in zip(saved, replayed, strict=True):
            with np.load(before) as old, np.load(after) as new:
                for key in ('actions', 'measured_relative_position'):
                    assert np.array_equal(old[key], new[key]), (number, before.name, key)
                controls += len(old['actions'])
        evidence.append({'command': number, 'type': request['type'], 'controls': controls,
                         'historical_response_available': expected is not None,
                         'render_differences': render_differences,
                         'request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest()})
        print('VERIFIED', number, session.steps, flush=True)
    assert sum(row['controls'] for row in evidence) == session.steps
    proof = {'case': root.name, 'attempt': session.attempt, 'replayed_controls': session.steps,
             'extra_policy_attempts': 0, 'new_policy_samples': 0,
             'verification': 'Exact first-attempt policy observation, own state, integration state, depth and calibration' if fresh_start else
                             'Exact recorded actions, per-step relative hand positions, per-command own robot poses, depth and camera calibration; RGB differences separately recorded',
             'rgb_tolerance_8bit_units': rgb_tolerance,
             'rgb_differences_diagnostic_only': args.record_render_differences,
             'commands': evidence, 'initial_state_sha256': session.initial_hash,
             'scene_structure_sha256': session.initial_scene_hash,
             'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'fresh_start_verified_against_first': fresh_start, 'fresh_start_checks': fresh_checks}
    write_json(destination / 'restoration.json', proof)
    if not (root / 'execution-reset').exists():
        shutil.copytree(destination / 'execution-reset', root / 'execution-reset')
    session.root, session.folder, session.args = root, folder, args
    session.observation_suffix = f'-restored-{destination.name}'
    if fresh_start:
        session.command_id = args.last_command
    write_json(session.root / 'observation.json', observation)
    if fresh_start:
        repair = {'case': root.name, 'attempt': session.attempt, 'fresh_initialization': True,
                  'replayed_controls': 0, 'new_policy_samples': 0, 'new_evaluation_attempts': 0,
                  'effective_initial_observation': str(destination / 'observation.json'),
                  'effective_initial_policy': str(destination / 'attempt-01/initial-policy-observation.npz'),
                  'original_attempt3_restored_initial_preserved': True, 'proof': str(destination / 'restoration.json'),
                  **fresh_checks}
        write_json(folder / 'fresh-start-repair.json', repair)
        write_json(folder / 'registration-start.json', {
            'case': root.name, 'attempt': session.attempt, 'seed': args.seed,
            'initial_state_sha256': session.initial_hash, 'scene_structure_sha256': session.initial_scene_hash,
            'same_instruction_all_attempts': True, 'effective_third_initial_matches_first': True,
            'historical_second_attempt_sensor_comparability_held': True, 'fresh_start_repair': str(folder / 'fresh-start-repair.json'),
            'second_attempt_replay_verified': True, 'model_endpoint': 'PRO 127.0.0.1:11395 -> local H100 127.0.0.1:11405',
            'policy_model': 'frozen_starvla_final', 'policy_source_sha256': original['model_metadata']['source_sha256'],
            'checkpoint_sha256': 'bbd26ff22f491d71d3325697655948b12138af70adc65027b7d084f36c860a88',
            'skill_sha256': skill_hash, 'attempt_limit': 3, 'steps': 0, 'registration_passed': True})
    response_path = root / f'response-{session.command_id:05d}.json'
    if session.command_id and not response_path.exists():
        write_json(response_path, {**observation, 'engineering_restoration': str(destination / 'restoration.json')})
    return session


# Input: verified native-stage continuation; output: remaining original attempts through attempt two.
def complete_native_stage(session: Session) -> None:
    started = time.time()
    assert session.attempt <= 2 and not (session.root / 'native-stage.json').exists()
    while True:
        while not session.terminal:
            session.command_id += 1
            request = {'type': 'vla', 'chunks': 1, 'reason': 'Native baseline after verified engineering continuation'}
            write_json(session.root / f'command-{session.command_id:05d}.json', request)
            response = session.command(request)
            write_json(session.root / f'response-{session.command_id:05d}.json', response)
            print('NATIVE', session.attempt, session.steps, session.success, flush=True)
        if session.success or session.attempt == 2:
            break
        session.reset()
    write_json(session.root / 'native-stage.json', {'case_id': session.root.name, 'success': session.success,
               'attempts_used': session.attempt, 'needs_assistant_recovery': not session.success,
               'elapsed_seconds': time.time() - started, 'continuation': session.observation_suffix})


# Input: explicit restore interval and original simulator arguments; output: bounded interactive command service.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--skill', required=True)
    parser.add_argument('--port', type=int, default=11393)
    parser.add_argument('--first-command', type=int, required=True)
    parser.add_argument('--last-command', type=int, required=True)
    parser.add_argument('--complete-native-stage', action='store_true')
    parser.add_argument('--rgb-tolerance', type=int, choices=(0, 1, 2), default=1)
    parser.add_argument('--record-render-differences', action='store_true')
    parser.add_argument('--fresh-start-verify-first', action='store_true')
    args = parser.parse_args()
    session = restore(args)
    print('READY', args.output, flush=True)
    try:
        if args.complete_native_stage:
            complete_native_stage(session)
            return
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

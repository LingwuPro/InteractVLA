import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from attempt_budget import attempt_limit
from PIL import Image

CHECKPOINT_SHA256 = 'bbd26ff22f491d71d3325697655948b12138af70adc65027b7d084f36c860a88'
POLICY_SHA256 = 'd6858592adfd79f2b3cb2dd84e52f081d5c595152e40036533bdbfa48dd0e9c3'


# Input: one saved JSON object; output: its structured record.
def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# Input: one local file; output: its SHA256 identity.
def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Input: two public three-camera RGB-D records; output: exact sensor checks by camera.
def sensor_checks(reference: dict[str, Any], current: dict[str, Any]) -> dict[str, dict[str, bool]]:
    checks = {}
    assert reference['cameras'].keys() == current['cameras'].keys()
    for name, spec in reference['cameras'].items():
        actual = current['cameras'][name]
        with Image.open(spec['rgb']) as first, Image.open(actual['rgb']) as second:
            rgb = np.array_equal(np.asarray(first), np.asarray(second))
        checks[name] = {
            'rgb': rgb,
            'depth': np.array_equal(np.load(spec['depth']), np.load(actual['depth'])),
            'intrinsic': np.array_equal(spec['intrinsic'], actual['intrinsic']),
            'world_from_camera': np.array_equal(spec['world_from_camera'], actual['world_from_camera']),
        }
    return checks


# Input: a ready third-attempt case and its log; output: registration artifacts, zero controls or model calls.
def register(case: Path, log: Path, wait_seconds: int) -> dict[str, Any]:
    root, attempt = case.parent.parent, case / 'attempt-03'
    assert case.parent.name == 'cases' and attempt_limit(case) == 3
    assert len(list(case.glob('attempt-*'))) == 3 and not (case / 'result.json').exists()
    for _ in range(wait_seconds):
        if log.exists() and 'READY ' in log.read_text() and (attempt / 'initial.json').exists():
            break
        time.sleep(1)
    assert f'READY {case}' in log.read_text(), 'Keep the same simulator and poll again'
    current = read_json(case / 'observation.json')
    assert current['attempt'] == 3 and current['steps'] == 0 and not current['terminal']
    for index in (1, 2):
        assert not read_json(case / f'attempt-{index:02d}' / 'result.json')['success']
    initials = [read_json(case / f'attempt-{i:02d}' / 'initial.json') for i in (1, 2, 3)]
    for key in ('state_sha256', 'scene_structure_sha256', 'task_language'):
        assert len({record[key] for record in initials}) == 1, key
    assert all(record['task'] == current['task'] and record['seed'] == current['seed'] for record in initials)
    assert initials[-1]['reset_source_sha256'] == digest(root / 'reset_state.py')
    assert initials[-1]['model_metadata']['source_sha256'] == digest(root / 'policy_server.py') == POLICY_SHA256
    policy_proof = root / 'policy-restart-20261106-after-reboot.json'
    assert read_json(policy_proof)['checkpoint_sha256'] == CHECKPOINT_SHA256
    observations = [read_json(next((case / f'attempt-{i:02d}').glob('observation-000000-*/observation.json')))
                    for i in (1, 2, 3)]
    camera_checks: dict[str, list[dict[str, bool]]] = {name: [] for name in observations[0]['cameras']}
    for index in (1, 2):
        checked = sensor_checks(observations[0], observations[index])
        for name, row in checked.items():
            assert all(row.values()), (index + 1, name, row)
            camera_checks[name].append(row)
        with np.load(case / 'attempt-01/initial-policy-observation.npz') as first, \
                np.load(case / f'attempt-{index + 1:02d}/initial-policy-observation.npz') as actual:
            assert set(first.files) == set(actual.files)
            assert all(np.array_equal(first[key], actual[key]) for key in first.files)
        assert all(np.array_equal(value, observations[index]['robot'][key])
                   for key, value in observations[0]['robot'].items() if key in observations[index]['robot'])
    proof = case / 'pre-recovery-attempt2-audit.json'
    replay = read_json(proof)
    assert replay['case'] == case.name and replay['attempt'] == 2
    assert replay['physics_and_sensor_checks_passed'] and not replay['corrected_native_success']
    assert replay['recorded_instruction_identical'] and replay['replay_instruction_matches_first_attempt']
    assert replay['maximum_rgb_difference_8bit'] <= 1
    assert replay['policy_calls'] == replay['evaluation_attempts_added'] == 0
    assert replay['initial_state_sha256'] == initials[0]['state_sha256']
    assert replay['scene_structure_sha256'] == initials[0]['scene_structure_sha256']
    assert replay['original_result_sha256'] == digest(case / 'attempt-02/result.json')
    assert replay['replayed_controls'] == read_json(case / 'attempt-02/result.json')['steps']
    skill = digest(Path('.codex/skills/robocasa365-agent/SKILL.md'))
    assert initials[-1]['skill_sha256'] == skill
    bundle = read_json(root / 'skill-history' / skill / 'manifest.json')
    assert bundle['skill_sha256'] == skill
    assert all(digest(Path('.codex/skills/robocasa365-agent') / name) == value
               for name, value in bundle['files'].items())
    registration = {
        'case': case.name, 'attempt': 3, 'seed': current['seed'],
        'initial_state_sha256': initials[0]['state_sha256'],
        'scene_structure_sha256': initials[0]['scene_structure_sha256'],
        'same_instruction_all_attempts': True, 'policy_arrays_exact_all_attempts': True,
        'rgb_depth_calibration_exact_all_attempts': True, 'common_robot_fields_exact_all_attempts': True,
        'camera_checks': camera_checks, 'second_attempt_replay_verified': True,
        'second_attempt_replay_proof': str(proof),
        'model_endpoint': 'PRO 127.0.0.1:11395 -> local H100 127.0.0.1:11405',
        'policy_model': 'frozen_starvla_final', 'policy_source_sha256': POLICY_SHA256,
        'checkpoint_sha256': CHECKPOINT_SHA256, 'checkpoint_hash_verified_from_file': True,
        'checkpoint_file_hash_proof': str(policy_proof), 'skill_sha256': skill,
        'attempt_limit': 3, 'steps': 0, 'registration_passed': True,
        'registration_source_sha256': digest(Path(__file__)),
    }
    records = {
        'skill-bundle.json': bundle,
        'attempt-budget.json': {'attempt_limit': 3, 'policy_sha256': digest(root / 'attempt-budget-policy.json')},
        'registration-start.json': registration,
    }
    assert all(not (attempt / name).exists() for name in records), 'Registration already exists'
    for name, record in records.items():
        with (attempt / name).open('x') as handle:
            handle.write(json.dumps(record, indent=2) + '\n')
        (attempt / name).chmod(0o644)
    state = read_json(root / 'task-state.json')
    state.update({'updated_utc': datetime.now(timezone.utc).isoformat(),
                  'status': f'active composite-unseen; {case.name} attempt 3 registered at step0',
                  'live_execution': {'case': case.name, 'attempt': 3, 'steps': 0, 'steps_at_registration': 0,
                                     'proof': str(attempt / 'registration-start.json'), 'sim_log': str(log),
                                     'success': False, 'terminal': False}})
    (root / 'task-state.json').write_text(json.dumps(state, indent=2) + '\n')
    return {'registration_passed': True, 'case': case.name, 'skill_sha256': skill,
            'task_language': initials[-1]['task_language'], 'steps': 0}


# Input: one explicit case and log; output: a short registration result after all comparisons pass.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('case', type=Path)
    parser.add_argument('--log', required=True, type=Path)
    parser.add_argument('--wait-seconds', type=int, default=55)
    args = parser.parse_args()
    print(json.dumps(register(args.case, args.log, args.wait_seconds)), flush=True)


if __name__ == '__main__':
    main()

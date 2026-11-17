import argparse
import json
import time
from pathlib import Path

from sim_tool import Session, restore_session, write_json

ROOT = Path(__file__).resolve().parent


# Input: disjoint manifest shard; output: up to two native attempts per case, then an explicit recovery queue.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--shard', type=int, required=True)
    parser.add_argument('--shards', type=int, default=4)
    parser.add_argument('--port', type=int, default=11393)
    parser.add_argument('--fresh-only', action='store_true')
    parser.add_argument('--case-limit', type=int)
    args = parser.parse_args()
    assert 0 <= args.shard < args.shards
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    skill = ROOT.parents[1] / '.codex/skills/robocasa365-agent/SKILL.md'
    repairs_path = ROOT / 'initialization-repairs.json'
    repairs = json.loads(repairs_path.read_text()) if repairs_path.exists() else {}
    processed = 0
    for case in manifest['cases'][args.shard::args.shards]:
        if (ROOT / f'pause-shard-{args.shard}').exists():
            print('PAUSED at case boundary', flush=True)
            break
        output = ROOT / 'cases' / case['case_id']
        if args.case_limit is not None and processed >= args.case_limit:
            break
        if args.fresh_only and output.exists():
            print('DEFER existing case', case['case_id'], flush=True)
            continue
        status = output / 'native-stage.json'
        if status.exists():
            continue
        options = argparse.Namespace(task=case['task'], seed=case['seed'], output=output,
                                     checkpoint=manifest['checkpoint'], skill=str(skill), port=args.port,
                                     resume=False, restore=False)
        session = restore_session(options) if output.exists() else Session(options)
        if case['case_id'] in repairs:
            expectation = repairs[case['case_id']]
            assert session.initial_hash == expectation['state_sha256']
            assert session.initial_scene_hash == expectation['scene_structure_sha256']
        started = time.time()
        try:
            for attempt in range(session.attempt, 3):
                while not session.terminal:
                    session.command_id += 1
                    request = {'type': 'vla', 'chunks': 1, 'reason': 'Native baseline before assisted recovery'}
                    write_json(output / f'command-{session.command_id:05d}.json', request)
                    response = session.command(request)
                    write_json(output / f'response-{session.command_id:05d}.json', response)
                print(json.dumps({'case': case['case_id'], 'attempt': attempt, 'success': session.success,
                                  'steps': session.steps}), flush=True)
                if session.success or attempt == 2:
                    break
                session.reset()
            processed += 1
            write_json(status, {'case_id': case['case_id'], 'success': session.success,
                                'attempts_used': session.attempt, 'needs_assistant_recovery': not session.success,
                                'elapsed_seconds': time.time() - started})
        finally:
            session.env.close()
            session.client.close()


if __name__ == '__main__':
    main()

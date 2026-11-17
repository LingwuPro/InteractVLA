import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# Input: campaign artifact tree; output: periodic native-stage and recovery-queue status without model calls.
def inspect() -> dict:
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    starts, completed, attempts, first, second = 0, 0, 0, 0, 0
    recovery = []
    for case in manifest['cases']:
        folder = ROOT / 'cases' / case['case_id']
        starts += folder.exists()
        completed += (folder / 'result.json').exists()
        results = [json.loads(p.read_text()) for p in sorted(folder.glob('attempt-*/result.json'))]
        attempts += len(results)
        successes = [r['attempt'] for r in results if r['success']]
        first += bool(successes and min(successes) == 1)
        second += bool(successes and min(successes) <= 2)
        stage = folder / 'native-stage.json'
        if stage.exists() and not (folder / 'result.json').exists():
            recovery.append({'case_id': case['case_id'], 'task': case['task'], 'seed': case['seed'],
                             'attempts_completed': len(results), 'attempts_remaining': 6 - len(results)})
    logs = {str(i): (ROOT / f'native-stage-{i}.log').read_text().splitlines()[-8:] for i in range(3)}
    return {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'expected_cases': 1250,
            'cases_started': starts, 'cases_completed': completed, 'attempts_completed': attempts,
            'first_attempt_successes': first, 'two_attempt_successes': second,
            'waiting_for_assistant_recovery': recovery, 'worker_log_tails': logs,
            'note': 'File status only; no automatic conversation wakeup or autonomous visual planner.'}


# Input: interval in seconds; output: append-only checks and atomically replaced current status.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=int, default=600)
    args = parser.parse_args()
    while True:
        status = inspect()
        temporary = ROOT / 'status.tmp'
        temporary.write_text(json.dumps(status, indent=2) + '\n')
        temporary.replace(ROOT / 'status.json')
        compact = {k: v for k, v in status.items() if k not in ('waiting_for_assistant_recovery', 'worker_log_tails')}
        compact['waiting_for_assistant_recovery'] = len(status['waiting_for_assistant_recovery'])
        with (ROOT / 'monitor.jsonl').open('a') as handle:
            handle.write(json.dumps(compact) + '\n')
        print(json.dumps(compact), flush=True)
        if status['cases_completed'] == 1250:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from attempt_budget import summarize_progress

ROOT = Path(__file__).resolve().parent


# Input: pinned 2500-job native manifest; output: deterministic 1250-case list without outcome selection.
def initialize() -> None:
    source = ROOT.parent / 'robocasa365_xr1/eval_results/scheduler/formal_pretrain_target50_seed7_20261022/manifest.json'
    manifest = json.loads(source.read_text())
    cases = [{'case_id': f"{job['env_name']}-{job['episode']:02d}", 'task': job['env_name'],
              'episode': job['episode'], 'seed': manifest['config']['seed'] + job['global_episode_index'],
              'max_attempts': 3} for job in manifest['jobs'] if job['episode'] < 25]
    assert len(cases) == 1250 and len({case['case_id'] for case in cases}) == 1250
    output = {'protocol': 'robocasa365-observation-agent-adaptive3-v2', 'split': 'pretrain',
              'expected_cases': 1250, 'max_attempts_per_case': 3,
              'checkpoint': str(ROOT.parent / 'robocasa365_starvla/checkpoints/target_composite_final/final_model/pytorch_model.pt'),
              'source_task_manifest_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'selection': 'first 25 episode indices in every original Target50 task, independent of outcomes',
              'paired_native_results': 'must collect within the new seeded protocol; old StarVLA vector results unpaired',
              'cases': cases}
    path = ROOT / 'manifest.json'
    with path.open('x') as handle:
        handle.write(json.dumps(output, indent=2) + '\n')
    print('Created', path, '1250 cases')


# Input: immutable attempts and recorded caps; output: fixed-denominator counts with separate cap cohorts.
def summarize() -> dict[str, Any]:
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    rows = []
    for case in manifest['cases']:
        folder = ROOT / 'cases' / case['case_id']
        attempts = [json.loads(p.read_text()) for p in sorted(folder.glob('attempt-*/result.json'))]
        assert len(attempts) <= case['max_attempts']
        successful = [a['attempt'] for a in attempts if a['success']]
        first = min(successful) if successful else None
        rows.append({'case_id': case['case_id'], 'attempt_limit': case['max_attempts'],
                     'attempts_completed': len(attempts),
                     'first_success_attempt': first, 'complete': (folder / 'result.json').is_file()})
    progress = summarize_progress(ROOT)
    report = {'expected_cases': 1250, 'completed_cases': sum(row['complete'] for row in rows),
              'actual_attempts_completed': sum(row['attempts_completed'] for row in rows),
              'successes_by_attempt': {str(n): sum(row['first_success_attempt'] is not None and row['first_success_attempt'] <= n for row in rows) for n in (1, 2, 3, 6)},
              'protocol': manifest['protocol'], 'cohorts': progress['cohorts'],
              'semantics': progress['semantics'], 'cases': rows}
    (ROOT / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    return report


# Input: requested manifest or aggregation operation; output: deterministic campaign bookkeeping.
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['initialize', 'summarize'])
    args = parser.parse_args()
    if args.operation == 'initialize':
        initialize()
    else:
        summarize()


if __name__ == '__main__':
    main()

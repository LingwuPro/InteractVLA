import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Input: one case directory; output: its frozen historical or current attempt cap.
def attempt_limit(folder: Path) -> int:
    policy = folder.parent.parent / 'attempt-budget-policy.json'
    if not policy.exists():
        manifest = folder.parent.parent / 'manifest.json'
        if manifest.exists():
            cases = json.loads(manifest.read_text())['cases']
            return int(next(case['max_attempts'] for case in cases if case['case_id'] == folder.name))
        return 6
    return int(json.loads(policy.read_text())['cases'][folder.name]['attempt_limit'])


# Input: case history; output: allowed cap, rejecting any extra attempt before submission.
def ensure_next_attempt(folder: Path) -> int:
    limit = attempt_limit(folder)
    attempts = sorted(folder.glob('attempt-*'))
    assert not (folder / 'result.json').exists(), 'Case is already final'
    assert len(attempts) < limit, f'Attempt limit {limit} reached'
    for item in attempts:
        result = json.loads((item / 'result.json').read_text())
        assert not result['success'], 'Successful cases cannot retry'
    return limit


# Input: completed native failures at the cap; output: immutable case terminal record.
def seal_exhausted_case(folder: Path) -> None:
    limit = attempt_limit(folder)
    results = [json.loads(p.read_text()) for p in sorted(folder.glob('attempt-*/result.json'))]
    assert len(results) == limit and not any(r['success'] for r in results)
    target = folder / 'result.json'
    if target.exists():
        existing = json.loads(target.read_text())
        assert not existing['success'] and existing['attempts_used'] == limit
        return
    final = {**results[-1], 'attempts_used': limit, 'attempt_limit': limit,
             'termination': 'attempt_limit_exhausted',
             'attempt_budget_policy': '../../attempt-budget-policy.json'}
    with target.open('x') as handle:
        handle.write(json.dumps(final, indent=2) + '\n')
    target.chmod(0o644)


# Input: fixed manifest and immutable completed attempts; output: mixed-cap progress and cohorts.
def summarize_progress(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / 'manifest.json').read_text())
    policy_path = root / 'attempt-budget-policy.json'
    policies = json.loads(policy_path.read_text())['cases'] if policy_path.exists() else {}
    finalized = successes = exhausted = attempts = controls = first = two = three = 0
    cohorts: dict[str, dict[str, int]] = {}
    for spec in manifest['cases']:
        name = spec['case_id']
        record: dict[str, Any] = policies[name] if policies else {
            'attempt_limit': spec['max_attempts'], 'cohort': f"max{spec['max_attempts']}"}
        limit = int(record['attempt_limit'])
        cohort = cohorts.setdefault(record['cohort'], {'cases': 0, 'finalized': 0,
                                                      'successes': 0, 'exhausted': 0})
        cohort['cases'] += 1
        folder = root / 'cases' / name
        results = [json.loads(p.read_text()) for p in sorted(folder.glob('attempt-*/result.json'))]
        assert len(results) <= limit, f'{name} exceeded its recorded cap'
        attempts += len(results)
        controls += sum(r['steps'] for r in results)
        wins = [i for i, r in enumerate(results, 1) if r['success']]
        if wins:
            assert len(wins) == 1 and wins[0] == len(results)
            successes += 1
            finalized += 1
            first += wins[0] == 1
            two += wins[0] <= 2
            three += wins[0] <= 3
            cohort['successes'] += 1
            cohort['finalized'] += 1
        elif len(results) == limit:
            exhausted += 1
            finalized += 1
            cohort['exhausted'] += 1
            cohort['finalized'] += 1
    expected = len(manifest['cases'])
    progress: dict[str, Any] = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'expected_cases': expected,
            'finalized_cases': finalized, 'successes': successes, 'exhausted_cases': exhausted,
            'pending_cases': expected - finalized, 'actual_completed_attempts': attempts,
            'actual_controls_completed_attempts': controls, 'first_attempt_successes': first,
            'within_two_successes': two, 'within_three_successes': three,
            'full_1250_complete': finalized == expected, 'cohorts': cohorts,
            'semantics': 'Adaptive recovery with a prospective attempt-cap change; historical and future cohorts remain separate.'}
    audit_path = root / 'navigation-consistency-audit-state.json'
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())
        pending = audit['all_counts_are_raw_pending_validity_audit']
        progress['execution_consistency_audit'] = {
            'source': str(audit_path), 'status': audit['status'],
            'counts_are_raw_pending_validity_audit': pending,
            'audited_cases': audit.get('audited_cases', {}),
        }
        progress['all_cases_have_native_result'] = finalized == expected
        progress['full_1250_complete'] = finalized == expected and not pending
        if pending:
            progress['semantics'] += ' Counts are raw native flags pending execution-state validity audit; not a validated success rate.'
    return progress

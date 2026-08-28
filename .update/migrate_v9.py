#!/usr/bin/env python3
import json
from pathlib import Path

H = Path('data/history.json')
T = Path('data/tests.json')
S = Path('data/state.json')

history = json.loads(H.read_text())
for row in history.get('hours', []):
    if 'infra_failures' in row:
        continue
    old_failed = int(row.pop('failed_runs', 0) or 0) + int(row.pop('failed_jobs', 0) or 0)
    row['workflow_failures'] = int(row.get('workflow_failures', 0) or 0)
    row['infra_failures'] = 0
    row['code_failures'] = 0
    row['ignored_nonverdicts'] = 0
    row['unresolved_failures'] = old_failed
    row.pop('probabilistic_workflows', None)
    row['failures'] = []
    row['reasons'] = {}
    if int(row.get('probabilistic_jobs', 0) or 0) > 0:
        row['status'] = 'down'
    elif row.get('coverage') == 'partial' or old_failed:
        row['status'] = 'unknown'
    elif int(row.get('runs', 0) or 0) > 0:
        row['status'] = 'healthy'
    else:
        row['status'] = 'unknown'
history['schema_version'] = 9
history['policy'] = {
    'code_failures_are_valid_ci_verdicts': True,
    'infrastructure_failures_are_unavailable': True,
    'probability_sensitive_job_presence_is_unavailable': True,
    'unresolved_failure_is_unknown': True,
    'unknown_excluded_from_availability': True,
}
H.write_text(json.dumps(history, ensure_ascii=False, indent=2) + '\n')

tests = json.loads(T.read_text())
for item in tests.get('tests', {}).values():
    if item.get('kind') == 'workflow':
        item['probabilistic'] = False
        item.pop('probability_reason', None)
        item.pop('first_detected_at', None)
tests['schema_version'] = 9
T.write_text(json.dumps(tests, ensure_ascii=False, indent=2) + '\n')

state = json.loads(S.read_text())
state['schema_version'] = 9
S.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
print('migrated data to policy schema v9 conservatively')

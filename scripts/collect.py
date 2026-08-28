#!/usr/bin/env python3
from __future__ import annotations
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
REPO = os.getenv('UPSTREAM_REPO', 'vllm-project/vllm-ascend')
ROOT = 'https://api.github.com'
DATA = Path('data')
HISTORY = DATA / 'history.json'
TESTS = DATA / 'tests.json'
STATE = DATA / 'state.json'
SCHEMA = 9
RETENTION = int(os.getenv('RETENTION_DAYS', '90'))
OBS = int(os.getenv('OBSERVATION_DAYS', '30'))
BOOT = int(os.getenv('BOOTSTRAP_HOURS', '24'))
LOOKBACK = int(os.getenv('NORMAL_LOOKBACK_HOURS', '3'))
DETAIL_HOURS = int(os.getenv('DETAIL_LOOKBACK_HOURS', '6'))
TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '25'))
ANON_BUDGET = int(os.getenv('ANON_REQUEST_BUDGET', '54'))
AUTH_BUDGET = int(os.getenv('AUTH_REQUEST_BUDGET', '1200'))
EVENTS = ('pull_request', 'pull_request_target', 'push', 'schedule', 'workflow_dispatch', 'repository_dispatch', 'workflow_call', 'workflow_run', 'merge_group')
SUCCESS = {'success', 'neutral'}
IGNORED = {'skipped', 'cancelled', 'action_required', 'stale'}
FAILURE_CONCLUSIONS = {'failure', 'timed_out', 'startup_failure'}
NON_CI = re.compile('(bot[_ -]|stale|label(er)?|merge[_ -]?conflict|issue[_ -]?(manage|triage)|handle /|command|auto[_ -]?merge|assign(er)?|welcome|pr[_ -]?close|cancel[_ -]?(runs?|jobs?)|cancel (runs?|jobs?))', re.I)
PROB_POLICY = re.compile('(performance|\\bperf\\b|benchmark|accuracy|acceptance|pass.?rate|precision|evaluation|\\beval\\b|性能|精度|采信)', re.I)
ARTIFACT_ONLY = re.compile('\\bartifact(s)?\\b', re.I)
POLICY_HELPER = re.compile('(^|[/ :(\\-_])(generate|prepare|setup|matrix|merge|upload|download|collect)(\\b|[/ :)\\-_])', re.I)
NETWORK_LOG = re.compile('(temporary failure in name resolution|could not resolve host|name or service not known|network is unreachable|connection (?:timed out|reset by peer|refused)|tls handshake timeout|proxy error|remote end closed connection|502 bad gateway|503 service unavailable|504 gateway timeout)', re.I)
DOWNLOAD_LOG = re.compile('(could not transfer artifact|failed to (?:fetch|download)|download (?:failed|error)|pip[^\\n]*(?:readtimeout|connectionerror)|curl:\\s*\\((?:6|7|18|28|35|56)\\)|wget[^\\n]*(?:unable to resolve|connection timed out)|could not install packages due to an oserror[^\\n]*(?:connection|http)|unexpected eof[^\\n]*(?:download|http|connection))', re.I)
RUNNER_LOG = re.compile('(runner (?:has )?lost communication|runner is offline|runner.*not responding|the self-hosted runner|no space left on device|cannot connect to the docker daemon|failed to start (?:the )?container|container.*(?:failed to start|unhealthy)|pod .*?(?:evicted|failed|unschedulable)|node .*?not ready|device .*?(?:not found|unavailable)|npu .*?(?:not found|unavailable|offline)|acl_error_rt_device|resource temporarily unavailable)', re.I)
TEST_META = re.compile('(test|pytest|unittest|assert|accuracy|acceptance|performance|perf|benchmark|bench|eval)', re.I)
BUILD_META = re.compile('(build|compile|cmake|ninja|gcc|g\\+\\+|clang|wheel|package|lint|format|mypy|ruff|docs? link)', re.I)
SETUP_META = re.compile('(checkout|install|download|dependency|setup|prepare|docker pull|pull image|cache)', re.I)

def is_ci_text(text: str) -> bool:
    return not bool(NON_CI.search(text or ''))

def is_ci_run(run: dict[str, Any]) -> bool:
    return is_ci_text((run.get('path') or '') + ' ' + (run.get('name') or ''))

def is_policy_prob(workflow: str | None, name: str | None) -> bool:
    text = (workflow or '') + ' ' + (name or '')
    return bool(PROB_POLICY.search(text)) and (not bool(ARTIFACT_ONLY.search(name or ''))) and (not bool(POLICY_HELPER.search(name or '')))

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None

def iso_hour(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat().replace('+00:00', 'Z')

def iso_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except (OSError, json.JSONDecodeError):
        return default

def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)

class GH:

    def __init__(self) -> None:
        self.token = os.getenv('UPSTREAM_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN') or ''
        self.rejected = False
        self.requests = 0
        self.fallbacks = 0

    @property
    def auth(self) -> bool:
        return bool(self.token) and (not self.rejected)

    @property
    def budget(self) -> int:
        return AUTH_BUDGET if self.auth else ANON_BUDGET

    def ok(self, reserve: int=0) -> bool:
        return self.requests < self.budget - reserve

    def request(self, url: str, use_auth: bool=True, fallback_404: bool=True) -> bytes:
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'vllm-ascend-ci-monitor/4'}
        if use_auth and self.auth:
            headers['Authorization'] = 'Bearer ' + self.token
        self.requests += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if use_auth and self.auth and (exc.code in (401, 403) or (fallback_404 and exc.code == 404)):
                self.rejected = True
                self.fallbacks += 1
                return self.request(url, False, fallback_404)
            raise

    def get(self, path: str, params: dict[str, Any] | None=None) -> Any:
        query = '?' + urllib.parse.urlencode(params) if params else ''
        return json.loads(self.request(ROOT + path + query).decode())

    def text(self, path: str) -> str:
        return self.request(ROOT + path, use_auth=False, fallback_404=False).decode('utf-8', errors='replace')

def _run_span(gh: GH, event: str, start: datetime, end: datetime, depth: int=0) -> tuple[dict[int, dict[str, Any]], bool, dict[str, Any]]:
    if not gh.ok(8):
        return ({}, False, {'expected': None, 'fetched': 0, 'complete': False, 'slices': 0})
    span = f'{iso_ts(start)}..{iso_ts(end)}'
    first = gh.get(f'/repos/{REPO}/actions/runs', {'event': event, 'created': span, 'per_page': 100, 'page': 1})
    expected = int(first.get('total_count') or 0)
    rows = first.get('workflow_runs', [])
    if expected > 950 and end - start > timedelta(minutes=15) and (depth < 12):
        mid = start + (end - start) / 2
        left, left_ok, left_meta = _run_span(gh, event, start, mid, depth + 1)
        right, right_ok, right_meta = _run_span(gh, event, mid + timedelta(seconds=1), end, depth + 1)
        left.update(right)
        complete = left_ok and right_ok
        return (left, complete, {'expected': (left_meta.get('expected') or 0) + (right_meta.get('expected') or 0), 'fetched': len(left), 'complete': complete, 'slices': (left_meta.get('slices') or 1) + (right_meta.get('slices') or 1)})
    out = {int(row['id']): row for row in rows if row.get('id')}
    pages = min(max(1, math.ceil(expected / 100)), 10)
    for page in range(2, pages + 1):
        if not gh.ok(8):
            break
        payload = gh.get(f'/repos/{REPO}/actions/runs', {'event': event, 'created': span, 'per_page': 100, 'page': page})
        for row in payload.get('workflow_runs', []):
            if row.get('id'):
                out[int(row['id'])] = row
    complete = len(out) >= expected
    return (out, complete, {'expected': expected, 'fetched': len(out), 'complete': complete, 'slices': 1})

def list_runs(gh: GH, start: datetime, end: datetime):
    out: dict[int, dict[str, Any]] = {}
    coverage: dict[str, Any] = {}
    complete = True
    for event in EVENTS:
        rows, ok, meta = _run_span(gh, event, start, end)
        out.update(rows)
        coverage[event] = meta
        complete &= ok
    return (list(out.values()), complete, coverage)

def list_jobs(gh: GH, run_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 20):
        if not gh.ok():
            break
        payload = gh.get(f'/repos/{REPO}/actions/runs/{run_id}/jobs', {'filter': 'latest', 'per_page': 100, 'page': page})
        rows = payload.get('jobs', [])
        out += rows
        if not rows or len(out) >= int(payload.get('total_count') or len(out)):
            break
    return out

def failed_step_names(job: dict[str, Any]) -> list[str]:
    return [step.get('name') or '' for step in job.get('steps', []) if step.get('conclusion') in FAILURE_CONCLUSIONS]

def classify_failure(gh: GH, job: dict[str, Any], allow_log: bool=True) -> tuple[str, bool, str | None]:
    """Return (reason, is_infra_unavailable, compact_evidence).

    Fast path: deterministic test/build failures are valid CI verdicts and do not
    require log downloads. Logs are fetched only for setup, timeout, or otherwise
    ambiguous failures where infrastructure evidence can change the verdict.
    """
    conclusion = (job.get('conclusion') or '').lower()
    steps = failed_step_names(job)
    meta = ' | '.join([job.get('name') or '', *steps])
    if conclusion == 'startup_failure':
        return ('STARTUP_FAILURE', True, 'job startup_failure')
    if conclusion in {'cancelled', 'action_required', 'stale'}:
        return ('CANCELLED_OR_NONVERDICT', False, None)

    def inspect_log() -> tuple[str, bool, str | None] | None:
        if not allow_log or not gh.ok(4):
            return None
        try:
            log_text = gh.text(f"/repos/{REPO}/actions/jobs/{int(job['id'])}/logs")[-700000:]
        except Exception:
            return None
        if DOWNLOAD_LOG.search(log_text):
            return ('DOWNLOAD', True, 'download/dependency transport failure')
        if NETWORK_LOG.search(log_text):
            return ('NETWORK', True, 'network/DNS/TLS/5xx transport failure')
        if RUNNER_LOG.search(log_text):
            return ('RUNNER', True, 'runner/container/device environment failure')
        return None

    if conclusion == 'timed_out':
        found = inspect_log()
        if found:
            return found
        if SETUP_META.search(meta):
            return ('INFRA_TIMEOUT', True, 'timeout during setup/download/environment')
        if TEST_META.search(meta):
            return ('TEST_TIMEOUT', False, None)
        return ('UNRESOLVED_TIMEOUT', False, None)

    # A normal test/assertion or compile/lint/docs failure is evidence that CI
    # produced a valid verdict about the submitted code, not that CI is down.
    if TEST_META.search(meta):
        return ('TEST_FAILURE', False, None)
    if BUILD_META.search(meta):
        return ('BUILD_OR_CHECK_FAILURE', False, None)

    if SETUP_META.search(meta):
        found = inspect_log()
        if found:
            return found
        return ('UNRESOLVED_SETUP_FAILURE', False, None)

    found = inspect_log()
    if found:
        return found
    return ('CODE_OR_UNKNOWN_FAILURE', False, None)

def run_key(run: dict[str, Any]) -> str:
    return 'workflow::' + (run.get('path') or run.get('name') or 'unknown')

def job_key(workflow: str, name: str) -> str:
    return f'job::{workflow}::{name}'

def ensure(tests: dict[str, Any], key: str, kind: str, workflow: str, name: str):
    return tests.setdefault('tests', {}).setdefault(key, {'kind': kind, 'workflow': workflow, 'name': name, 'probabilistic': False, 'observations': []})

def observe_run(tests: dict[str, Any], state: dict[str, Any], run: dict[str, Any]) -> None:
    conclusion = run.get('conclusion')
    if conclusion not in SUCCESS | FAILURE_CONCLUSIONS:
        return
    run_id = str(run.get('id'))
    if not run_id or run_id == 'None' or run_id in state['seen_run_ids']:
        return
    workflow = run.get('name') or 'Unnamed workflow'
    item = ensure(tests, run_key(run), 'workflow', workflow, '(workflow aggregate)')
    at = run.get('updated_at') or run.get('created_at') or iso_ts(utcnow())
    item['observations'].append({'run_id': run_id, 'at': at, 'outcome': 'success' if conclusion in SUCCESS else 'failure', 'conclusion': conclusion, 'head_sha': run.get('head_sha')})
    state['seen_run_ids'][run_id] = at

def observe_job(tests: dict[str, Any], state: dict[str, Any], workflow: str, job: dict[str, Any], sha: str | None) -> None:
    conclusion = job.get('conclusion')
    name = job.get('name') or 'Unnamed job'
    item = ensure(tests, job_key(workflow, name), 'job', workflow, name)
    if conclusion not in SUCCESS | FAILURE_CONCLUSIONS:
        return
    job_id = str(job.get('id'))
    if not job_id or job_id == 'None' or job_id in state['seen_job_ids']:
        return
    at = job.get('completed_at') or job.get('started_at') or iso_ts(utcnow())
    item['observations'].append({'job_id': job_id, 'at': at, 'outcome': 'success' if conclusion in SUCCESS else 'failure', 'conclusion': conclusion, 'head_sha': sha})
    state['seen_job_ids'][job_id] = at

def recompute(item: dict[str, Any], current: datetime) -> None:
    cutoff = current - timedelta(days=OBS)
    observations = [row for row in item.get('observations', []) if (parse_dt(row.get('at')) or current) >= cutoff][-240:]
    observations.sort(key=lambda row: row.get('at', ''))
    item['observations'] = observations
    sequence = [row['outcome'] for row in observations if row.get('outcome') in {'success', 'failure'}]
    successes = sequence.count('success')
    failures = sequence.count('failure')
    by_sha: dict[str, set[str]] = defaultdict(set)
    if item.get('kind') == 'job':
        for row in observations:
            conclusion = row.get('conclusion')
            sha = row.get('head_sha')
            if not sha:
                continue
            if conclusion == 'success':
                by_sha[sha].add('success')
            elif conclusion in {'failure', 'timed_out'}:
                by_sha[sha].add('failure')
    same_sha_mixed = item.get('kind') == 'job' and any((len(values) > 1 for values in by_sha.values()))
    policy = item.get('kind') == 'job' and is_policy_prob(item.get('workflow'), item.get('name'))
    detected = same_sha_mixed or policy
    old = bool(item.get('probabilistic'))
    if detected and (not old):
        item['first_detected_at'] = iso_ts(current)
        item['probability_reason'] = 'policy_probability_sensitive' if policy else 'same_commit_mixed_outcomes'
    elif policy:
        item['probability_reason'] = 'policy_probability_sensitive'
    item['probabilistic'] = old or detected
    item['samples_30d'] = len(sequence)
    item['successes_30d'] = successes
    item['failures_30d'] = failures
    item['pass_rate_30d'] = round(successes / len(sequence), 4) if sequence else None

def migrate(tests: dict[str, Any], state: dict[str, Any]) -> bool:
    old = int(tests.get('schema_version') or 0)
    state.setdefault('seen_run_ids', {})
    state.setdefault('seen_job_ids', {})
    if old < SCHEMA:
        for item in tests.get('tests', {}).values():
            if item.get('kind') == 'workflow':
                item['probabilistic'] = False
                item.pop('probability_reason', None)
                item.pop('first_detected_at', None)
            elif item.get('probability_reason') == 'policy_probability_sensitive' and (not is_policy_prob(item.get('workflow'), item.get('name'))):
                item['probabilistic'] = False
                item.pop('probability_reason', None)
                item.pop('first_detected_at', None)
        tests['schema_version'] = SCHEMA
        state['schema_version'] = SCHEMA
        return True
    return False

def bucket(hour: datetime) -> dict[str, Any]:
    return {'hour': iso_hour(hour), 'status': 'unknown', 'coverage': 'complete', 'runs': 0, 'jobs': 0, 'workflow_failures': 0, 'infra_failures': 0, 'code_failures': 0, 'ignored_nonverdicts': 0, 'unresolved_failures': 0, 'probabilistic_jobs': 0, 'active_runs': 0, 'reasons': {}, 'failures': [], 'probabilistic': []}

def add_prob(bucket_row: dict[str, Any], key: str, item: dict[str, Any]) -> None:
    if any((row.get('key') == key for row in bucket_row['probabilistic'])):
        return
    bucket_row['probabilistic'].append({'key': key, 'kind': item.get('kind'), 'workflow': item.get('workflow'), 'job': item.get('name'), 'pass_rate_30d': item.get('pass_rate_30d'), 'reason': item.get('probability_reason')})

def prune(state: dict[str, Any], current: datetime) -> None:
    cutoff = current - timedelta(days=OBS + 2)
    for field in ('seen_run_ids', 'seen_job_ids'):
        state[field] = {key: value for key, value in state.get(field, {}).items() if (parse_dt(value) or current) >= cutoff}

def main() -> int:
    current = utcnow()
    history = load(HISTORY, {'schema_version': SCHEMA, 'hours': []})
    tests = load(TESTS, {'schema_version': SCHEMA, 'tests': {}})
    state = load(STATE, {'schema_version': SCHEMA, 'seen_run_ids': {}, 'seen_job_ids': {}})
    legacy = migrate(tests, state)
    bootstrap = legacy or int(history.get('schema_version') or 0) < SCHEMA or (not history.get('hours'))
    hours = BOOT if bootstrap else LOOKBACK
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    buckets: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        buckets[iso_hour(cursor)] = bucket(cursor)
        cursor += timedelta(hours=1)
    gh = GH()
    errors: list[str] = []
    try:
        runs, complete, event_coverage = list_runs(gh, start - timedelta(hours=2), current)
    except Exception as exc:
        runs, complete, event_coverage = ([], False, {})
        errors.append(f'runs: {type(exc).__name__}: {exc}')
    runs = [row for row in runs if is_ci_run(row) and (not (row.get('status') == 'completed' and row.get('conclusion') == 'skipped'))]
    runs.sort(key=lambda row: row.get('updated_at') or row.get('created_at') or '', reverse=True)
    for run in runs:
        observe_run(tests, state, run)
    for item in tests.get('tests', {}).values():
        recompute(item, current)
    by_id: dict[int, dict[str, Any]] = {}
    failed_run_ids: set[int] = set()
    for run in runs:
        run_id = int(run.get('id') or 0)
        if not run_id:
            continue
        by_id[run_id] = run
        created = parse_dt(run.get('created_at'))
        updated = parse_dt(run.get('updated_at'))
        if run.get('status') == 'completed':
            event_time = updated or created
            row = buckets.get(iso_hour(event_time)) if event_time else None
            if not row:
                continue
            row['runs'] += 1
            if run.get('conclusion') in FAILURE_CONCLUSIONS:
                row['workflow_failures'] += 1
                failed_run_ids.add(run_id)
        elif created:
            cursor = max(created.replace(minute=0, second=0, microsecond=0), start)
            while cursor < end:
                if iso_hour(cursor) in buckets:
                    buckets[iso_hour(cursor)]['active_runs'] += 1
                cursor += timedelta(hours=1)
    detail_cut = start if bootstrap else current - timedelta(hours=DETAIL_HOURS)
    candidates = [run for run in runs if run.get('status') == 'completed' and (parse_dt(run.get('updated_at')) or current) >= detail_cut]

    def priority(run: dict[str, Any]) -> tuple[int, float]:
        conclusion = run.get('conclusion')
        name = run.get('name') or ''
        if conclusion in FAILURE_CONCLUSIONS:
            rank = 0
        elif PROB_POLICY.search(name) or re.search('nightly|weekly|benchmark|accuracy|performance', name, re.I):
            rank = 1
        else:
            rank = 2
        return (rank, -(parse_dt(run.get('updated_at')) or current).timestamp())
    candidates.sort(key=priority)
    max_detail = 650 if gh.auth else 24
    cache: dict[int, list[dict[str, Any]]] = {}
    detailed_failed_runs: set[int] = set()
    for run in candidates[:max_detail]:
        if not gh.ok(8):
            break
        run_id = int(run.get('id') or 0)
        try:
            jobs = list_jobs(gh, run_id)
            cache[run_id] = jobs
        except Exception as exc:
            errors.append(f'jobs:{run_id}: {type(exc).__name__}: {exc}')
            continue
        if run_id in failed_run_ids:
            detailed_failed_runs.add(run_id)
        workflow = run.get('name') or 'Unnamed workflow'
        for job in jobs:
            observe_job(tests, state, workflow, job, run.get('head_sha'))
    for item in tests.get('tests', {}).values():
        recompute(item, current)
    unstable_jobs = {key for key, item in tests.get('tests', {}).items() if item.get('kind') == 'job' and item.get('probabilistic')}
    for run_id in failed_run_ids - detailed_failed_runs:
        run = by_id.get(run_id, {})
        event_time = parse_dt(run.get('updated_at')) or parse_dt(run.get('created_at'))
        row = buckets.get(iso_hour(event_time)) if event_time else None
        if row:
            row['unresolved_failures'] += 1
    for run_id, jobs in cache.items():
        run = by_id.get(run_id, {})
        workflow = run.get('name') or 'Unnamed workflow'
        run_bucket = None
        run_time = parse_dt(run.get('updated_at'))
        if run_time:
            run_bucket = buckets.get(iso_hour(run_time))
        run_had_unresolved = False
        for job in jobs:
            event_time = parse_dt(job.get('completed_at')) or parse_dt(job.get('started_at'))
            row = buckets.get(iso_hour(event_time)) if event_time else run_bucket
            if not row:
                continue
            row['jobs'] += 1
            conclusion = (job.get('conclusion') or '').lower()
            key = job_key(workflow, job.get('name') or 'Unnamed job')
            if conclusion in FAILURE_CONCLUSIONS | {'cancelled', 'action_required', 'stale'}:
                why, infra, evidence = classify_failure(gh, job, allow_log=True)
                if infra:
                    row['infra_failures'] += 1
                    row['reasons'][why] = row['reasons'].get(why, 0) + 1
                    if len(row['failures']) < 20:
                        row['failures'].append({'workflow': workflow, 'job': job.get('name') or 'Unnamed job', 'conclusion': conclusion or 'unknown', 'reason': why, 'evidence': evidence, 'failed_steps': failed_step_names(job)[:4], 'url': job.get('html_url') or run.get('html_url')})
                elif why in {'CANCELLED_OR_NONVERDICT'}:
                    row['ignored_nonverdicts'] += 1
                elif why.startswith('UNRESOLVED') or why == 'CODE_OR_UNKNOWN_FAILURE':
                    row['unresolved_failures'] += 1
                    run_had_unresolved = True
                else:
                    row['code_failures'] += 1
            if key in unstable_jobs:
                row['probabilistic_jobs'] += 1
                add_prob(row, key, tests['tests'][key])
        if run_id in failed_run_ids:
            meaningful = [job for job in jobs if (job.get('conclusion') or '').lower() in FAILURE_CONCLUSIONS]
            if not meaningful and run_bucket and (not run_had_unresolved):
                run_bucket['unresolved_failures'] += 1
    for row in buckets.values():
        if not complete:
            row['coverage'] = 'partial'
        if row['infra_failures'] or row['probabilistic_jobs']:
            row['status'] = 'down'
        elif row['coverage'] == 'partial' or row['unresolved_failures']:
            row['status'] = 'unknown'
        elif row['runs']:
            row['status'] = 'healthy'
        else:
            row['status'] = 'unknown'
    old = {row.get('hour'): row for row in history.get('hours', []) if row.get('hour')}
    for key, row in buckets.items():
        if row['runs'] or row['active_runs'] or key not in old or (not errors):
            old[key] = row
    cutoff = current - timedelta(days=RETENTION)
    rows = [value for key, value in sorted(old.items()) if (parse_dt(key) or current) >= cutoff]
    prune(state, current)
    history.update({'schema_version': SCHEMA, 'updated_at': iso_ts(current), 'upstream_repo': REPO, 'policy': {'code_failures_are_valid_ci_verdicts': True, 'infrastructure_failures_are_unavailable': True, 'probability_sensitive_job_presence_is_unavailable': True, 'unresolved_failure_is_unknown': True, 'unknown_excluded_from_availability': True}, 'collector': {'authenticated': gh.auth, 'auth_fallbacks': gh.fallbacks, 'api_requests': gh.requests, 'request_budget': gh.budget, 'run_listing_complete': complete, 'event_coverage': event_coverage, 'detail_runs': len(cache), 'errors': errors[-10:]}, 'hours': rows})
    tests.update({'schema_version': SCHEMA, 'updated_at': iso_ts(current), 'upstream_repo': REPO, 'tests': dict(sorted(tests.get('tests', {}).items(), key=lambda pair: (not bool(pair[1].get('probabilistic')), 0 if pair[1].get('kind') == 'workflow' else 1, pair[0].lower())))})
    state.update({'schema_version': SCHEMA, 'updated_at': iso_ts(current)})
    save(HISTORY, history)
    save(TESTS, tests)
    save(STATE, state)
    counts = Counter((row['status'] for row in buckets.values()))
    print(f'runs={len(runs)} detail_runs={len(cache)} requests={gh.requests}/{gh.budget} auth={gh.auth} complete={complete} buckets={dict(counts)} unstable_jobs={len(unstable_jobs)}')
    for error in errors:
        print('warning:', error, file=sys.stderr)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())

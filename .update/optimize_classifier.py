#!/usr/bin/env python3
from pathlib import Path

p = Path('scripts/collect.py')
s = p.read_text()
start = s.index('def classify_failure(')
end = s.index('\ndef run_key(', start)
new = '''def classify_failure(gh: GH, job: dict[str, Any], allow_log: bool=True) -> tuple[str, bool, str | None]:
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
'''
p.write_text(s[:start] + new + s[end:])
print('optimized classify_failure')

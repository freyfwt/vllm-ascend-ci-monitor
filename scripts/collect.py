#!/usr/bin/env python3
"""Hourly passive monitor for public vLLM-Ascend GitHub Actions CI.

No upstream repository permissions are required. The script reads public workflow
runs/jobs, classifies failures heuristically, detects observed unstable jobs, and
stores compact JSON data for the static dashboard.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

UPSTREAM_REPO = os.getenv("UPSTREAM_REPO", "vllm-project/vllm-ascend")
API_ROOT = "https://api.github.com"
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
HISTORY_PATH = DATA_DIR / "history.json"
TESTS_PATH = DATA_DIR / "tests.json"
STATE_PATH = DATA_DIR / "state.json"

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))
OBSERVATION_DAYS = int(os.getenv("OBSERVATION_DAYS", "30"))
BOOTSTRAP_HOURS = int(os.getenv("BOOTSTRAP_HOURS", "24"))
NORMAL_LOOKBACK_HOURS = int(os.getenv("NORMAL_LOOKBACK_HOURS", "3"))
MAX_RUN_PAGES = int(os.getenv("MAX_RUN_PAGES", "5"))
MAX_API_REQUESTS = int(os.getenv("MAX_API_REQUESTS", "52"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

GOOD_CONCLUSIONS = {"success", "neutral", "skipped"}
BAD_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}
NETWORK_PATTERNS = re.compile(
    r"(network|dns|connection|timeout|timed out|resolve|proxy|socket|http\s*[45]\d\d)",
    re.I,
)
DOWNLOAD_PATTERNS = re.compile(
    r"(install|download|dependency|dependencies|pip|apt|yum|wget|curl|checkout|"
    r"pull image|docker pull|setup python|setup node|setup go|cache)",
    re.I,
)
RUNNER_PATTERNS = re.compile(
    r"(runner|self[- ]hosted|machine|pod|k8s|kubernetes|docker|container|environment|device|npu)",
    re.I,
)

IGNORED_EVENTS = {
    "issue_comment", "issues", "discussion", "discussion_comment",
    "pull_request_review", "pull_request_review_comment", "fork", "watch",
    "create", "delete", "release",
}

TEST_PATTERNS = re.compile(
    r"(test|pytest|unittest|accuracy|acceptance|performance|perf|benchmark|bench|eval|evaluation)",
    re.I,
)

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

def iso_hour(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def iso_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default

def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

class GitHubClient:
    def __init__(self) -> None:
        # Prefer an explicit upstream PAT if the user adds one later. Otherwise use
        # anonymous public access with a conservative request budget.
        self.token = os.getenv("UPSTREAM_GITHUB_TOKEN") or ""
        self.requests = 0
        self.auth_fallbacks = 0

    def _request(self, url: str, use_auth: bool = True) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vllm-ascend-ci-monitor/1.0",
        }
        if use_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # A repository-scoped token may not be accepted for a different public
            # repository. Anonymous access still works for public Actions data.
            if use_auth and self.token and exc.code in (401, 403, 404):
                self.auth_fallbacks += 1
                return self._request(url, use_auth=False)
            raise

    def json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        data = self._request(f"{API_ROOT}{path}{query}")
        return json.loads(data.decode("utf-8"))

def list_recent_runs(
    client: GitHubClient,
    cutoff: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    runs: list[dict[str, Any]] = []
    total_expected: int | None = None
    created_range = f"{iso_ts(cutoff)}..{iso_ts(end)}"
    for page in range(1, MAX_RUN_PAGES + 1):
        if client.requests >= MAX_API_REQUESTS:
            break
        payload = client.json(
            f"/repos/{UPSTREAM_REPO}/actions/runs",
            {"per_page": 100, "page": page, "created": created_range},
        )
        if total_expected is None:
            total_expected = int(payload.get("total_count") or 0)
        page_runs = payload.get("workflow_runs", [])
        if not page_runs:
            break
        runs.extend(page_runs)
        if total_expected is not None and len(runs) >= total_expected:
            break
    dedup = {int(r["id"]): r for r in runs if r.get("id") is not None}
    complete = total_expected is not None and len(dedup) >= total_expected
    return list(dedup.values()), complete

def is_candidate_ci_run(run: dict[str, Any]) -> bool:
    event = (run.get("event") or "").lower()
    if event in IGNORED_EVENTS:
        return False
    # A fully skipped workflow did not execute a CI signal and is not worth one
    # of the limited anonymous job-list API requests.
    if run.get("status") == "completed" and run.get("conclusion") == "skipped":
        return False
    return True

def list_jobs(client: GitHubClient, run_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    total = None
    while client.requests < MAX_API_REQUESTS:
        payload = client.json(
            f"/repos/{UPSTREAM_REPO}/actions/runs/{run_id}/jobs",
            {"per_page": 100, "page": page, "filter": "latest"},
        )
        page_jobs = payload.get("jobs", [])
        jobs.extend(page_jobs)
        total = int(payload.get("total_count") or len(jobs))
        if len(jobs) >= total or not page_jobs:
            break
        page += 1
    return jobs

def conclusion_is_bad(conclusion: str | None) -> bool:
    if conclusion is None:
        return False
    if conclusion in GOOD_CONCLUSIONS:
        return False
    return True

def failed_step_names(job: dict[str, Any]) -> list[str]:
    names = []
    for step in job.get("steps") or []:
        if conclusion_is_bad(step.get("conclusion")):
            names.append(step.get("name") or "")
    return [name for name in names if name]

def classify_job(job: dict[str, Any]) -> str:
    conclusion = (job.get("conclusion") or "").lower()
    if conclusion == "timed_out":
        return "TIMEOUT"
    if conclusion == "cancelled":
        return "CANCELLED"

    text = " | ".join([job.get("name") or "", *failed_step_names(job)])
    # Step names are the only log-like signal that is cheap enough for hourly,
    # anonymous monitoring. Prefer explicit dependency/setup indications.
    if DOWNLOAD_PATTERNS.search(text):
        return "DOWNLOAD"
    if NETWORK_PATTERNS.search(text):
        return "NETWORK"
    if RUNNER_PATTERNS.search(text):
        return "RUNNER"
    if TEST_PATTERNS.search(text):
        return "TEST"
    return "UNKNOWN"

def job_key(workflow: str, job_name: str) -> str:
    return f"{workflow} :: {job_name}"

def recompute_test(test: dict[str, Any], now: datetime) -> None:
    cutoff = now - timedelta(days=OBSERVATION_DAYS)
    obs = [
        o for o in test.get("observations", [])
        if (parse_dt(o.get("at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    obs.sort(key=lambda x: x.get("at", ""))
    # Keep a hard cap too; matrix jobs can be very busy.
    obs = obs[-120:]
    test["observations"] = obs

    successes = sum(1 for o in obs if o.get("outcome") == "success")
    failures = sum(1 for o in obs if o.get("outcome") == "failure")
    total = successes + failures
    transitions = 0
    seq = [o.get("outcome") for o in obs if o.get("outcome") in {"success", "failure"}]
    for a, b in zip(seq, seq[1:]):
        if a != b:
            transitions += 1

    sha_outcomes: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        sha = o.get("head_sha")
        outcome = o.get("outcome")
        if sha and outcome in {"success", "failure"}:
            sha_outcomes[sha].add(outcome)
    same_sha_mixed = any(len(outcomes) > 1 for outcomes in sha_outcomes.values())

    previously_unstable = bool(test.get("probabilistic"))
    detected = same_sha_mixed or (
        total >= 5
        and successes > 0
        and failures > 0
        and transitions >= 2
    )

    if detected and not previously_unstable:
        test["first_detected_at"] = iso_ts(now)
        if same_sha_mixed:
            test["probability_reason"] = "same_commit_mixed_outcomes"
        else:
            test["probability_reason"] = "repeated_pass_fail_transitions"

    # Sticky by design: once public history demonstrates probabilistic/unstable
    # behaviour, future appearances are considered unavailable as requested.
    test["probabilistic"] = previously_unstable or detected
    test["samples_30d"] = total
    test["successes_30d"] = successes
    test["failures_30d"] = failures
    test["pass_rate_30d"] = round(successes / total, 4) if total else None
    test["transitions_30d"] = transitions

def add_observation(
    tests: dict[str, Any],
    state: dict[str, Any],
    workflow: str,
    job: dict[str, Any],
    head_sha: str | None,
    now: datetime,
) -> None:
    job_id = str(job.get("id"))
    if not job_id or job_id == "None":
        return
    seen = state.setdefault("seen_job_ids", {})
    if job_id in seen:
        return

    conclusion = job.get("conclusion")
    if conclusion in GOOD_CONCLUSIONS:
        outcome = "success"
    elif conclusion:
        outcome = "failure"
    else:
        return

    key = job_key(workflow, job.get("name") or "Unnamed job")
    test = tests.setdefault("tests", {}).setdefault(
        key,
        {
            "workflow": workflow,
            "name": job.get("name") or "Unnamed job",
            "probabilistic": False,
            "observations": [],
        },
    )
    at = job.get("completed_at") or job.get("started_at") or iso_ts(now)
    test.setdefault("observations", []).append(
        {
            "job_id": job_id,
            "at": at,
            "outcome": outcome,
            "conclusion": conclusion,
            "head_sha": head_sha,
        }
    )
    seen[job_id] = at

def prune_state(state: dict[str, Any], now: datetime) -> None:
    cutoff = now - timedelta(days=OBSERVATION_DAYS + 2)
    seen = state.get("seen_job_ids", {})
    state["seen_job_ids"] = {
        jid: at
        for jid, at in seen.items()
        if (parse_dt(at) or now) >= cutoff
    }

def make_bucket(hour: datetime) -> dict[str, Any]:
    return {
        "hour": iso_hour(hour),
        "status": "unknown",
        "coverage": "complete",
        "runs": 0,
        "jobs": 0,
        "failed_jobs": 0,
        "probabilistic_jobs": 0,
        "active_runs": 0,
        "reasons": {},
        "failures": [],
        "probabilistic": [],
    }

def bucket_for(dt: datetime, buckets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return buckets.get(iso_hour(dt))

def main() -> int:
    now = utcnow()
    history = load_json(
        HISTORY_PATH,
        {"schema_version": 1, "updated_at": None, "hours": []},
    )
    tests = load_json(
        TESTS_PATH,
        {"schema_version": 1, "updated_at": None, "tests": {}},
    )
    state = load_json(
        STATE_PATH,
        {"schema_version": 1, "seen_job_ids": {}},
    )

    first_run = not history.get("hours")
    lookback_hours = BOOTSTRAP_HOURS if first_run else NORMAL_LOOKBACK_HOURS

    # Only report completed clock hours. This makes every data point comparable.
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    start_hour = end_hour - timedelta(hours=lookback_hours)
    buckets: dict[str, dict[str, Any]] = {}
    cursor = start_hour
    while cursor < end_hour:
        buckets[iso_hour(cursor)] = make_bucket(cursor)
        cursor += timedelta(hours=1)

    client = GitHubClient()
    errors: list[str] = []
    try:
        runs, reached_cutoff = list_recent_runs(client, start_hour - timedelta(hours=2), now)
    except Exception as exc:
        runs = []
        reached_cutoff = False
        errors.append(f"workflow_runs: {type(exc).__name__}: {exc}")

    # Newest first gives the most useful coverage when anonymous API budget is tight.
    runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)

    job_requests = 0
    covered_run_ids: set[int] = set()
    run_ids_seen_in_bucket: dict[str, set[int]] = defaultdict(set)
    jobs_cache: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    for run in runs:
        run_id = int(run.get("id") or 0)
        created = parse_dt(run.get("created_at"))
        updated = parse_dt(run.get("updated_at"))
        run_time = updated or created
        if not run_time:
            continue
        if not is_candidate_ci_run(run):
            continue

        # A still-running workflow that has occupied an earlier hour is visible as
        # degraded evidence. Completed jobs are bucketed more precisely later.
        if run.get("status") != "completed" and created:
            cursor = max(created.replace(minute=0, second=0, microsecond=0), start_hour)
            while cursor < min(end_hour, now):
                b = bucket_for(cursor, buckets)
                if b:
                    b["active_runs"] += 1
                    run_ids_seen_in_bucket[b["hour"]].add(run_id)
                cursor += timedelta(hours=1)

        if run_time < start_hour - timedelta(hours=2):
            continue
        if client.requests >= MAX_API_REQUESTS:
            # Mark the update hour as evidence of partial coverage.
            b = bucket_for(run_time, buckets)
            if b:
                run_ids_seen_in_bucket[b["hour"]].add(run_id)
            continue

        try:
            before = client.requests
            jobs = list_jobs(client, run_id)
            job_requests += client.requests - before
            covered_run_ids.add(run_id)
            jobs_cache[run_id] = (run, jobs)
        except Exception as exc:
            errors.append(f"jobs:{run_id}: {type(exc).__name__}: {exc}")
            b = bucket_for(run_time, buckets)
            if b:
                run_ids_seen_in_bucket[b["hour"]].add(run_id)
            continue

        workflow = run.get("name") or run.get("display_title") or "Unnamed workflow"
        head_sha = run.get("head_sha")
        for job in jobs:
            add_observation(tests, state, workflow, job, head_sha, now)

    # Recompute instability before applying it to hourly status.
    for test in tests.get("tests", {}).values():
        recompute_test(test, now)

    probabilistic_keys = {
        key
        for key, test in tests.get("tests", {}).items()
        if test.get("probabilistic")
    }

    for run_id, (run, jobs) in jobs_cache.items():
        workflow = run.get("name") or run.get("display_title") or "Unnamed workflow"
        run_url = run.get("html_url")
        for job in jobs:
            event_dt = parse_dt(job.get("completed_at")) or parse_dt(job.get("started_at"))
            if not event_dt:
                continue
            b = bucket_for(event_dt, buckets)
            if not b:
                continue

            run_ids_seen_in_bucket[b["hour"]].add(run_id)
            b["jobs"] += 1
            conclusion = job.get("conclusion")
            key = job_key(workflow, job.get("name") or "Unnamed job")

            if conclusion_is_bad(conclusion):
                reason = classify_job(job)
                b["failed_jobs"] += 1
                b["reasons"][reason] = int(b["reasons"].get(reason, 0)) + 1
                if len(b["failures"]) < 20:
                    b["failures"].append(
                        {
                            "workflow": workflow,
                            "job": job.get("name") or "Unnamed job",
                            "conclusion": conclusion or "unknown",
                            "reason": reason,
                            "failed_steps": failed_step_names(job)[:4],
                            "url": job.get("html_url") or run_url,
                        }
                    )

            if key in probabilistic_keys:
                b["probabilistic_jobs"] += 1
                if key not in b["probabilistic"]:
                    test = tests["tests"][key]
                    b["probabilistic"].append(
                        {
                            "key": key,
                            "workflow": workflow,
                            "job": job.get("name") or "Unnamed job",
                            "pass_rate_30d": test.get("pass_rate_30d"),
                            "reason": test.get("probability_reason"),
                        }
                    )

    for hour_key, b in buckets.items():
        b["runs"] = len(run_ids_seen_in_bucket.get(hour_key, set()))
        uncovered = run_ids_seen_in_bucket.get(hour_key, set()) - covered_run_ids
        if uncovered or not reached_cutoff:
            b["coverage"] = "partial"

        if b["failed_jobs"] > 0 or b["probabilistic_jobs"] > 0:
            b["status"] = "down"
        elif b["coverage"] == "partial" and (b["jobs"] > 0 or b["runs"] > 0):
            # Never claim healthy when the public API budget prevented full inspection.
            b["status"] = "unknown"
        elif b["jobs"] > 0:
            b["status"] = "healthy"
        elif b["active_runs"] > 0:
            b["status"] = "degraded"
        else:
            b["status"] = "unknown"

    # Merge recent buckets into history. Do not destroy previously useful evidence
    # when a transient API error returns no data at all.
    existing = {item.get("hour"): item for item in history.get("hours", []) if item.get("hour")}
    for hour_key, b in buckets.items():
        has_evidence = bool(b["jobs"] or b["runs"] or b["active_runs"])
        if has_evidence or hour_key not in existing or not errors:
            existing[hour_key] = b

    retention_cutoff = now - timedelta(days=RETENTION_DAYS)
    merged = [
        item for _, item in sorted(existing.items())
        if (parse_dt(item.get("hour")) or now) >= retention_cutoff
    ]

    prune_state(state, now)
    history.update(
        {
            "schema_version": 1,
            "updated_at": iso_ts(now),
            "upstream_repo": UPSTREAM_REPO,
            "policy": {
                "any_failed_job_is_unavailable": True,
                "probabilistic_job_presence_is_unavailable": True,
                "unknown_excluded_from_availability": True,
            },
            "collector": {
                "api_requests": client.requests,
                "job_requests": job_requests,
                "auth_fallbacks": client.auth_fallbacks,
                "run_pages_complete": reached_cutoff,
                "errors": errors[-10:],
            },
            "hours": merged,
        }
    )
    tests.update(
        {
            "schema_version": 1,
            "updated_at": iso_ts(now),
            "upstream_repo": UPSTREAM_REPO,
            "tests": dict(
                sorted(
                    tests.get("tests", {}).items(),
                    key=lambda kv: (
                        not bool(kv[1].get("probabilistic")),
                        kv[0].lower(),
                    ),
                )
            ),
        }
    )
    state["updated_at"] = iso_ts(now)

    save_json(HISTORY_PATH, history)
    save_json(TESTS_PATH, tests)
    save_json(STATE_PATH, state)

    counts = Counter(item["status"] for item in buckets.values())
    print(
        "Collected "
        f"{len(runs)} runs, {sum(len(v[1]) for v in jobs_cache.values())} jobs, "
        f"{client.requests} API requests; buckets={dict(counts)}; "
        f"probabilistic_jobs={len(probabilistic_keys)}"
    )
    if errors:
        print("Collector warnings:", file=sys.stderr)
        for err in errors[-10:]:
            print(f"- {err}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise

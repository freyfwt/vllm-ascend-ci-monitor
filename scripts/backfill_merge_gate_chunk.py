#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from classify_merge_availability import (
    GATE,
    LogReader,
    classify_gate_without_failed_leaf,
    classify_leaf,
    direct_gate_job,
    list_jobs,
)
from collect import GH, TESTS, iso_hour, iso_ts, load, parse_dt
from run_merge_availability import list_e2e_pr_runs_status, list_failed_e2e_pr_runs

SHARD_SCHEMA = 3
FAIL = {"failure", "timed_out", "startup_failure"}
NONVERDICT = {"skipped", "cancelled", "action_required", "stale", "neutral"}


def parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def job_key(name: str) -> str:
    return f"job::E2E::{name}"


def job_time(job: dict[str, Any], run: dict[str, Any]) -> datetime | None:
    return (
        parse_dt(job.get("completed_at"))
        or parse_dt(job.get("started_at"))
        or parse_dt(run.get("updated_at"))
        or parse_dt(run.get("created_at"))
    )


def seed_day_observations(tests: dict[str, Any], run_jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
    """Augment bounded tests.json with same-SHA required-leaf outcomes near this day."""
    for run, jobs in run_jobs:
        sha = run.get("head_sha")
        if not sha:
            continue
        for job in jobs:
            name = job.get("name") or "Unnamed job"
            if not direct_gate_job(name):
                continue
            conclusion = (job.get("conclusion") or "").lower()
            if conclusion not in {"success"} | FAIL:
                continue
            key = job_key(name)
            item = tests.setdefault("tests", {}).setdefault(
                key,
                {"kind": "job", "workflow": "E2E", "name": name, "observations": []},
            )
            item.setdefault("observations", []).append(
                {
                    "head_sha": sha,
                    "conclusion": conclusion,
                    "outcome": "success" if conclusion == "success" else "failure",
                    "at": job.get("completed_at") or job.get("started_at") or run.get("updated_at"),
                    "job_id": str(job.get("id") or ""),
                }
            )


def blank(hour: datetime) -> dict[str, Any]:
    return {
        "hour": iso_hour(hour),
        "merge_gate_runs": 0,
        "merge_gate_code_failures": 0,
        "merge_gate_policy_failures": 0,
        "merge_blocking_ci_failures": 0,
        "merge_gate_unknown_failures": 0,
        "merge_gate_evidence": [],
    }


def classify_leaf_set(
    gh: GH,
    logs: LogReader,
    tests: dict[str, Any],
    failures: list[dict[str, Any]],
    sha: str | None,
) -> tuple[str, str, str | None]:
    results = [classify_leaf(gh, logs, tests, job, sha) for job in failures]
    if any(item[0] == "ci" for item in results):
        first = next(item for item in results if item[0] == "ci")
        return "ci", first[1], first[2]
    if results and all(item[0] == "code" for item in results):
        return "code", results[0][1], results[0][2]
    first = next(
        (item for item in results if item[0] == "unknown"),
        ("unknown", "MERGE_PATH_FAILURE_UNKNOWN", None),
    )
    return "unknown", first[1], first[2]


def record(
    row: dict[str, Any],
    stats: Counter,
    *,
    kind: str,
    reason: str,
    evidence: str | None,
    run_id: int,
    sha: str | None,
    url: str | None,
    gate_conclusion: str,
) -> None:
    event = {
        "run_id": run_id,
        "sha": sha,
        "kind": kind,
        "reason": reason,
        "evidence": evidence,
        "gate_conclusion": gate_conclusion,
        "url": url,
    }
    if kind == "ci":
        row["merge_blocking_ci_failures"] += 1
        stats["merge_blocking_ci"] += 1
    elif kind == "code":
        row["merge_gate_code_failures"] += 1
        stats["code_verdict"] += 1
    elif kind == "policy":
        row["merge_gate_policy_failures"] += 1
        stats["policy_verdict"] += 1
    else:
        row["merge_gate_unknown_failures"] += 1
        stats["unknown_gate"] += 1
    if len(row["merge_gate_evidence"]) < 20:
        row["merge_gate_evidence"].append(event)


def fetch_jobs(gh: GH, runs: list[dict[str, Any]], errors: list[str]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for run in runs:
        if not gh.ok(10):
            errors.append("API budget exhausted while listing E2E jobs")
            break
        try:
            jobs = list_jobs(gh, int(run.get("id") or 0))
        except Exception as exc:
            errors.append(f"jobs:{run.get('id')}: {type(exc).__name__}: {exc}")
            continue
        out.append((run, jobs))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    day = parse_day(args.date)
    day_end = day + timedelta(days=1)
    scan_start = day - timedelta(hours=12)
    scan_end = day_end + timedelta(hours=12)

    gh = GH()
    tests = load(TESTS, {"tests": {}})
    logs = LogReader(gh.token)
    stats = Counter()
    errors: list[str] = []

    try:
        failed_runs = list_failed_e2e_pr_runs(gh, scan_start, scan_end)
    except Exception as exc:
        failed_runs = []
        errors.append(f"failed-runs: {type(exc).__name__}: {exc}")

    failed_runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")
    failed_shas = {run.get("head_sha") for run in failed_runs if run.get("head_sha")}

    # Same-SHA instability requires seeing the successful rerun too. Query
    # successful E2E run metadata, but fetch jobs only for SHAs that also had a
    # failed E2E run in this local window.
    try:
        success_meta = list_e2e_pr_runs_status(gh, scan_start, scan_end, "success") if failed_shas else []
    except Exception as exc:
        success_meta = []
        errors.append(f"success-runs: {type(exc).__name__}: {exc}")
    matched_success = [run for run in success_meta if run.get("head_sha") in failed_shas]

    failed_run_jobs = fetch_jobs(gh, failed_runs, errors)
    success_run_jobs = fetch_jobs(gh, matched_success, errors)
    seed_day_observations(tests, failed_run_jobs + success_run_jobs)

    hours = {iso_hour(day + timedelta(hours=h)): blank(day + timedelta(hours=h)) for h in range(24)}

    for run, jobs in failed_run_jobs:
        run_id = int(run.get("id") or 0)
        sha = run.get("head_sha")
        gates = [job for job in jobs if (job.get("name") or "").lower() == GATE]
        gate = gates[-1] if gates else None
        gate_conclusion = ((gate or {}).get("conclusion") or "absent").lower()

        leaf_failures = [
            job
            for job in jobs
            if (job.get("conclusion") or "").lower() in FAIL
            and (job.get("name") or "").lower() != GATE
            and direct_gate_job(job.get("name") or "")
        ]

        event_time = job_time(gate, run) if gate else None
        if (not event_time or gate_conclusion in NONVERDICT | {"absent"}) and leaf_failures:
            leaf_times = [job_time(job, run) for job in leaf_failures]
            event_time = max((value for value in leaf_times if value), default=event_time)

        if not event_time or not (day <= event_time < day_end):
            continue
        row = hours[iso_hour(event_time)]
        if gate:
            row["merge_gate_runs"] += 1

        if gate_conclusion == "success":
            # The workflow failed elsewhere but the required merge gate passed;
            # it cannot make this hour unavailable.
            stats["gate_success_in_failed_workflow"] += 1
            continue

        if gate_conclusion in FAIL:
            stats["gate_failure"] += 1
            if leaf_failures:
                kind, reason, evidence = classify_leaf_set(gh, logs, tests, leaf_failures, sha)
            else:
                kind, reason, evidence = classify_gate_without_failed_leaf(gh, logs, gate)
            record(
                row,
                stats,
                kind=kind,
                reason=reason,
                evidence=evidence,
                run_id=run_id,
                sha=sha,
                url=(gate or {}).get("html_url") or run.get("html_url"),
                gate_conclusion=gate_conclusion,
            )
            continue

        if gate_conclusion in NONVERDICT | {"absent"}:
            stats[f"gate_{gate_conclusion}"] += 1
            if leaf_failures:
                stats["nonverdict_with_failed_leaf"] += 1
                kind, reason, evidence = classify_leaf_set(gh, logs, tests, leaf_failures, sha)
                record(
                    row,
                    stats,
                    kind=kind,
                    reason=reason,
                    evidence=evidence,
                    run_id=run_id,
                    sha=sha,
                    url=(gate or {}).get("html_url") or leaf_failures[0].get("html_url") or run.get("html_url"),
                    gate_conclusion=gate_conclusion,
                )
            elif gate_conclusion == "cancelled":
                record(
                    row,
                    stats,
                    kind="unknown",
                    reason="MERGE_GATE_CANCELLED_UNKNOWN",
                    evidence=None,
                    run_id=run_id,
                    sha=sha,
                    url=(gate or {}).get("html_url") or run.get("html_url"),
                    gate_conclusion=gate_conclusion,
                )

    payload = {
        "schema_version": SHARD_SCHEMA,
        "date": args.date,
        "generated_at": iso_ts(datetime.now(timezone.utc)),
        "analysis": {
            "failed_runs_scanned": len(failed_runs),
            "failed_runs_with_jobs": len(failed_run_jobs),
            "matched_success_runs": len(matched_success),
            "matched_success_runs_with_jobs": len(success_run_jobs),
            "api_requests": gh.requests,
            "request_budget": gh.budget,
            "log_reads": logs.used,
            "stats": dict(stats),
            "errors": errors[-20:],
        },
        "hours": [hours[key] for key in sorted(hours)],
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        args.date,
        dict(stats),
        f"failed_runs={len(failed_runs)} failed_jobs={len(failed_run_jobs)} "
        f"matched_success={len(matched_success)} requests={gh.requests}/{gh.budget} logs={logs.used}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

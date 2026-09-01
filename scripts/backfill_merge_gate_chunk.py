#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
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
    list_pr_runs,
)
from collect import GH, TESTS, iso_hour, iso_ts, load, parse_dt


def parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def job_key(name: str) -> str:
    return f"job::E2E::{name}"


def seed_day_observations(tests: dict[str, Any], run_jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
    """Augment tests.json with same-SHA outcomes visible around this UTC day.

    The live tests store is bounded, so an older rerun may have aged out. This
    local augmentation keeps historical same-SHA required-leaf instability
    detectable without mutating the repository's tests.json.
    """
    for run, jobs in run_jobs:
        sha = run.get("head_sha")
        if not sha:
            continue
        for job in jobs:
            name = job.get("name") or "Unnamed job"
            if not direct_gate_job(name):
                continue
            conclusion = (job.get("conclusion") or "").lower()
            if conclusion not in {"success", "failure", "timed_out", "startup_failure"}:
                continue
            key = job_key(name)
            item = tests.setdefault("tests", {}).setdefault(key, {"kind": "job", "workflow": "E2E", "name": name, "observations": []})
            item.setdefault("observations", []).append({
                "head_sha": sha,
                "conclusion": conclusion,
                "outcome": "success" if conclusion == "success" else "failure",
                "at": job.get("completed_at") or job.get("started_at") or run.get("updated_at"),
                "job_id": str(job.get("id") or ""),
            })


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    day = parse_day(args.date)
    day_end = day + timedelta(days=1)
    # Pull-request runs can start before midnight and finish in the target day;
    # also include the next half-day so same-SHA reruns around midnight are seen.
    scan_start = day - timedelta(hours=12)
    scan_end = day_end + timedelta(hours=12)

    gh = GH()
    tests = load(TESTS, {"tests": {}})
    logs = LogReader(gh.token)
    stats = Counter()
    errors: list[str] = []

    try:
        runs = list_pr_runs(gh, scan_start, scan_end)
    except Exception as exc:
        runs = []
        errors.append(f"runs: {type(exc).__name__}: {exc}")

    runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")
    run_jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for run in runs:
        if not gh.ok(10):
            errors.append("API budget exhausted while listing E2E jobs")
            break
        try:
            jobs = list_jobs(gh, int(run.get("id") or 0))
        except Exception as exc:
            errors.append(f"jobs:{run.get('id')}: {type(exc).__name__}: {exc}")
            continue
        run_jobs.append((run, jobs))

    seed_day_observations(tests, run_jobs)

    hours = {iso_hour(day + timedelta(hours=h)): blank(day + timedelta(hours=h)) for h in range(24)}

    for run, jobs in run_jobs:
        run_id = int(run.get("id") or 0)
        sha = run.get("head_sha")
        gates = [job for job in jobs if (job.get("name") or "").lower() == GATE]
        if not gates:
            continue
        gate = gates[-1]
        gate_time = (
            parse_dt(gate.get("completed_at"))
            or parse_dt(gate.get("started_at"))
            or parse_dt(run.get("updated_at"))
            or parse_dt(run.get("created_at"))
        )
        if not gate_time or not (day <= gate_time < day_end):
            continue

        row = hours[iso_hour(gate_time)]
        row["merge_gate_runs"] += 1
        gate_conclusion = (gate.get("conclusion") or "").lower()
        if gate_conclusion == "success":
            stats["gate_success"] += 1
            continue
        if gate_conclusion not in {"failure", "timed_out", "startup_failure"}:
            stats[f"gate_{gate_conclusion or 'none'}"] += 1
            continue

        stats["gate_failure"] += 1
        leaf_failures = [
            job
            for job in jobs
            if (job.get("conclusion") or "").lower() in {"failure", "timed_out", "startup_failure"}
            and (job.get("name") or "").lower() != GATE
            and direct_gate_job(job.get("name") or "")
        ]
        results = [classify_leaf(gh, logs, tests, job, sha) for job in leaf_failures]

        if any(item[0] == "ci" for item in results):
            kind = "ci"
            first = next(item for item in results if item[0] == "ci")
            reason, evidence = first[1], first[2]
        elif results and all(item[0] == "code" for item in results):
            kind = "code"
            reason, evidence = results[0][1], results[0][2]
        elif not leaf_failures:
            kind, reason, evidence = classify_gate_without_failed_leaf(gh, logs, gate)
        else:
            kind = "unknown"
            first = next((item for item in results if item[0] == "unknown"), ("unknown", "MERGE_PATH_FAILURE_UNKNOWN", None))
            reason, evidence = first[1], first[2]

        event = {
            "run_id": run_id,
            "sha": sha,
            "kind": kind,
            "reason": reason,
            "evidence": evidence,
            "url": gate.get("html_url") or run.get("html_url"),
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

    payload = {
        "schema_version": 1,
        "date": args.date,
        "generated_at": iso_ts(datetime.now(timezone.utc)),
        "analysis": {
            "runs_scanned": len(runs),
            "runs_with_jobs": len(run_jobs),
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
    print(args.date, dict(stats), f"runs={len(runs)} jobs={len(run_jobs)} requests={gh.requests}/{gh.budget} logs={logs.used}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

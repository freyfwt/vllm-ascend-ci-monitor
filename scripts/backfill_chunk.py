#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collect import (
    GH, FAILURE_CONCLUSIONS, SUCCESS, classify_failure, failed_step_names,
    is_ci_run, is_policy_prob, job_key, list_jobs, list_runs, load, parse_dt,
)

TESTS = Path("data/tests.json")

def hour_bucket(date: str, hour: int) -> dict:
    return {
        "hour": f"{date}T{hour:02d}:00:00Z", "status": "unknown", "coverage": "complete",
        "runs": 0, "jobs": 0, "workflow_failures": 0, "infra_failures": 0,
        "code_failures": 0, "ignored_nonverdicts": 0, "unresolved_failures": 0,
        "probabilistic_jobs": 0, "active_runs": 0, "reasons": {}, "failures": [], "probabilistic": [],
    }

def iso_hour(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--start-hour", type=int, required=True)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    start = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc) + timedelta(hours=args.start_hour)
    now = datetime.now(timezone.utc)
    end = min(start + timedelta(hours=args.hours), now.replace(minute=0, second=0, microsecond=0))
    if end <= start:
        Path(args.output).write_text(json.dumps({"date": args.date, "start_hour": args.start_hour, "hours": []}) + "\n")
        return 0
    buckets = {}
    cursor = start
    while cursor < end:
        buckets[iso_hour(cursor)] = hour_bucket(args.date, cursor.hour)
        cursor += timedelta(hours=1)
    tests = load(TESTS, {"tests": {}})
    known_unstable = {key for key, item in tests.get("tests", {}).items()
                      if item.get("kind") == "job" and item.get("probabilistic")}
    gh = GH()
    errors = []
    try:
        runs, complete, coverage = list_runs(gh, start - timedelta(hours=12), end)
    except Exception as exc:
        runs, complete, coverage = [], False, {}
        errors.append(f"runs: {type(exc).__name__}: {exc}")
    runs = [r for r in runs if is_ci_run(r) and not (r.get("status") == "completed" and r.get("conclusion") == "skipped")]
    runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")
    candidate_runs = []
    failed_runs = set()
    for run in runs:
        rid = int(run.get("id") or 0)
        created = parse_dt(run.get("created_at"))
        updated = parse_dt(run.get("updated_at"))
        if not rid or not created or run.get("status") != "completed":
            continue
        if (updated or created) < start or created >= end:
            continue
        candidate_runs.append(run)
        event = updated or created
        key = iso_hour(event)
        if key in buckets:
            buckets[key]["runs"] += 1
            if run.get("conclusion") in FAILURE_CONCLUSIONS:
                buckets[key]["workflow_failures"] += 1
                failed_runs.add(rid)
    outcomes = defaultdict(set)
    occurrences = defaultdict(list)
    detailed_failed = set()
    for run in candidate_runs:
        if not gh.ok(8):
            break
        rid = int(run["id"])
        try:
            jobs = list_jobs(gh, rid)
        except Exception as exc:
            errors.append(f"jobs:{rid}: {type(exc).__name__}: {exc}")
            continue
        if rid in failed_runs:
            detailed_failed.add(rid)
        workflow = run.get("name") or "Unnamed workflow"
        run_event = parse_dt(run.get("updated_at")) or parse_dt(run.get("created_at"))
        run_bucket = buckets.get(iso_hour(run_event)) if run_event else None
        unresolved_for_run = False
        for job in jobs:
            event = parse_dt(job.get("completed_at")) or parse_dt(job.get("started_at"))
            if not event:
                continue
            hour_key = iso_hour(event)
            row = buckets.get(hour_key)
            if not row:
                continue
            row["jobs"] += 1
            conclusion = (job.get("conclusion") or "").lower()
            name = job.get("name") or "Unnamed job"
            key = job_key(workflow, name)
            occurrences[key].append(hour_key)
            sha = run.get("head_sha")
            if conclusion in SUCCESS and sha:
                outcomes[(key, sha)].add("success")
            elif conclusion in {"failure", "timed_out"} and sha:
                outcomes[(key, sha)].add("failure")
            policy_prob = is_policy_prob(workflow, name)
            if policy_prob or key in known_unstable:
                if not any(item.get("key") == key for item in row["probabilistic"]):
                    row["probabilistic"].append({
                        "key": key, "kind": "job", "workflow": workflow, "job": name,
                        "pass_rate_30d": tests.get("tests", {}).get(key, {}).get("pass_rate_30d"),
                        "reason": "policy_probability_sensitive" if policy_prob else
                                  tests.get("tests", {}).get(key, {}).get("probability_reason", "same_commit_mixed_outcomes"),
                    })
                    row["probabilistic_jobs"] += 1
            if conclusion in FAILURE_CONCLUSIONS | {"cancelled", "action_required", "stale"}:
                why, infra, evidence = classify_failure(gh, job, allow_log=True)
                if infra:
                    row["infra_failures"] += 1
                    row["reasons"][why] = row["reasons"].get(why, 0) + 1
                    if len(row["failures"]) < 20:
                        row["failures"].append({
                            "workflow": workflow, "job": name, "conclusion": conclusion,
                            "reason": why, "evidence": evidence, "failed_steps": failed_step_names(job)[:4],
                            "url": job.get("html_url") or run.get("html_url"),
                        })
                elif why == "CANCELLED_OR_NONVERDICT":
                    row["ignored_nonverdicts"] += 1
                elif why.startswith("UNRESOLVED") or why == "CODE_OR_UNKNOWN_FAILURE":
                    row["unresolved_failures"] += 1
                    unresolved_for_run = True
                else:
                    row["code_failures"] += 1
        if rid in failed_runs:
            meaningful = [j for j in jobs if (j.get("conclusion") or "").lower() in FAILURE_CONCLUSIONS]
            if not meaningful and run_bucket and not unresolved_for_run:
                run_bucket["unresolved_failures"] += 1
    for rid in failed_runs - detailed_failed:
        run = next((r for r in candidate_runs if int(r.get("id") or 0) == rid), None)
        if run:
            event = parse_dt(run.get("updated_at")) or parse_dt(run.get("created_at"))
            key = iso_hour(event) if event else None
            if key in buckets:
                buckets[key]["unresolved_failures"] += 1
    local_flaky = {key for (key, _sha), values in outcomes.items() if len(values) > 1}
    for key in local_flaky:
        for hour_key in set(occurrences.get(key, [])):
            row = buckets.get(hour_key)
            if row and not any(item.get("key") == key for item in row["probabilistic"]):
                row["probabilistic_jobs"] += 1
                parts = key.split("::", 2)
                row["probabilistic"].append({"key": key, "kind": "job", "workflow": parts[1],
                                             "job": parts[2], "pass_rate_30d": None,
                                             "reason": "same_commit_mixed_outcomes"})
    for row in buckets.values():
        if not complete:
            row["coverage"] = "partial"
        if row["infra_failures"] or row["probabilistic_jobs"]:
            row["status"] = "down"
        elif row["coverage"] == "partial" or row["unresolved_failures"]:
            row["status"] = "unknown"
        elif row["runs"]:
            row["status"] = "healthy"
        else:
            row["status"] = "unknown"
    output = {
        "schema_version": 2, "date": args.date, "start_hour": args.start_hour,
        "complete": complete, "coverage": coverage, "api_requests": gh.requests,
        "errors": errors[-10:], "hours": [buckets[k] for k in sorted(buckets)],
        "job_outcomes": [
            {"key": key, "sha": sha, "outcomes": sorted(values)}
            for (key, sha), values in sorted(outcomes.items())
        ],
        "job_occurrences": [
            {"key": key, "hours": sorted(set(values))}
            for key, values in sorted(occurrences.items())
        ],
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"{args.date} {args.start_hour:02d}:00 hours={len(output['hours'])} runs={len(candidate_runs)} requests={gh.requests} complete={complete}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

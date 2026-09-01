#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
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
    is_e2e_pr_run,
)
from collect import GH, REPO, TESTS, iso_hour, iso_ts, load, parse_dt

SHARD_SCHEMA = 5
FAIL = {"failure", "timed_out", "startup_failure"}
NONVERDICT = {"skipped", "cancelled", "action_required", "stale", "neutral"}
SCAN_BACK_HOURS = 24


def parse_start(value: str) -> datetime:
    parsed = parse_dt(value)
    if not parsed:
        raise argparse.ArgumentTypeError("expected ISO-8601 UTC datetime")
    return parsed.astimezone(timezone.utc)


def job_key(name: str) -> str:
    return f"job::E2E::{name}"


def job_time(job: dict[str, Any], run: dict[str, Any]) -> datetime | None:
    return (
        parse_dt(job.get("completed_at"))
        or parse_dt(job.get("started_at"))
        or parse_dt(run.get("updated_at"))
        or parse_dt(run.get("created_at"))
    )


def run_interval(run: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = parse_dt(run.get("run_started_at")) or parse_dt(run.get("created_at"))
    end = parse_dt(run.get("updated_at")) or start
    if start and end and end < start:
        end = start
    return start, end


def overlap_hour_keys(run: dict[str, Any], start: datetime, end: datetime) -> list[str]:
    run_start, run_end = run_interval(run)
    if not run_start or not run_end or run_start >= end or run_end < start:
        return []
    cursor = max(run_start, start).replace(minute=0, second=0, microsecond=0)
    last = min(run_end, end - timedelta(microseconds=1))
    keys: list[str] = []
    while cursor <= last:
        keys.append(iso_hour(cursor))
        cursor += timedelta(hours=1)
    return keys


def seed_observations(tests: dict[str, Any], run_jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
    """Augment bounded tests.json with required-leaf failures visible in this shard."""
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
        "merge_gate_analyzed": False,
        "merge_gate_runs": 0,
        "merge_gate_code_failures": 0,
        "merge_gate_policy_failures": 0,
        "merge_blocking_ci_failures": 0,
        "merge_gate_unknown_failures": 0,
        "merge_gate_evidence": [],
    }


def _list_status_span(
    gh: GH,
    start: datetime,
    end: datetime,
    status: str,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """List repository PR runs for one conclusion with explicit completeness."""
    if not gh.ok(6):
        return [], False, {"status": status, "expected": None, "fetched": 0, "complete": False, "slices": 0}

    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params: dict[str, Any] = {
        "event": "pull_request",
        "status": status,
        "created": span,
        "per_page": 100,
        "page": 1,
    }
    first = gh.get(f"/repos/{REPO}/actions/runs", params)
    expected = int(first.get("total_count") or 0)

    # GitHub caps pagination at 1000 results. Split dense ranges rather than
    # silently treating a truncated result as complete.
    if expected > 950 and end - start > timedelta(minutes=30) and depth < 10:
        mid = start + (end - start) / 2
        left, left_ok, left_meta = _list_status_span(gh, start, mid, status, depth + 1)
        right, right_ok, right_meta = _list_status_span(
            gh, mid + timedelta(seconds=1), end, status, depth + 1
        )
        dedup = {int(row["id"]): row for row in left + right if row.get("id")}
        return list(dedup.values()), left_ok and right_ok, {
            "status": status,
            "expected": (left_meta.get("expected") or 0) + (right_meta.get("expected") or 0),
            "fetched": len(dedup),
            "complete": left_ok and right_ok,
            "slices": (left_meta.get("slices") or 1) + (right_meta.get("slices") or 1),
        }

    all_rows: dict[int, dict[str, Any]] = {
        int(row["id"]): row for row in first.get("workflow_runs", []) if row.get("id")
    }
    pages = min(10, max(1, math.ceil(expected / 100)))
    for page in range(2, pages + 1):
        if not gh.ok(6):
            break
        params["page"] = page
        payload = gh.get(f"/repos/{REPO}/actions/runs", params)
        for row in payload.get("workflow_runs", []):
            if row.get("id"):
                all_rows[int(row["id"])] = row

    complete = len(all_rows) >= expected
    rows = [row for row in all_rows.values() if row.get("status") == "completed" and is_e2e_pr_run(row)]
    return rows, complete, {
        "status": status,
        "expected": expected,
        "fetched": len(all_rows),
        "e2e": len(rows),
        "complete": complete,
        "slices": 1,
    }


def list_failed_runs(
    gh: GH, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    complete = True
    for status in ("failure", "timed_out", "startup_failure"):
        try:
            part, ok, meta = _list_status_span(gh, start, end, status)
        except Exception as exc:
            part, ok = [], False
            meta = {
                "status": status,
                "expected": None,
                "fetched": 0,
                "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        rows.extend(part)
        coverage.append(meta)
        complete &= ok
        if not gh.ok(6):
            complete = False
            break
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return list(dedup.values()), complete, coverage


def list_jobs_complete(gh: GH, run_id: int) -> tuple[list[dict[str, Any]], bool]:
    if not gh.ok(4):
        return [], False
    out: dict[int, dict[str, Any]] = {}
    expected: int | None = None
    for page in range(1, 10):
        if not gh.ok(4):
            break
        payload = gh.get(
            f"/repos/{REPO}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": 100, "page": page},
        )
        if expected is None:
            expected = int(payload.get("total_count") or 0)
        part = payload.get("jobs", [])
        for job in part:
            if job.get("id"):
                out[int(job["id"])] = job
        if not part or len(out) >= (expected or len(out)):
            break
    return list(out.values()), expected is not None and len(out) >= expected


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=parse_start, help="UTC window start")
    ap.add_argument("--date", help="compatibility: UTC date YYYY-MM-DD")
    ap.add_argument("--hours", type=int)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.start:
        target_start = args.start.replace(minute=0, second=0, microsecond=0)
        target_hours = args.hours or 4
    elif args.date:
        target_start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        target_hours = args.hours or 24
    else:
        raise SystemExit("--start or --date is required")
    if target_hours < 1 or target_hours > 24:
        raise SystemExit("--hours must be between 1 and 24")

    target_end = target_start + timedelta(hours=target_hours)
    scan_start = target_start - timedelta(hours=SCAN_BACK_HOURS)
    scan_end = target_end

    gh = GH()
    tests = load(TESTS, {"tests": {}})
    logs = LogReader(gh.token)
    stats = Counter()
    errors: list[str] = []

    failed_runs, failed_query_complete, query_coverage = list_failed_runs(gh, scan_start, scan_end)
    failed_runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")

    candidates = [run for run in failed_runs if overlap_hour_keys(run, target_start, target_end)]
    hours = {
        iso_hour(target_start + timedelta(hours=h)): blank(target_start + timedelta(hours=h))
        for h in range(target_hours)
    }
    unresolved: dict[str, set[int]] = {key: set() for key in hours}
    for run in candidates:
        run_id = int(run.get("id") or 0)
        for key in overlap_hour_keys(run, target_start, target_end):
            if key in unresolved:
                unresolved[key].add(run_id)

    # Fetch all candidate job metadata before spending remaining API budget on
    # annotations/log diagnosis. This maximizes the number of hours whose
    # absence of a hidden merge blocker can be proven.
    run_jobs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for run in candidates:
        run_id = int(run.get("id") or 0)
        try:
            jobs, jobs_complete = list_jobs_complete(gh, run_id)
        except Exception as exc:
            jobs, jobs_complete = [], False
            errors.append(f"jobs:{run_id}: {type(exc).__name__}: {exc}")
        if not jobs_complete:
            errors.append(f"jobs:{run_id}: incomplete")
            continue
        run_jobs.append((run, jobs))
        for key in overlap_hour_keys(run, target_start, target_end):
            unresolved.get(key, set()).discard(run_id)

    seed_observations(tests, run_jobs)

    for run, jobs in run_jobs:
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

        if not event_time or not (target_start <= event_time < target_end):
            continue
        row = hours[iso_hour(event_time)]
        if gate:
            row["merge_gate_runs"] += 1

        if gate_conclusion == "success":
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

    for key, row in hours.items():
        row["merge_gate_analyzed"] = bool(failed_query_complete and not unresolved[key])

    analyzed_hours = sum(1 for row in hours.values() if row["merge_gate_analyzed"])
    payload = {
        "schema_version": SHARD_SCHEMA,
        "window_start": iso_ts(target_start),
        "window_hours": target_hours,
        "generated_at": iso_ts(datetime.now(timezone.utc)),
        "analysis": {
            "complete": analyzed_hours == target_hours,
            "failed_query_complete": failed_query_complete,
            "query_coverage": query_coverage,
            "failed_runs_scanned": len(failed_runs),
            "candidate_runs": len(candidates),
            "candidate_runs_with_complete_jobs": len(run_jobs),
            "analyzed_hours": analyzed_hours,
            "unresolved_by_hour": {key: len(value) for key, value in unresolved.items() if value},
            "scan_back_hours": SCAN_BACK_HOURS,
            "api_requests": gh.requests,
            "request_budget": gh.budget,
            "log_reads": logs.used,
            "stats": dict(stats),
            "errors": errors[-40:],
        },
        "hours": [hours[key] for key in sorted(hours)],
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        iso_ts(target_start),
        dict(stats),
        f"query_complete={failed_query_complete} candidates={len(candidates)} jobs={len(run_jobs)} "
        f"analyzed={analyzed_hours}/{target_hours} requests={gh.requests}/{gh.budget} logs={logs.used}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

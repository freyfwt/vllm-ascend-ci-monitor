#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import classify_merge_availability as merge
from collect import GH, REPO, iso_ts, load, parse_dt, save

ENDPOINT = f"/repos/{REPO}/actions/runs"
HISTORY = Path("data/history.json")
SCAN_COMPLETE = True


def mark_incomplete() -> None:
    global SCAN_COMPLETE
    SCAN_COMPLETE = False


def list_e2e_pr_runs_status(gh: GH, start, end, status: str) -> list[dict[str, Any]]:
    """List one conclusion class and track whether pagination was exhaustive."""
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params: dict[str, Any] = {
        "event": "pull_request",
        "status": status,
        "created": span,
        "per_page": 100,
        "page": 1,
    }
    first = gh.get(ENDPOINT, params)
    expected = int(first.get("total_count") or 0)
    all_rows: dict[int, dict[str, Any]] = {
        int(row["id"]): row for row in first.get("workflow_runs", []) if row.get("id")
    }
    pages = min(10, max(1, (expected + 99) // 100))
    for page in range(2, pages + 1):
        if not gh.ok(8):
            mark_incomplete()
            break
        params["page"] = page
        payload = gh.get(ENDPOINT, params)
        for row in payload.get("workflow_runs", []):
            if row.get("id"):
                all_rows[int(row["id"])] = row
    if expected > 1000 or len(all_rows) < expected:
        mark_incomplete()
    return [
        row
        for row in all_rows.values()
        if row.get("status") == "completed" and merge.is_e2e_pr_run(row)
    ]


def list_failed_e2e_pr_runs(gh: GH, start, end) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for status in ("failure", "timed_out", "startup_failure"):
        if not gh.ok(8):
            mark_incomplete()
            break
        try:
            rows.extend(list_e2e_pr_runs_status(gh, start, end, status))
        except Exception:
            mark_incomplete()
            raise
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return list(dedup.values())


def list_jobs_checked(gh: GH, run_id: int) -> list[dict[str, Any]]:
    """Fetch the latest job attempt exhaustively or make the live scan gray."""
    if not gh.ok(6):
        mark_incomplete()
        return []
    out: dict[int, dict[str, Any]] = {}
    expected: int | None = None
    for page in range(1, 10):
        if not gh.ok(6):
            mark_incomplete()
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
    if expected is None or len(out) < expected:
        mark_incomplete()
    return list(out.values())


def main() -> int:
    global SCAN_COMPLETE
    SCAN_COMPLETE = True

    # Hourly availability only needs failed E2E workflows, but every returned
    # candidate and its job list must be exhaustively inspected before a no-fault
    # hour is allowed to become green.
    merge.list_pr_runs = list_failed_e2e_pr_runs
    merge.list_jobs = list_jobs_checked

    now = datetime.now(timezone.utc)
    floor = (now - timedelta(hours=merge.LOOKBACK)).replace(minute=0, second=0, microsecond=0)
    history = load(HISTORY, {"hours": []})
    before = history.get("merge_availability_updated_at")

    # Clear the green permission first. If the causal scan is partial or fails,
    # reapply_merge_status.py will keep these hours gray; explicit red evidence
    # still overrides this guard.
    for row in history.get("hours", []):
        dt = parse_dt(row.get("hour"))
        if dt and dt >= floor:
            row["merge_gate_analyzed"] = False
    save(HISTORY, history)

    rc = merge.main()
    history = load(HISTORY, {"hours": []})
    after = history.get("merge_availability_updated_at")

    if SCAN_COMPLETE and after and after != before:
        for row in history.get("hours", []):
            dt = parse_dt(row.get("hour"))
            if dt and dt >= floor:
                row["merge_gate_analyzed"] = True
    history["merge_gate_live_scan_complete"] = bool(SCAN_COMPLETE and after and after != before)
    history["schema_version"] = max(12, int(history.get("schema_version") or 0))
    save(HISTORY, history)
    print(f"merge-gate live_scan_complete={history['merge_gate_live_scan_complete']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import classify_merge_availability as merge
from collect import GH, REPO, iso_ts, load, parse_dt, save

ENDPOINT = f"/repos/{REPO}/actions/runs"
HISTORY = Path("data/history.json")


def list_e2e_pr_runs_status(gh: GH, start, end, status: str) -> list[dict[str, Any]]:
    """List one conclusion class from repository-wide PR runs, then keep E2E."""
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
    rows = list(first.get("workflow_runs", []))
    pages = min(10, max(1, (expected + 99) // 100))
    for page in range(2, pages + 1):
        if not gh.ok(8):
            break
        params["page"] = page
        payload = gh.get(ENDPOINT, params)
        rows.extend(payload.get("workflow_runs", []))
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return [
        row
        for row in dedup.values()
        if row.get("status") == "completed" and merge.is_e2e_pr_run(row)
    ]


def list_failed_e2e_pr_runs(gh: GH, start, end) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for status in ("failure", "timed_out", "startup_failure"):
        if not gh.ok(8):
            break
        rows.extend(list_e2e_pr_runs_status(gh, start, end, status))
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return list(dedup.values())


def main() -> int:
    # Hourly availability only needs E2E workflows capable of turning an hour
    # red/gray. Generic collector activity proves working CI for green hours.
    merge.list_pr_runs = list_failed_e2e_pr_runs

    before = load(HISTORY, {}).get("merge_availability_updated_at")
    rc = merge.main()
    history = load(HISTORY, {"hours": []})
    after = history.get("merge_availability_updated_at")

    # merge.main updates this timestamp only after a successful causal scan.
    # Old, never-replayed history must not become green merely because it has
    # ordinary CI runs.
    if after and after != before:
        now = datetime.now(timezone.utc)
        floor = now - timedelta(hours=merge.LOOKBACK)
        for row in history.get("hours", []):
            dt = parse_dt(row.get("hour"))
            if dt and dt >= floor:
                row["merge_gate_analyzed"] = True
        history["schema_version"] = max(12, int(history.get("schema_version") or 0))
        save(HISTORY, history)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

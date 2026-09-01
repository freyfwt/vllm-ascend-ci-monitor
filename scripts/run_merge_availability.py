#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

import classify_merge_availability as merge
from collect import GH, REPO, iso_ts

ENDPOINT = f"/repos/{REPO}/actions/workflows/pr_test.yaml/runs"


def _list(gh: GH, start, end, status: str | None = None) -> list[dict[str, Any]]:
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params: dict[str, Any] = {
        "event": "pull_request",
        "created": span,
        "per_page": 100,
        "page": 1,
    }
    if status:
        params["status"] = status
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
    return rows


def list_e2e_pr_runs(gh: GH, start, end) -> list[dict[str, Any]]:
    """All completed E2E PR runs; used by historical same-SHA replay."""
    rows = _list(gh, start, end)
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return [row for row in dedup.values() if row.get("status") == "completed"]


def list_failed_e2e_pr_runs(gh: GH, start, end) -> list[dict[str, Any]]:
    """Only E2E runs that can contain a merge-blocking failure.

    Hourly availability does not need to enumerate successful E2E jobs: generic
    CI activity already proves that the hour had working CI. This keeps the
    causal scan focused on runs that might turn the hour red or gray.
    """
    rows: list[dict[str, Any]] = []
    for status in ("failure", "timed_out", "startup_failure"):
        if not gh.ok(8):
            break
        rows.extend(_list(gh, start, end, status=status))
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return [row for row in dedup.values() if row.get("status") == "completed"]


def main() -> int:
    merge.list_pr_runs = list_failed_e2e_pr_runs
    return merge.main()


if __name__ == "__main__":
    raise SystemExit(main())

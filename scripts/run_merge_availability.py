#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

import classify_merge_availability as merge
from collect import GH, REPO, iso_ts


def list_e2e_pr_runs(gh: GH, start, end) -> list[dict[str, Any]]:
    """List only pr_test.yaml runs instead of every pull_request workflow."""
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params = {
        "event": "pull_request",
        "created": span,
        "per_page": 100,
        "page": 1,
    }
    endpoint = f"/repos/{REPO}/actions/workflows/pr_test.yaml/runs"
    first = gh.get(endpoint, params)
    expected = int(first.get("total_count") or 0)
    rows = list(first.get("workflow_runs", []))
    pages = min(10, max(1, (expected + 99) // 100))
    for page in range(2, pages + 1):
        if not gh.ok(8):
            break
        params["page"] = page
        payload = gh.get(endpoint, params)
        rows.extend(payload.get("workflow_runs", []))
    dedup = {int(row["id"]): row for row in rows if row.get("id")}
    return [row for row in dedup.values() if row.get("status") == "completed"]


def main() -> int:
    merge.list_pr_runs = list_e2e_pr_runs
    return merge.main()


if __name__ == "__main__":
    raise SystemExit(main())

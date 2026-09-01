#!/usr/bin/env python3
from __future__ import annotations

import math
import os
from datetime import timedelta
from typing import Any

import backfill_merge_gate_chunk as base
from collect import REPO, iso_ts

E2E_WORKFLOW_ID = int(os.getenv("E2E_WORKFLOW_ID", "280054652"))


def list_status_span(
    gh,
    start,
    end,
    status: str,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """List recent runs from the current E2E workflow only.

    The general historical replay has to search repository-wide because old
    workflow identities changed. The recent seven-day replay does not: using
    the current workflow id avoids spending the anonymous API budget on every
    unrelated pull-request workflow.
    """
    if not gh.ok(6):
        return [], False, {
            "status": status,
            "expected": None,
            "fetched": 0,
            "complete": False,
            "slices": 0,
            "workflow_id": E2E_WORKFLOW_ID,
        }

    endpoint = f"/repos/{REPO}/actions/workflows/{E2E_WORKFLOW_ID}/runs"
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params: dict[str, Any] = {
        "event": "pull_request",
        "status": status,
        "created": span,
        "per_page": 100,
        "page": 1,
    }
    first = gh.get(endpoint, params)
    expected = int(first.get("total_count") or 0)

    if expected > 950 and end - start > timedelta(minutes=30) and depth < 10:
        mid = start + (end - start) / 2
        left, left_ok, left_meta = list_status_span(gh, start, mid, status, depth + 1)
        right, right_ok, right_meta = list_status_span(
            gh, mid + timedelta(seconds=1), end, status, depth + 1
        )
        dedup = {int(row["id"]): row for row in left + right if row.get("id")}
        return list(dedup.values()), left_ok and right_ok, {
            "status": status,
            "expected": (left_meta.get("expected") or 0) + (right_meta.get("expected") or 0),
            "fetched": len(dedup),
            "complete": left_ok and right_ok,
            "slices": (left_meta.get("slices") or 1) + (right_meta.get("slices") or 1),
            "workflow_id": E2E_WORKFLOW_ID,
        }

    rows: dict[int, dict[str, Any]] = {
        int(row["id"]): row
        for row in first.get("workflow_runs", [])
        if row.get("id")
    }
    pages = min(10, max(1, math.ceil(expected / 100)))
    for page in range(2, pages + 1):
        if not gh.ok(6):
            break
        params["page"] = page
        payload = gh.get(endpoint, params)
        for row in payload.get("workflow_runs", []):
            if row.get("id"):
                rows[int(row["id"])] = row

    complete = len(rows) >= expected
    completed = [row for row in rows.values() if row.get("status") == "completed"]
    return completed, complete, {
        "status": status,
        "expected": expected,
        "fetched": len(rows),
        "e2e": len(completed),
        "complete": complete,
        "slices": 1,
        "workflow_id": E2E_WORKFLOW_ID,
    }


def main() -> int:
    base._list_status_span = list_status_span
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())

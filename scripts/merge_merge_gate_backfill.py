#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from collect import load, save
from reapply_merge_status import apply

HISTORY = Path("data/history.json")
REQUIRED_SHARD_SCHEMA = 6
FIELDS = (
    "merge_gate_runs",
    "merge_gate_code_failures",
    "merge_gate_policy_failures",
    "merge_blocking_ci_failures",
    "merge_gate_unknown_failures",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()

    history = load(HISTORY, {"hours": []})
    by_hour = {row.get("hour"): row for row in history.get("hours", []) if row.get("hour")}
    files = sorted(Path(args.dir).glob("*.json"))
    if not files:
        raise SystemExit("no merge-gate backfill shards found")

    legacy = []
    for path in files:
        payload = load(path, {})
        if int(payload.get("schema_version") or 0) < REQUIRED_SHARD_SCHEMA:
            legacy.append(path.name)
    if legacy:
        raise SystemExit(
            "refusing legacy merge-gate shards; schema 6 root-cause attribution is required: "
            + ", ".join(legacy[:8])
            + (" ..." if len(legacy) > 8 else "")
        )

    windows: list[str] = []
    dates: set[str] = set()
    merged_hours = 0
    analyzed_hours = 0
    errors: list[str] = []

    for path in files:
        payload = load(path, {})
        window_start = payload.get("window_start") or path.stem
        windows.append(str(window_start))
        analysis = payload.get("analysis", {})
        errors.extend(f"{window_start}: {e}" for e in analysis.get("errors", []))

        for overlay in payload.get("hours", []):
            key = overlay.get("hour")
            row = by_hour.get(key)
            if not row:
                continue
            dates.add(str(key)[:10])
            row["merge_gate_analyzed"] = bool(overlay.get("merge_gate_analyzed"))
            if row["merge_gate_analyzed"]:
                analyzed_hours += 1
            for field in FIELDS:
                row[field] = int(overlay.get(field) or 0)
            row["merge_gate_evidence"] = list(overlay.get("merge_gate_evidence") or [])[:20]
            merged_hours += 1

    counts = apply(history)
    history["merge_gate_backfill"] = {
        "schema_version": REQUIRED_SHARD_SCHEMA,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "windows": sorted(set(windows)),
        "window_count": len(set(windows)),
        "dates": sorted(dates),
        "days": len(dates),
        "hours_merged": merged_hours,
        "hours_analyzed": analyzed_hours,
        "errors": errors[-80:],
        "status_counts": counts,
    }
    save(HISTORY, history)
    print(
        f"merge-gate backfill files={len(files)} windows={len(set(windows))} "
        f"hours={merged_hours} analyzed={analyzed_hours} status={counts}"
    )
    for error in errors[-20:]:
        print("warning:", error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

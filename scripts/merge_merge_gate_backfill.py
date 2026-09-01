#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from collect import load, save
from reapply_merge_status import apply

HISTORY = Path("data/history.json")
REQUIRED_SHARD_SCHEMA = 2
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
        # Shard v1 did not interpret historical ci-gate=skipped runs. Refuse to
        # publish those results rather than silently painting old history green.
        raise SystemExit(
            "refusing legacy merge-gate shards without skipped-gate leaf analysis: "
            + ", ".join(legacy[:8])
            + (" ..." if len(legacy) > 8 else "")
        )

    dates: list[str] = []
    merged_hours = 0
    errors: list[str] = []
    for path in files:
        payload = load(path, {})
        date = payload.get("date")
        if not date:
            continue
        dates.append(date)
        errors.extend(f"{date}: {e}" for e in payload.get("analysis", {}).get("errors", []))
        for overlay in payload.get("hours", []):
            key = overlay.get("hour")
            row = by_hour.get(key)
            if not row:
                continue
            for field in FIELDS:
                row[field] = int(overlay.get(field) or 0)
            row["merge_gate_evidence"] = list(overlay.get("merge_gate_evidence") or [])[:20]
            merged_hours += 1

    counts = apply(history)
    history["merge_gate_backfill"] = {
        "schema_version": REQUIRED_SHARD_SCHEMA,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "dates": sorted(set(dates)),
        "days": len(set(dates)),
        "hours_merged": merged_hours,
        "errors": errors[-50:],
        "status_counts": counts,
    }
    save(HISTORY, history)
    print(f"merge-gate backfill files={len(files)} dates={len(set(dates))} hours={merged_hours} status={counts}")
    for error in errors[-20:]:
        print("warning:", error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

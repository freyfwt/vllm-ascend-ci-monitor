#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
HISTORY = DATA / "history.json"
TESTS = DATA / "tests.json"

def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "_backfill")
    history = load(HISTORY, {"schema_version": 9, "hours": []})
    tests = load(TESTS, {"schema_version": 9, "tests": {}})
    hourly = {row.get("hour"): row for row in history.get("hours", []) if row.get("hour")}
    chunks = sorted(root.rglob("*.json"))
    outcomes = defaultdict(set)
    occurrences = defaultdict(set)
    imported = 0
    for path in chunks:
        payload = load(path, {})
        for row in payload.get("hours", []):
            key = row.get("hour")
            if key and key >= "2026-08-01T00:00:00Z":
                hourly[key] = row
                imported += 1
        for item in payload.get("job_outcomes", []):
            key, sha = item.get("key"), item.get("sha")
            if key and sha:
                outcomes[(key, sha)].update(item.get("outcomes", []))
        for item in payload.get("job_occurrences", []):
            key = item.get("key")
            if key:
                occurrences[key].update(item.get("hours", []))
    global_flaky = {key for (key, _sha), values in outcomes.items() if len(values) > 1}
    detected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for key in sorted(global_flaky):
        parts = key.split("::", 2)
        workflow = parts[1] if len(parts) > 1 else "Unknown workflow"
        job = parts[2] if len(parts) > 2 else key
        test = tests.setdefault("tests", {}).setdefault(key, {
            "kind": "job", "workflow": workflow, "name": job,
            "probabilistic": False, "observations": [],
        })
        test["probabilistic"] = True
        test["probability_reason"] = "same_commit_mixed_outcomes"
        test.setdefault("first_detected_at", detected_at)
        for hour_key in sorted(occurrences.get(key, set())):
            row = hourly.get(hour_key)
            if not row:
                continue
            entries = row.setdefault("probabilistic", [])
            if not any(item.get("key") == key for item in entries):
                entries.append({
                    "key": key, "kind": "job", "workflow": workflow, "job": job,
                    "pass_rate_30d": test.get("pass_rate_30d"),
                    "reason": "same_commit_mixed_outcomes",
                })
                row["probabilistic_jobs"] = int(row.get("probabilistic_jobs") or 0) + 1
            row["status"] = "down"
    history["hours"] = [hourly[key] for key in sorted(hourly)]
    history["schema_version"] = max(int(history.get("schema_version") or 0), 9)
    tests["schema_version"] = max(int(tests.get("schema_version") or 0), 9)
    save(HISTORY, history)
    save(TESTS, tests)
    print(f"merged chunks={len(chunks)} hourly_rows={imported} total_hours={len(history['hours'])} cross_chunk_flaky={len(global_flaky)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

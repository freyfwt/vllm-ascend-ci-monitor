#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
from pathlib import Path

DATA = Path("data")
HISTORY = DATA / "history.json"

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
    hourly = {row.get("hour"): row for row in history.get("hours", []) if row.get("hour")}
    chunks = sorted(root.rglob("*.json"))
    imported = 0
    for path in chunks:
        payload = load(path, {})
        for row in payload.get("hours", []):
            key = row.get("hour")
            if key and key >= "2026-08-01T00:00:00Z":
                hourly[key] = row
                imported += 1
    history["hours"] = [hourly[key] for key in sorted(hourly)]
    history["schema_version"] = max(int(history.get("schema_version") or 0), 9)
    save(HISTORY, history)
    print(f"merged chunks={len(chunks)} hourly_rows={imported} total_hours={len(history['hours'])}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

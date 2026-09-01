#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from collect import load, save

HISTORY = Path("data/history.json")


def ensure(row: dict[str, Any]) -> None:
    row.setdefault("merge_gate_runs", 0)
    row.setdefault("merge_gate_code_failures", 0)
    row.setdefault("merge_gate_policy_failures", 0)
    row.setdefault("merge_blocking_ci_failures", 0)
    row.setdefault("merge_gate_unknown_failures", 0)
    row.setdefault("nonblocking_ci_failures", 0)
    row.setdefault("merge_gate_evidence", [])


def decide(row: dict[str, Any]) -> str:
    ensure(row)
    if int(row.get("merge_blocking_ci_failures") or 0) > 0:
        return "down"
    if row.get("coverage") == "partial":
        return "unknown"
    if int(row.get("merge_gate_unknown_failures") or 0) > 0:
        return "unknown"
    if int(row.get("infra_failures") or 0) > 0:
        return "degraded"
    if int(row.get("runs") or 0) > 0:
        return "healthy"
    return "unknown"


def apply(history: dict[str, Any]) -> dict[str, int]:
    counts = {"healthy": 0, "down": 0, "degraded": 0, "unknown": 0}
    for row in history.get("hours", []):
        row["status"] = decide(row)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    history["schema_version"] = max(11, int(history.get("schema_version") or 0))
    return counts


def main() -> int:
    history = load(HISTORY, {"hours": []})
    counts = apply(history)
    save(HISTORY, history)
    print("merge-gate status", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

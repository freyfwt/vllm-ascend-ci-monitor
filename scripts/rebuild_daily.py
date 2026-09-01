#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA = Path("data")
HISTORY = DATA / "history.json"
CALENDAR = DATA / "calendar.json"
SUMMARY = DATA / "summary.json"
DAYS = DATA / "days"
TRACKING_START = "2026-08-01"
SCHEMA = 2


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except (OSError, json.JSONDecodeError):
        return default


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def hour_template(date: str, hour: int) -> dict[str, Any]:
    return {
        "hour": f"{date}T{hour:02d}:00:00Z",
        "status": "unknown",
        "coverage": "complete",
        "runs": 0,
        "jobs": 0,
        "workflow_failures": 0,
        "infra_failures": 0,
        "code_failures": 0,
        "ignored_nonverdicts": 0,
        "unresolved_failures": 0,
        "probabilistic_jobs": 0,
        "active_runs": 0,
        "merge_gate_runs": 0,
        "merge_gate_code_failures": 0,
        "merge_blocking_ci_failures": 0,
        "merge_gate_unknown_failures": 0,
        "reasons": {},
        "failures": [],
        "probabilistic": [],
        "merge_gate_evidence": [],
    }


def expected_hours(date: str, now: datetime) -> int:
    today = now.date().isoformat()
    if date < today:
        return 24
    if date > today:
        return 0
    return now.hour


def day_payload(date: str, source: list[dict[str, Any]], updated_at: str, now: datetime) -> dict[str, Any]:
    by_hour = {row.get("hour"): row for row in source if row.get("hour")}
    hours = [by_hour.get(f"{date}T{h:02d}:00:00Z", hour_template(date, h)) for h in range(24)]
    exp = expected_hours(date, now)
    completed = hours[:exp]

    counts = {"healthy": 0, "down": 0, "unknown": 0, "degraded": 0}
    for row in completed:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1

    healthy = counts["healthy"]
    down = counts["down"]
    degraded = counts["degraded"]
    unknown = counts["unknown"]

    # Degraded means the PR merge gate remained usable, but some non-gating
    # CI signal was unhealthy. It is yellow, not red.
    observed = healthy + down + degraded
    available = healthy + degraded
    availability = round(available / observed, 6) if observed else None
    coverage = round(observed / exp, 6) if exp else None
    availability_floor = round(available / exp, 6) if exp else None

    metrics = {
        "availability": availability,
        "availability_floor": availability_floor,
        "coverage": coverage,
        "expected_hours": exp,
        "observed_hours": observed,
        "healthy_hours": healthy,
        "degraded_hours": degraded,
        "down_hours": down,
        "unknown_hours": unknown,
        "runs": sum(int(row.get("runs") or 0) for row in completed),
        "jobs": sum(int(row.get("jobs") or 0) for row in completed),
        "merge_gate_runs": sum(int(row.get("merge_gate_runs") or 0) for row in completed),
        "merge_gate_code_failures": sum(int(row.get("merge_gate_code_failures") or 0) for row in completed),
        "merge_blocking_ci_failures": sum(int(row.get("merge_blocking_ci_failures") or 0) for row in completed),
        "merge_gate_unknown_failures": sum(int(row.get("merge_gate_unknown_failures") or 0) for row in completed),
        "infra_failures": sum(int(row.get("infra_failures") or 0) for row in completed),
        "code_failures": sum(int(row.get("code_failures") or 0) for row in completed),
        "unresolved_failures": sum(int(row.get("unresolved_failures") or 0) for row in completed),
        "probabilistic_jobs": sum(int(row.get("probabilistic_jobs") or 0) for row in completed),
    }

    if down:
        status = "down"
    elif degraded:
        status = "degraded"
    elif exp and unknown:
        status = "partial"
    elif exp and healthy == exp:
        status = "healthy"
    elif observed:
        status = "partial"
    else:
        status = "unknown"

    return {
        "schema_version": SCHEMA,
        "date": date,
        "timezone": "UTC",
        "updated_at": updated_at,
        "status": status,
        "metrics": metrics,
        "hours": hours,
    }


def main() -> int:
    history = load(HISTORY, {"hours": []})
    rows = history.get("hours", [])
    now = datetime.now(timezone.utc)
    updated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        hour = row.get("hour") or ""
        if len(hour) >= 10:
            date = hour[:10]
            if date >= TRACKING_START:
                grouped.setdefault(date, []).append(row)

    today = now.date().isoformat()
    dates = sorted(set(grouped) | ({today} if today >= TRACKING_START else set()))
    DAYS.mkdir(parents=True, exist_ok=True)
    for date in dates:
        save(DAYS / f"{date}.json", day_payload(date, grouped.get(date, []), updated_at, now))

    calendar_days = []
    for path in sorted(DAYS.glob("????-??-??.json")):
        payload = load(path, {})
        date = payload.get("date")
        if not date or date < TRACKING_START:
            continue
        m = payload.get("metrics", {})
        calendar_days.append({
            "date": date,
            "status": payload.get("status", "unknown"),
            "availability": m.get("availability"),
            "availability_floor": m.get("availability_floor"),
            "coverage": m.get("coverage"),
            "expected_hours": m.get("expected_hours", 0),
            "healthy_hours": m.get("healthy_hours", 0),
            "degraded_hours": m.get("degraded_hours", 0),
            "down_hours": m.get("down_hours", 0),
            "unknown_hours": m.get("unknown_hours", 0),
            "merge_gate_runs": m.get("merge_gate_runs", 0),
            "merge_blocking_ci_failures": m.get("merge_blocking_ci_failures", 0),
            "merge_gate_unknown_failures": m.get("merge_gate_unknown_failures", 0),
            "infra_failures": m.get("infra_failures", 0),
            "code_failures": m.get("code_failures", 0),
            "unresolved_failures": m.get("unresolved_failures", 0),
            "probabilistic_jobs": m.get("probabilistic_jobs", 0),
        })
    calendar_days.sort(key=lambda row: row["date"])
    save(CALENDAR, {
        "schema_version": SCHEMA,
        "tracking_start": TRACKING_START,
        "timezone": "UTC",
        "updated_at": updated_at,
        "days": calendar_days,
    })

    healthy = sum(int(day.get("healthy_hours") or 0) for day in calendar_days)
    degraded = sum(int(day.get("degraded_hours") or 0) for day in calendar_days)
    down = sum(int(day.get("down_hours") or 0) for day in calendar_days)
    unknown = sum(int(day.get("unknown_hours") or 0) for day in calendar_days)
    observed = healthy + degraded + down
    expected = sum(int(day.get("expected_hours") or 0) for day in calendar_days)
    available = healthy + degraded

    save(SUMMARY, {
        "schema_version": SCHEMA,
        "tracking_start": TRACKING_START,
        "timezone": "UTC",
        "updated_at": updated_at,
        "latest_hour": rows[-1] if rows else None,
        "days_tracked": len(calendar_days),
        "overall": {
            "availability": round(available / observed, 6) if observed else None,
            "availability_floor": round(available / expected, 6) if expected else None,
            "coverage": round(observed / expected, 6) if expected else None,
            "observed_hours": observed,
            "healthy_hours": healthy,
            "degraded_hours": degraded,
            "down_hours": down,
            "unknown_hours": unknown,
            "merge_gate_runs": sum(int(day.get("merge_gate_runs") or 0) for day in calendar_days),
            "merge_blocking_ci_failures": sum(int(day.get("merge_blocking_ci_failures") or 0) for day in calendar_days),
            "merge_gate_unknown_failures": sum(int(day.get("merge_gate_unknown_failures") or 0) for day in calendar_days),
            "infra_failures": sum(int(day.get("infra_failures") or 0) for day in calendar_days),
            "code_failures": sum(int(day.get("code_failures") or 0) for day in calendar_days),
            "unresolved_failures": sum(int(day.get("unresolved_failures") or 0) for day in calendar_days),
            "probabilistic_jobs": sum(int(day.get("probabilistic_jobs") or 0) for day in calendar_days),
        },
    })
    print(f"daily files={len(calendar_days)} tracking_start={TRACKING_START}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

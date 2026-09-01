#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collect import (
    GH,
    REPO,
    classify_failure,
    diagnose_log,
    failed_step_names,
    is_ci_run,
    iso_ts,
    parse_dt,
)

DATA = Path("data")
OUT = DATA / "blockers.json"
STATE = DATA / "blocker_state.json"
SCHEMA = 5
EVENTS = ("push", "schedule", "workflow_dispatch", "repository_dispatch", "workflow_run")
BOOT = int(os.getenv("BLOCKER_BOOTSTRAP_HOURS", "168"))
LOOK = int(os.getenv("BLOCKER_LOOKBACK_HOURS", "10"))
KEEP = int(os.getenv("BLOCKER_STATE_DAYS", "60"))
EVIDENCE_CAP = int(os.getenv("BLOCKER_EVIDENCE_CAP", "96"))
FLOOR = os.getenv("BLOCKER_TRACKING_FLOOR", "2026-08-01T00:00:00Z")
PASS = {"success"}
FAIL = {"failure", "timed_out", "startup_failure"}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TS = re.compile(r"^\s*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*")
DYN = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,})\b", re.I)
BIG = re.compile(r"(?<![\w.-])\d{5,}(?![\w.-])")
PATHISH = re.compile(r"(?:/[A-Za-z0-9_.-]+){3,}")
GENERIC = re.compile(
    r"(process completed with exit code|command terminated with (?:non-zero )?exit code|"
    r"executing the custom container implementation failed|please contact your self[- ]hosted runner administrator|"
    r"^error:\s*job failed\.?$|^job failed\.?$|^upload failed(?:\.|$)|"
    r"cleaning up orphan processes|post job cleanup)",
    re.I,
)
DERIVED_JOB = re.compile(
    r"(^|[/ :_-])(ci[-_ ]?gate|merge[-_ ]?(?:result|results)|collect[-_ ]?(?:result|results)|"
    r"summary|report|stream[-_ ]?logs?|upload[-_ ]?(?:artifact|artifacts)|artifact[-_ ]?upload|"
    r"cleanup|finalize)(\b|[/ :_-])",
    re.I,
)
DERIVED_STEP = re.compile(
    r"(^|[/ :_-])(stream[-_ ]?logs?|upload[-_ ]?(?:artifact|artifacts)|artifact[-_ ]?upload|"
    r"collect[-_ ]?logs?|cleanup|post[-_ ]?job|finalize|report|summary)(\b|[/ :_-])",
    re.I,
)
ERROR_LINE = re.compile(
    r"(traceback|assertionerror|runtimeerror|valueerror|typeerror|modulenotfounderror|importerror|"
    r"diststoreerror|connectionerror|readtimeout|timeout(error)?|segmentation|fatal|"
    r"no such file|not found|unavailable|refused|reset by peer|no space left|evicted|unschedulable|"
    r"ruff .*failed|files were modified by this hook|pre-commit hook\(s\) made changes|"
    r"FAILED\s+[^\n]+::|compilation terminated|undefined reference|acl_error)",
    re.I,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def norm(text: str) -> str:
    text = ANSI.sub("", TS.sub("", text or "")).replace("##[error]", "").strip()
    text = DYN.sub("<id>", text)
    text = BIG.sub("<num>", text)
    text = PATHISH.sub("<path>", text)
    return re.sub(r"\s+", " ", text)[:700]


def root_signature(text: str, fallback: str) -> tuple[str, str, bool]:
    candidates: list[str] = []
    for raw in (text or "").splitlines()[-5000:]:
        line = norm(raw)
        if not line or GENERIC.search(line):
            continue
        if ERROR_LINE.search(line) and line not in candidates:
            candidates.append(line)
    if candidates:
        def specificity(line: str) -> int:
            if re.search(
                r"(diststoreerror|assertionerror|modulenotfounderror|importerror|segmentation|"
                r"undefined reference|no such file|connection reset|could not resolve|no space left|"
                r"evicted|unschedulable)",
                line,
                re.I,
            ):
                return 4
            if re.search(
                r"(ruff .*failed|FAILED\s+[^\n]+::|compilation terminated|typeerror|valueerror|timeout(error)?)",
                line,
                re.I,
            ):
                return 3
            if re.search(
                r"(runtimeerror: server .* exited unexpectedly|pre-commit hook\(s\) made changes|"
                r"files were modified by this hook)",
                line,
                re.I,
            ):
                return 1
            return 2

        best_score = max(specificity(line) for line in candidates)
        signature = [line for line in candidates if specificity(line) == best_score][-1][:320]
        excerpt = "\n".join(candidates[-6:])[:1800]
        return signature, excerpt, True
    clean = norm(fallback) or "CI job failure"
    return clean[:320], clean[:1800], False


def job_family(name: str) -> str:
    value = norm(name or "Unnamed job")
    value = re.sub(r"\s*\([^)]{10,}\)(?:\s*/.*)?$", "", value)
    value = re.sub(r"\s*/\s*[^/]{20,}$", "", value)
    return value[:180] or "Unnamed job"


def incident_id(classification: str, signature: str, family: str, source: str) -> str:
    basis = f"{classification}\x1f{signature.lower()}"
    if source == "step_summary":
        basis += f"\x1f{family.lower()}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def category(reason: str, infra: bool) -> str:
    if infra:
        return "infrastructure"
    if reason in {"TEST_FAILURE", "TEST_TIMEOUT", "BUILD_OR_CHECK_FAILURE"}:
        return "code"
    return "unresolved"


def main_span(gh: GH, event: str, start: datetime, end: datetime, depth: int = 0):
    if not gh.ok(8):
        return {}, False, {"expected": None, "fetched": 0, "complete": False, "slices": 0}
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params = {"event": event, "branch": "main", "created": span, "per_page": 100, "page": 1}
    first = gh.get(f"/repos/{REPO}/actions/runs", params)
    expected = int(first.get("total_count") or 0)
    rows = first.get("workflow_runs", [])
    if expected > 950 and end - start > timedelta(minutes=15) and depth < 12:
        mid = start + (end - start) / 2
        left, lok, lm = main_span(gh, event, start, mid, depth + 1)
        right, rok, rm = main_span(gh, event, mid + timedelta(seconds=1), end, depth + 1)
        left.update(right)
        ok = lok and rok
        return left, ok, {
            "expected": (lm.get("expected") or 0) + (rm.get("expected") or 0),
            "fetched": len(left),
            "complete": ok,
            "slices": (lm.get("slices") or 1) + (rm.get("slices") or 1),
        }
    out = {int(r["id"]): r for r in rows if r.get("id")}
    pages = min(max(1, math.ceil(expected / 100)), 10)
    for page in range(2, pages + 1):
        if not gh.ok(8):
            break
        params["page"] = page
        payload = gh.get(f"/repos/{REPO}/actions/runs", params)
        for row in payload.get("workflow_runs", []):
            if row.get("id"):
                out[int(row["id"])] = row
    ok = len(out) >= expected
    return out, ok, {"expected": expected, "fetched": len(out), "complete": ok, "slices": 1}


def main_runs(gh: GH, start: datetime, end: datetime):
    merged: dict[int, dict[str, Any]] = {}
    coverage: dict[str, Any] = {}
    complete = True
    for event in EVENTS:
        if not gh.ok(15):
            complete = False
            break
        rows, ok, meta = main_span(gh, event, start, end)
        coverage[event] = meta
        complete = complete and ok
        merged.update(
            {i: row for i, row in rows.items() if row.get("head_branch") == "main" and is_ci_run(row)}
        )
    rows = [
        r
        for r in merged.values()
        if r.get("status") == "completed" and r.get("conclusion") != "skipped"
    ]
    # Recent failures get the scarce evidence budget first.
    rows.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    return rows, complete, coverage


def list_jobs(gh: GH, run_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 15):
        if not gh.ok(8):
            break
        payload = gh.get(
            f"/repos/{REPO}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": 100, "page": page},
        )
        part = payload.get("jobs", [])
        out += part
        if not part or len(out) >= int(payload.get("total_count") or len(out)):
            break
    return out


def annotations(gh: GH, job_id: int, stats: dict[str, int]) -> str:
    stats["annotations_attempted"] += 1
    try:
        payload = gh.get(f"/repos/{REPO}/check-runs/{job_id}/annotations", {"per_page": 100})
        stats["annotations_succeeded"] += 1
        pieces: list[str] = []
        for item in payload if isinstance(payload, list) else []:
            for key in ("title", "message", "raw_details"):
                value = (item.get(key) or "").strip()
                if value and value not in pieces:
                    pieces.append(value)
        if pieces:
            stats["annotations_with_text"] += 1
            return "\n".join(pieces)[:300000]
    except Exception:
        stats["annotations_failed"] += 1
    return ""


def job_log(gh: GH, job_id: int, stats: dict[str, int]) -> str:
    """Try the upstream read token without mutating GH metadata-auth state on failure."""
    stats["job_logs_attempted"] += 1
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "vllm-ascend-ci-monitor-blockers/5",
    }
    if gh.token:
        headers["Authorization"] = "Bearer " + gh.token
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            text = response.read().decode("utf-8", errors="replace")[-700000:]
        if text:
            stats["job_logs_succeeded"] += 1
            return text
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        pass
    stats["job_logs_failed"] += 1
    return ""


def evidence_for_failure(gh: GH, event: dict[str, Any], stats: dict[str, int]) -> tuple[str, str]:
    if stats["evidence_attempted"] >= EVIDENCE_CAP or not gh.ok(8):
        return "", "step_summary"
    stats["evidence_attempted"] += 1

    # Full logs contain the real causal traceback when the upstream read token permits them.
    text = job_log(gh, int(event.get("job_id") or 0), stats)
    if text:
        return text, "job_log"

    # Check annotations are the reliable public fallback and often preserve compiler/lint errors.
    text = annotations(gh, int(event.get("job_id") or 0), stats)
    if text:
        return text, "annotations"
    return "", "step_summary"


class NoLogGH:
    def ok(self, reserve: int = 0) -> bool:
        return False


def compact_event(run: dict[str, Any], job: dict[str, Any], workflow: str) -> dict[str, Any] | None:
    conclusion = (job.get("conclusion") or "").lower()
    if conclusion not in PASS | FAIL:
        return None
    at = job.get("completed_at") or job.get("started_at") or run.get("updated_at") or run.get("created_at")
    if not at:
        return None
    name = job.get("name") or "Unnamed job"
    family = job_family(name)
    event: dict[str, Any] = {
        "at": at,
        "outcome": "success" if conclusion in PASS else "failure",
        "conclusion": conclusion,
        "sha": run.get("head_sha"),
        "run_id": int(run.get("id") or 0),
        "job_id": int(job.get("id") or 0),
        "workflow": workflow,
        "job": name,
        "job_family": family,
        "run_url": run.get("html_url"),
        "job_url": job.get("html_url") or run.get("html_url"),
    }
    if conclusion in FAIL:
        failed_step = (failed_step_names(job) or ["(job failure)"])[0]
        if DERIVED_STEP.search(failed_step):
            failed_step = "(job failure)"
        event["failed_step"] = failed_step
        reason, infra, _ = classify_failure(NoLogGH(), job, allow_log=False)
        event["classification"] = reason
        event["infra"] = bool(infra)
    return event


def enrich_failure(gh: GH, event: dict[str, Any], stats: dict[str, int]) -> None:
    text, source = evidence_for_failure(gh, event, stats)
    reason = event.get("classification") or "CODE_OR_UNKNOWN_FAILURE"
    infra = bool(event.get("infra"))
    if text:
        diagnosed, diag_infra, _ = diagnose_log(text)
        if diagnosed:
            reason, infra = diagnosed, diag_infra

    fallback = " | ".join(
        (
            event.get("job_family") or "Unnamed job",
            event.get("failed_step") or "(job failure)",
            event.get("conclusion") or "failure",
        )
    )
    signature, excerpt, concrete = root_signature(text, fallback)
    if not concrete:
        source = "step_summary"
        # A derived/generic failed step is not evidence that the test code itself failed.
        if event.get("failed_step") == "(job failure)" and reason in {
            "TEST_FAILURE",
            "TEST_TIMEOUT",
            "BUILD_OR_CHECK_FAILURE",
        }:
            reason, infra = "CODE_OR_UNKNOWN_FAILURE", False
            signature = f"{event.get('job_family') or 'CI job'} | root cause unavailable"
            excerpt = signature

    event["classification"] = reason
    event["infra"] = bool(infra)
    event["category"] = category(reason, infra)
    event["signature"] = signature
    event["log_excerpt"] = excerpt
    event["evidence_source"] = source
    event["incident_id"] = incident_id(reason, signature, event["job_family"], source)


def prior_three_pass(history: list[dict[str, Any]], first_seen: str) -> bool:
    earlier = [
        e
        for e in sorted(history, key=lambda x: x.get("at") or "")
        if (e.get("at") or "") < first_seen and e.get("outcome") in {"success", "failure"}
    ]
    return len(earlier) >= 3 and all(e.get("outcome") == "success" for e in earlier[-3:])


def pr_for_sha(gh: GH, sha: str | None, first_seen: str | None) -> dict[str, Any] | None:
    if not sha or not first_seen or not gh.ok(8):
        return None
    try:
        pulls = gh.get(f"/repos/{REPO}/commits/{sha}/pulls", {"per_page": 100})
    except Exception:
        return None
    pulls = [p for p in pulls if p.get("merged_at") and p.get("merge_commit_sha") == sha]
    if len(pulls) != 1:
        return None
    try:
        pr = gh.get(f"/repos/{REPO}/pulls/{pulls[0]['number']}")
    except Exception:
        return None
    merged = parse_dt(pr.get("merged_at"))
    first = parse_dt(first_seen)
    if not merged or not first or first < merged or first - merged > timedelta(hours=6):
        return None
    files: list[str] = []
    try:
        files = [
            x.get("filename")
            for x in gh.get(f"/repos/{REPO}/pulls/{pr['number']}/files", {"per_page": 100})
            if x.get("filename")
        ][:20]
    except Exception:
        pass
    return {
        "number": pr["number"],
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "author": (pr.get("user") or {}).get("login"),
        "merged_by": (pr.get("merged_by") or {}).get("login"),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "changed_files": files,
        "confidence": "high",
        "rationale": "previous_3_main_passes_then_first_failure_on_merge_commit_then_reproduced",
    }


def cadence_ttl_hours(issue: dict[str, Any]) -> int:
    text = " ".join(issue.get("affected_workflows", []) + issue.get("affected_jobs", [])).lower()
    if "weekly" in text:
        return 8 * 24
    if "nightly" in text or "daily" in text:
        return 36
    return 10


def public_issue(issue: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "category",
        "classification",
        "signature",
        "log_excerpt",
        "evidence_source",
        "first_seen",
        "last_seen",
        "occurrences",
        "pass_streak",
        "latest_url",
        "latest_run_url",
        "affected_commits",
        "affected_runs",
        "affected_workflows",
        "affected_jobs",
        "affected_job_families",
        "signals",
        "introduced_by",
        "priority_score",
        "blast_radius",
        "age_hours",
    )
    return {k: issue.get(k) for k in keys if issue.get(k) is not None}


def build_incidents(
    gh: GH,
    histories: dict[str, list[dict[str, Any]]],
    current: datetime,
) -> dict[str, list[dict[str, Any]]]:
    failures: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_events: list[dict[str, Any]] = []
    mixed: set[tuple[str, str]] = set()
    for family, history in histories.items():
        sha_outcomes: dict[str, set[str]] = defaultdict(set)
        for event in history:
            all_events.append(event)
            sha = event.get("sha")
            if sha:
                sha_outcomes[sha].add(event.get("outcome") or "")
            if event.get("outcome") == "failure" and event.get("incident_id"):
                failures[event["incident_id"]].append(event)
        for sha, outcomes in sha_outcomes.items():
            if {"success", "failure"}.issubset(outcomes):
                mixed.add((family, sha))

    output: dict[str, list[dict[str, Any]]] = {
        "active": [],
        "watch": [],
        "stale": [],
        "resolved": [],
    }
    for ident, events in failures.items():
        events.sort(key=lambda x: x.get("at") or "")
        first, last = events[0], events[-1]
        workflows = sorted({e.get("workflow") or "Unnamed workflow" for e in events})
        jobs = sorted({e.get("job") or "Unnamed job" for e in events})
        families = sorted({e.get("job_family") or "Unnamed job" for e in events})
        commits = list(dict.fromkeys(e.get("sha") for e in events if e.get("sha")))[-30:]
        runs = list(dict.fromkeys(e.get("run_id") for e in events if e.get("run_id")))[-50:]
        last_at = last.get("at") or ""
        recovery_runs: set[int] = set()
        for event in all_events:
            if event.get("job_family") not in families or (event.get("at") or "") <= last_at:
                continue
            if event.get("outcome") == "success" and event.get("run_id"):
                recovery_runs.add(int(event["run_id"]))
        pass_streak = min(3, len(recovery_runs))
        source_rank = {"job_log": 2, "annotations": 1, "step_summary": 0}
        best = max(
            events,
            key=lambda e: (source_rank.get(e.get("evidence_source") or "", 0), e.get("at") or ""),
        )
        infra = any(bool(e.get("infra")) for e in events)
        issue_category = "infrastructure" if infra else (best.get("category") or "unresolved")
        classification = best.get("classification") or "CODE_OR_UNKNOWN_FAILURE"
        signals: list[str] = []
        if (
            any((e.get("job_family"), e.get("sha")) in mixed for e in events if e.get("sha"))
            and not infra
        ):
            issue_category = "flaky"
            classification = "FLAKY"
            signals.append("same_commit_mixed_outcomes")
        last_dt = parse_dt(last.get("at")) or current
        age_hours = max(0.0, (current - last_dt).total_seconds() / 3600)
        blast = len(jobs) + 2 * max(0, len(workflows) - 1)
        persistent = len(events) >= 2 or len(jobs) >= 2 or len(workflows) >= 2
        ttl = cadence_ttl_hours({"affected_workflows": workflows, "affected_jobs": jobs})
        if pass_streak >= 3:
            status = "resolved"
        elif age_hours <= ttl and persistent:
            status = "active"
        elif age_hours <= ttl * 2 or pass_streak > 0:
            status = "watch"
        else:
            status = "stale"
        priority = (
            (120 if status == "active" else 70 if status == "watch" else 20)
            + min(len(events), 20) * 2
            + blast * 5
            + source_rank.get(best.get("evidence_source") or "", 0) * 6
            - min(age_hours, 100)
        )
        issue: dict[str, Any] = {
            "id": ident,
            "status": status,
            "category": issue_category,
            "classification": classification,
            "signature": best.get("signature"),
            "log_excerpt": best.get("log_excerpt"),
            "evidence_source": best.get("evidence_source"),
            "first_seen": first.get("at"),
            "last_seen": last.get("at"),
            "occurrences": len(events),
            "pass_streak": pass_streak,
            "latest_url": last.get("job_url"),
            "latest_run_url": last.get("run_url"),
            "affected_commits": commits,
            "affected_runs": runs,
            "affected_workflows": workflows,
            "affected_jobs": jobs,
            "affected_job_families": families,
            "signals": signals,
            "priority_score": round(priority, 1),
            "blast_radius": blast,
            "age_hours": round(age_hours, 1),
        }
        if (
            status != "resolved"
            and issue_category == "code"
            and len(events) >= 2
            and best.get("evidence_source") != "step_summary"
            and prior_three_pass(histories.get(first.get("job_family") or "", []), first.get("at") or "")
        ):
            pr = pr_for_sha(gh, first.get("sha"), first.get("at"))
            if pr:
                issue["introduced_by"] = pr
        output[status].append(public_issue(issue))

    for status, rows in output.items():
        if status == "resolved":
            rows.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
            output[status] = rows[:120]
        else:
            rows.sort(
                key=lambda x: (-(float(x.get("priority_score") or 0)), x.get("last_seen") or "")
            )
    return output


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    current = utcnow()
    state = load(STATE, {})
    fresh = args.rebuild or int(state.get("schema_version") or 0) < SCHEMA
    if fresh:
        state = {"schema_version": SCHEMA, "seen": {}, "job_history": {}}
    state.setdefault("seen", {})
    state.setdefault("job_history", {})
    if args.since:
        raw = args.since if "T" in args.since else args.since + "T00:00:00Z"
        start = parse_dt(raw) or current - timedelta(hours=BOOT)
    elif fresh or not state.get("last_scan_at"):
        start = max(parse_dt(FLOOR) or current - timedelta(hours=BOOT), current - timedelta(hours=BOOT))
    else:
        start = current - timedelta(hours=LOOK)

    gh = GH()
    errors: list[str] = []
    stats = {
        "evidence_attempted": 0,
        "annotations_attempted": 0,
        "annotations_succeeded": 0,
        "annotations_with_text": 0,
        "annotations_failed": 0,
        "job_logs_attempted": 0,
        "job_logs_succeeded": 0,
        "job_logs_failed": 0,
    }
    try:
        runs, complete, coverage = main_runs(gh, start, current)
    except Exception as exc:
        runs, complete, coverage = [], False, {}
        errors.append(f"runs: {type(exc).__name__}: {exc}")

    run_count = job_count = failure_count = 0
    for run_obj in runs:
        if not gh.ok(15):
            complete = False
            errors.append("request budget reached")
            break
        rid = int(run_obj.get("id") or 0)
        try:
            jobs = list_jobs(gh, rid)
        except Exception as exc:
            complete = False
            errors.append(f"jobs:{rid}: {type(exc).__name__}: {exc}")
            continue
        run_count += 1
        workflow = run_obj.get("name") or "Unnamed workflow"
        non_derived_failures = [
            j
            for j in jobs
            if (j.get("conclusion") or "").lower() in FAIL
            and not DERIVED_JOB.search(j.get("name") or "")
        ]
        for job in jobs:
            jid = str(job.get("id") or "")
            if not jid or jid in state["seen"]:
                continue
            event = compact_event(run_obj, job, workflow)
            seen_at = (
                job.get("completed_at")
                or job.get("started_at")
                or run_obj.get("updated_at")
                or iso_ts(current)
            )
            state["seen"][jid] = seen_at
            if event is None:
                continue
            job_count += 1
            if (
                event["outcome"] == "failure"
                and DERIVED_JOB.search(event.get("job") or "")
                and non_derived_failures
            ):
                continue
            if event["outcome"] == "failure":
                failure_count += 1
                enrich_failure(gh, event, stats)
            state["job_history"].setdefault(event["job_family"], []).append(event)

    cutoff = current - timedelta(days=KEEP)
    state["seen"] = {
        k: v for k, v in state["seen"].items() if (parse_dt(v) or current) >= cutoff
    }
    for family, history in list(state["job_history"].items()):
        state["job_history"][family] = [
            e for e in history if (parse_dt(e.get("at")) or current) >= cutoff
        ][-800:]
        if not state["job_history"][family]:
            state["job_history"].pop(family, None)

    groups = build_incidents(gh, state["job_history"], current)
    open_rows = groups["active"] + groups["watch"] + groups["stale"]
    output = {
        "schema_version": SCHEMA,
        "updated_at": iso_ts(current),
        "upstream_repo": REPO,
        "scope": {
            "branch": "main",
            "grouping": "normalized causal signature across matrix jobs; derived gate/report/stream-log failures suppressed",
            "close_after_consecutive_passes": 3,
            "pass_definition": "three distinct later main CI runs with success in an affected job family",
            "active_definition": "recent + reproduced or multi-job/workflow blast radius; cadence-aware for nightly/weekly jobs",
            "pr_attribution": "high-confidence code regressions with concrete evidence only",
            "evidence_priority": "authenticated upstream job log, then check annotation, then failed-step summary",
        },
        "analysis": {
            "complete": complete,
            "window_start": iso_ts(start),
            "window_end": iso_ts(current),
            "runs": run_count,
            "jobs": job_count,
            "new_failures": failure_count,
            "api_requests": gh.requests,
            "request_budget": gh.budget,
            "evidence_requests": stats,
            "coverage": coverage,
            "errors": errors[-10:],
        },
        "stats": {
            "active": len(groups["active"]),
            "watch": len(groups["watch"]),
            "stale": len(groups["stale"]),
            "open": len(open_rows),
            "resolved": len(groups["resolved"]),
            "attributed": sum(bool(i.get("introduced_by")) for i in open_rows),
            "flaky": sum(i.get("category") == "flaky" for i in open_rows),
            "infrastructure": sum(i.get("category") == "infrastructure" for i in open_rows),
        },
        "active": groups["active"],
        "watch": groups["watch"],
        "stale": groups["stale"],
        "open": open_rows,
        "resolved": groups["resolved"],
    }
    state.update({"schema_version": SCHEMA, "updated_at": iso_ts(current), "last_scan_at": iso_ts(current)})
    save(OUT, output)
    save(STATE, state)
    print(
        f"main_runs={run_count} jobs={job_count} failures={failure_count} "
        f"active={len(groups['active'])} watch={len(groups['watch'])} "
        f"stale={len(groups['stale'])} resolved={len(groups['resolved'])} "
        f"logs={stats['job_logs_succeeded']}/{stats['job_logs_attempted']} "
        f"annotations={stats['annotations_with_text']}/{stats['annotations_attempted']} "
        f"requests={gh.requests}/{gh.budget} complete={complete}"
    )
    for error in errors:
        print("warning:", error, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

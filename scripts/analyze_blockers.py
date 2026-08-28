#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collect import (
    GH, REPO, DOWNLOAD_LOG, NETWORK_LOG, RUNNER_LOG,
    classify_failure, failed_step_names, is_ci_run, iso_ts, parse_dt, _run_span,
)

DATA = Path("data")
OUT = DATA / "blockers.json"
STATE = DATA / "blocker_state.json"
SCHEMA = 2
EVENTS = ("push", "schedule", "workflow_dispatch", "repository_dispatch", "workflow_run")
BOOT = int(os.getenv("BLOCKER_BOOTSTRAP_HOURS", "168"))
LOOK = int(os.getenv("BLOCKER_LOOKBACK_HOURS", "8"))
KEEP = int(os.getenv("BLOCKER_STATE_DAYS", "60"))
LOG_CAP = int(os.getenv("BLOCKER_LOG_CAP", "48"))
FLOOR = os.getenv("BLOCKER_TRACKING_FLOOR", "2026-08-01T00:00:00Z")

# A strict blocker recovery requires an actual PASS. Neutral/skipped do not count.
PASS = {"success"}
FAIL = {"failure", "timed_out", "startup_failure"}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TS = re.compile(r"^\s*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*")
ERR = re.compile(
    r"(traceback|exception|error|failed|fatal|assert|segmentation|timeout|"
    r"no such file|not found|unavailable|refused|reset by peer|cannot |could not |"
    r"out of memory|crashloop|evicted|acl_error|runtimeerror|valueerror|typeerror|"
    r"modulenotfounderror|connectionerror|readtimeout)",
    re.I,
)
GEN = re.compile(r"^(?:Error:\s*)?(?:Process completed with exit code \d+\.?|Job failed\.?)$", re.I)
DYN = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,})\b", re.I)
BIG = re.compile(r"(?<![\w.-])\d{5,}(?![\w.-])")


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
    text = ANSI.sub("", TS.sub("", text)).replace("##[error]", "").strip()
    text = DYN.sub("<id>", text)
    text = BIG.sub("<num>", text)
    return re.sub(r"\s+", " ", text)[:500]


def evidence(text: str, fallback: str) -> tuple[str, str]:
    lines: list[str] = []
    for raw in (text or "").splitlines()[-4000:]:
        line = norm(raw)
        if line and ERR.search(line) and not GEN.match(line) and line not in lines:
            lines.append(line)
    lines = lines[-5:]
    signature = (lines[-1] if lines else norm(fallback))[:260] or "CI job failure"
    excerpt = "\n".join(lines)[:1500] if lines else signature
    return signature, excerpt


def issue_id(workflow: str, job: str, step: str, signature: str) -> str:
    raw = "\x1f".join((workflow.lower(), job.lower(), step.lower(), signature.lower()))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def job_key(workflow: str, job: str) -> str:
    return f"{workflow}::{job}"


def category(reason: str, infra: bool) -> str:
    if infra:
        return "infrastructure"
    if reason in {"TEST_FAILURE", "TEST_TIMEOUT", "BUILD_OR_CHECK_FAILURE"}:
        return "code"
    return "unresolved"


def main_runs(gh: GH, start: datetime, end: datetime):
    merged: dict[int, dict[str, Any]] = {}
    coverage: dict[str, Any] = {}
    complete = True
    for event in EVENTS:
        if not gh.ok(15):
            complete = False
            break
        rows, ok, meta = _run_span(gh, event, start, end)
        coverage[event] = meta
        complete = complete and ok
        merged.update({
            run_id: run
            for run_id, run in rows.items()
            if run.get("head_branch") == "main" and is_ci_run(run)
        })
    rows = [
        run for run in merged.values()
        if run.get("status") == "completed" and run.get("conclusion") != "skipped"
    ]
    rows.sort(key=lambda run: run.get("updated_at") or run.get("created_at") or "")
    return rows, complete, coverage


def list_jobs(gh: GH, run_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 15):
        payload = gh.get(
            f"/repos/{REPO}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": 100, "page": page},
        )
        part = payload.get("jobs", [])
        out.extend(part)
        if not part or len(out) >= int(payload.get("total_count") or len(out)):
            break
    return out


def job_log(gh: GH, event: dict[str, Any], log_stats: dict[str, int]) -> str:
    if log_stats["attempted"] >= LOG_CAP:
        return ""
    job_id = int(event.get("job_id") or 0)
    if not job_id:
        return ""
    log_stats["attempted"] += 1
    try:
        text = gh.text(f"/repos/{REPO}/actions/jobs/{job_id}/logs")[-300000:]
        log_stats["succeeded"] += 1
        return text
    except Exception:
        log_stats["failed"] += 1
        return ""


def pr_for_sha(gh: GH, sha: str | None, first_seen: str | None):
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


class NoLogGH:
    """classify_failure(..., allow_log=False) never performs a request."""
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
    event = {
        "at": at,
        "outcome": "success" if conclusion in PASS else "failure",
        "conclusion": conclusion,
        "sha": run.get("head_sha"),
        "run_id": int(run.get("id") or 0),
        "job_id": int(job.get("id") or 0),
        "workflow": workflow,
        "job": name,
        "job_key": job_key(workflow, name),
        "run_url": run.get("html_url"),
        "job_url": job.get("html_url") or run.get("html_url"),
    }
    if conclusion in FAIL:
        event["failed_step"] = (failed_step_names(job) or ["(job failure)"])[0]
        reason, infra, _ = classify_failure(NoLogGH(), job, allow_log=False)
        event["reason"] = reason
        event["infra"] = bool(infra)
    return event


def unresolved_segment(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(history, key=lambda x: x.get("at") or "")
    recovery_index = 0
    pass_streak = 0
    for index, event in enumerate(ordered):
        if event.get("outcome") == "success":
            pass_streak += 1
            if pass_streak >= 3:
                recovery_index = index + 1
        elif event.get("outcome") == "failure":
            pass_streak = 0
    segment = ordered[recovery_index:]
    if not any(x.get("outcome") == "failure" for x in segment):
        return []
    return segment


def prior_three_pass(history: list[dict[str, Any]], first_seen: str) -> bool:
    earlier = [
        x for x in sorted(history, key=lambda y: y.get("at") or "")
        if (x.get("at") or "") < first_seen and x.get("outcome") in {"success", "failure"}
    ]
    return len(earlier) >= 3 and all(x.get("outcome") == "success" for x in earlier[-3:])


def issue_from_event(gh: GH, event: dict[str, Any], log_stats: dict[str, int], sampled: bool = True):
    text = job_log(gh, event, log_stats) if sampled else ""
    reason = event.get("reason") or "CODE_OR_UNKNOWN_FAILURE"
    infra = bool(event.get("infra"))
    if text:
        if DOWNLOAD_LOG.search(text):
            reason, infra = "DOWNLOAD", True
        elif NETWORK_LOG.search(text):
            reason, infra = "NETWORK", True
        elif RUNNER_LOG.search(text):
            reason, infra = "RUNNER", True
    fallback = " | ".join((
        event.get("job") or "Unnamed job",
        event.get("failed_step") or "(job failure)",
        event.get("conclusion") or "failure",
    ))
    signature, excerpt = evidence(text, fallback)
    ident = issue_id(
        event.get("workflow") or "Unnamed workflow",
        event.get("job") or "Unnamed job",
        event.get("failed_step") or "(job failure)",
        signature,
    )
    issue = {
        "id": ident,
        "status": "open",
        "category": category(reason, infra),
        "classification": reason,
        "workflow": event.get("workflow"),
        "job": event.get("job"),
        "job_key": event.get("job_key"),
        "failed_step": event.get("failed_step"),
        "signature": signature,
        "log_excerpt": excerpt,
        "log_sampled": bool(text),
        "first_seen": event.get("at"),
        "first_sha": event.get("sha"),
        "last_seen": event.get("at"),
        "occurrences": 1,
        "pass_streak": 0,
        "latest_url": event.get("job_url"),
        "latest_run_url": event.get("run_url"),
        "affected_commits": [event["sha"]] if event.get("sha") else [],
        "affected_runs": [event["run_id"]] if event.get("run_id") else [],
        "signals": [],
    }
    return issue, ident


def merge_failure(issue: dict[str, Any], event: dict[str, Any], excerpt: str | None = None) -> None:
    issue["status"] = "open"
    issue.pop("resolved_at", None)
    if (event.get("at") or "") < (issue.get("first_seen") or event.get("at") or ""):
        issue["first_seen"] = event.get("at")
        issue["first_sha"] = event.get("sha")
    if (event.get("at") or "") >= (issue.get("last_seen") or ""):
        issue["last_seen"] = event.get("at")
        issue["latest_url"] = event.get("job_url")
        issue["latest_run_url"] = event.get("run_url")
        if excerpt:
            issue["log_excerpt"] = excerpt
    issue["occurrences"] = int(issue.get("occurrences") or 0) + 1
    issue["pass_streak"] = 0
    sha = event.get("sha")
    if sha and sha not in issue.setdefault("affected_commits", []):
        issue["affected_commits"] = (issue["affected_commits"] + [sha])[-20:]
    run_id = event.get("run_id")
    if run_id and run_id not in issue.setdefault("affected_runs", []):
        issue["affected_runs"] = (issue["affected_runs"] + [run_id])[-30:]


def close_or_increment(issues: dict[str, dict[str, Any]], event: dict[str, Any]) -> None:
    for issue in issues.values():
        if issue.get("status") != "open" or issue.get("job_key") != event.get("job_key"):
            continue
        if (event.get("at") or "") <= (issue.get("last_seen") or ""):
            continue
        issue["pass_streak"] = int(issue.get("pass_streak") or 0) + 1
        if issue["pass_streak"] >= 3:
            issue["status"] = "resolved"
            issue["resolved_at"] = event.get("at")


def reset_job_issues(issues: dict[str, dict[str, Any]], job_key_value: str) -> None:
    for issue in issues.values():
        if issue.get("status") == "open" and issue.get("job_key") == job_key_value:
            issue["pass_streak"] = 0


def bootstrap_open_issues(gh: GH, state: dict[str, Any], log_stats: dict[str, int]) -> None:
    issues = state["issues"]
    histories = state["job_history"]
    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for key, history in histories.items():
        segment = unresolved_segment(history)
        if any(x.get("outcome") == "failure" for x in segment):
            candidates.append((key, segment))
    candidates.sort(
        key=lambda item: max((x.get("at") or "" for x in item[1]), default=""),
        reverse=True,
    )
    for key, segment in candidates:
        failures = [x for x in segment if x.get("outcome") == "failure"]
        passes_after_last = 0
        for event in reversed(segment):
            if event.get("outcome") == "success":
                passes_after_last += 1
            elif event.get("outcome") == "failure":
                break
        samples = [failures[0]]
        if failures[-1].get("job_id") != failures[0].get("job_id"):
            samples.append(failures[-1])
        built = []
        for event in samples:
            sampled = log_stats["attempted"] < LOG_CAP
            issue, _ = issue_from_event(gh, event, log_stats, sampled=sampled)
            built.append((issue, event))
        if len(built) == 2 and built[0][0]["id"] == built[1][0]["id"]:
            issue = built[0][0]
            issue["occurrences"] = len(failures)
            issue["last_seen"] = failures[-1].get("at")
            issue["latest_url"] = failures[-1].get("job_url")
            issue["latest_run_url"] = failures[-1].get("run_url")
            issue["pass_streak"] = passes_after_last
            issue["affected_commits"] = list(dict.fromkeys(
                x.get("sha") for x in failures if x.get("sha")
            ))[-20:]
            issue["affected_runs"] = list(dict.fromkeys(
                x.get("run_id") for x in failures if x.get("run_id")
            ))[-30:]
            if built[1][0].get("log_sampled"):
                issue["log_excerpt"] = built[1][0].get("log_excerpt")
                issue["log_sampled"] = True
            issues[issue["id"]] = issue
            if (
                issue.get("classification") in {"TEST_FAILURE", "TEST_TIMEOUT"}
                and len(failures) >= 2
                and prior_three_pass(histories[key], issue["first_seen"])
            ):
                pr = pr_for_sha(gh, issue.get("first_sha"), issue.get("first_seen"))
                if pr:
                    issue["introduced_by"] = pr
        else:
            for issue, _ in built:
                issue["pass_streak"] = passes_after_last
                issues[issue["id"]] = issue
        if len(failures) > len(samples):
            for issue, _ in built:
                issue["bootstrap_failure_count"] = len(failures)
                issue["log_sampling"] = "first_and_latest_failure"


def public_issue(issue: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "status", "category", "classification", "workflow", "job", "failed_step",
        "signature", "log_excerpt", "log_sampled", "first_seen", "last_seen", "occurrences",
        "bootstrap_failure_count", "log_sampling", "pass_streak", "resolved_at", "latest_url",
        "latest_run_url", "affected_commits", "affected_runs", "signals", "introduced_by",
    )
    return {key: issue.get(key) for key in keys if issue.get(key) is not None}


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    current = utcnow()

    state = load(STATE, {})
    fresh = args.rebuild or int(state.get("schema_version") or 0) < SCHEMA
    if fresh:
        state = {"schema_version": SCHEMA, "seen": {}, "issues": {}, "job_history": {}}
    for key in ("seen", "issues", "job_history"):
        state.setdefault(key, {})

    if args.since:
        raw = args.since if "T" in args.since else args.since + "T00:00:00Z"
        start = parse_dt(raw) or current - timedelta(hours=BOOT)
    elif not state.get("last_scan_at"):
        start = max(parse_dt(FLOOR) or current - timedelta(hours=BOOT), current - timedelta(hours=BOOT))
    else:
        start = current - timedelta(hours=LOOK)

    gh = GH()
    errors: list[str] = []
    log_stats = {"attempted": 0, "succeeded": 0, "failed": 0}
    try:
        runs, complete, coverage = main_runs(gh, start, current)
    except Exception as exc:
        runs, complete, coverage = [], False, {}
        errors.append(f"runs: {type(exc).__name__}: {exc}")

    issues = state["issues"]
    batch: dict[tuple[str, str], set[str]] = defaultdict(set)
    run_count = 0
    job_count = 0

    for run_obj in runs:
        if not gh.ok(15):
            complete = False
            errors.append("request budget reached")
            break
        run_id = int(run_obj.get("id") or 0)
        try:
            jobs = list_jobs(gh, run_id)
        except Exception as exc:
            complete = False
            errors.append(f"jobs:{run_id}: {type(exc).__name__}: {exc}")
            continue
        run_count += 1
        workflow = run_obj.get("name") or "Unnamed workflow"
        for job in jobs:
            job_id = str(job.get("id") or "")
            if not job_id or job_id in state["seen"]:
                continue
            event = compact_event(run_obj, job, workflow)
            seen_at = job.get("completed_at") or job.get("started_at") or run_obj.get("updated_at") or iso_ts(current)
            state["seen"][job_id] = seen_at
            if event is None:
                continue
            job_count += 1
            key = event["job_key"]
            history = state["job_history"].setdefault(key, [])
            history.append(event)
            batch[(key, event.get("sha") or "")].add(event["outcome"])

            if fresh:
                continue
            if event["outcome"] == "success":
                close_or_increment(issues, event)
                continue

            reset_job_issues(issues, key)
            sampled = log_stats["attempted"] < LOG_CAP
            candidate, ident = issue_from_event(gh, event, log_stats, sampled=sampled)
            existing = issues.get(ident)
            if existing:
                merge_failure(existing, event, candidate.get("log_excerpt"))
                existing["log_sampled"] = bool(existing.get("log_sampled") or candidate.get("log_sampled"))
                issue = existing
            else:
                issues[ident] = candidate
                issue = candidate

            if (
                issue.get("classification") in {"TEST_FAILURE", "TEST_TIMEOUT"}
                and issue.get("occurrences", 0) >= 2
                and prior_three_pass(history, issue["first_seen"])
                and not issue.get("introduced_by")
            ):
                pr = pr_for_sha(gh, issue.get("first_sha"), issue.get("first_seen"))
                if pr:
                    issue["introduced_by"] = pr

    if fresh:
        bootstrap_open_issues(gh, state, log_stats)

    for (key, sha), outcomes in batch.items():
        if sha and outcomes == {"success", "failure"}:
            for issue in issues.values():
                if issue.get("status") == "open" and issue.get("job_key") == key and sha in issue.get("affected_commits", []):
                    signals = issue.setdefault("signals", [])
                    if "same_commit_mixed_outcomes" not in signals:
                        signals.append("same_commit_mixed_outcomes")
                    if issue.get("category") != "infrastructure":
                        issue["category"] = "flaky"
                        issue["classification"] = "FLAKY"
                        issue.pop("introduced_by", None)

    cutoff = current - timedelta(days=KEEP)
    state["seen"] = {
        key: value for key, value in state["seen"].items()
        if (parse_dt(value) or current) >= cutoff
    }
    for key, history in list(state["job_history"].items()):
        state["job_history"][key] = [
            item for item in history if (parse_dt(item.get("at")) or current) >= cutoff
        ][-500:]

    open_issues = [public_issue(item) for item in issues.values() if item.get("status") == "open"]
    resolved = [public_issue(item) for item in issues.values() if item.get("status") == "resolved"]
    open_issues.sort(key=lambda item: (-int(item.get("occurrences") or 0), item.get("first_seen") or ""))
    resolved.sort(key=lambda item: item.get("resolved_at") or "", reverse=True)
    resolved = resolved[:120]

    output = {
        "schema_version": SCHEMA,
        "updated_at": iso_ts(current),
        "upstream_repo": REPO,
        "scope": {
            "branch": "main",
            "close_after_consecutive_passes": 3,
            "pass_definition": "job conclusion == success",
            "pr_attribution": "high-confidence test regressions only",
        },
        "analysis": {
            "complete": complete,
            "window_start": iso_ts(start),
            "window_end": iso_ts(current),
            "runs": run_count,
            "jobs": job_count,
            "api_requests": gh.requests,
            "request_budget": gh.budget,
            "log_requests": log_stats,
            "coverage": coverage,
            "errors": errors[-10:],
        },
        "stats": {
            "open": len(open_issues),
            "attributed": sum(bool(item.get("introduced_by")) for item in open_issues),
            "flaky": sum(item.get("category") == "flaky" for item in open_issues),
            "infrastructure": sum(item.get("category") == "infrastructure" for item in open_issues),
            "resolved": len(resolved),
        },
        "open": open_issues,
        "resolved": resolved,
    }

    state.update(schema_version=SCHEMA, updated_at=iso_ts(current), last_scan_at=iso_ts(current))
    save(OUT, output)
    save(STATE, state)
    print(
        f"main_runs={run_count} jobs={job_count} open={len(open_issues)} "
        f"resolved={len(resolved)} attributed={output['stats']['attributed']} "
        f"logs={log_stats['succeeded']}/{log_stats['attempted']} "
        f"requests={gh.requests}/{gh.budget} complete={complete}"
    )
    for error in errors:
        print("warning:", error, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

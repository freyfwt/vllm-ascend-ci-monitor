#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collect import GH, REPO, diagnose_log, failed_step_names, iso_hour, iso_ts, load, parse_dt, save

DATA = Path("data")
HISTORY = DATA / "history.json"
TESTS = DATA / "tests.json"
LOOKBACK = int(os.getenv("MERGE_AVAILABILITY_LOOKBACK_HOURS", "12"))
LOG_CAP = int(os.getenv("MERGE_GATE_LOG_CAP", "12"))
LOG_TIMEOUT = int(os.getenv("MERGE_GATE_LOG_TIMEOUT", "8"))
E2E_PATH = ".github/workflows/pr_test.yaml"
GATE = "ci-gate"
DIRECT_GATE_PREFIXES = (
    "pre-commit",
    "select-tests",
    "run-selected-tests",
    "run-selected-tests-a5",
)

# Strong deterministic PR verdict steps. Do not match generic words such as
# "test" here: select-tests is control-plane logic and can fail for CI reasons.
CODE_STEP = re.compile(
    r"(run pre-commit|run mypy|ruff|format|lint|pytest|unit.?test|"
    r"validate pr title|gitleaks|secret scan|compile|build wheel)",
    re.I,
)
TITLE_STEP = re.compile(r"validate pr title", re.I)
DETERMINISTIC_META_STEP = re.compile(
    r"(run pre-commit|run mypy|ruff|format|lint|gitleaks|secret scan|codespell|typos)",
    re.I,
)
TITLE_POLICY_LOG = re.compile(
    r"(pr title must contain|pr title cannot be empty|invalid pr title|unsupported pr title)",
    re.I,
)
DETERMINISTIC_CODE_LOG = re.compile(
    r"(pre-commit hook\(s\) made changes|files were modified by this hook|"
    r"ruff (?:check|format).*failed|hook id:\s*(?:ruff|ruff-check|ruff-format|codespell|typos|mypy|markdownlint)|"
    r"(?:codespell|typos|mypy|markdownlint|gitleaks|secret scan).*?(?:failed|error|finding|leak))",
    re.I,
)
RECOVERED_ACTION_DOWNLOAD = re.compile(r"failed to download action", re.I)
SETUP_OR_INFRA_STEP = re.compile(
    r"(checkout|download|install|cache|container|runner|docker|setup|prepare|"
    r"recommend tests from coverage|ensure csrc cache)",
    re.I,
)

# ci-gate also enforces contribution policy. A missing ready-* label is a valid
# verdict that blocks the PR, but it is not CI unavailability.
GATE_POLICY = re.compile(
    r"(selected tests are required; add the .*ready.*label|"
    r"ready-precise or ready-all label|ready-a5.*(?:not set|required))",
    re.I,
)
GATE_CONTROL_PLANE = re.compile(
    r"(invalid or missing has_tests(?:_a5)? output|"
    r"run-selected-tests(?:-a5)? did not succeed \(result: (?:skipped|cancelled|startup_failure)\))",
    re.I,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_e2e_pr_run(run: dict[str, Any]) -> bool:
    return (
        run.get("event") == "pull_request"
        and (
            run.get("name") == "E2E"
            or (run.get("path") or "").endswith(E2E_PATH)
            or (run.get("path") or "").endswith("pr_test.yaml")
        )
    )


def list_pr_runs(gh: GH, start: datetime, end: datetime) -> list[dict[str, Any]]:
    span = f"{iso_ts(start)}..{iso_ts(end)}"
    params = {"event": "pull_request", "created": span, "per_page": 100, "page": 1}
    first = gh.get(f"/repos/{REPO}/actions/runs", params)
    expected = int(first.get("total_count") or 0)
    rows = list(first.get("workflow_runs", []))
    pages = min(10, max(1, (expected + 99) // 100))
    for page in range(2, pages + 1):
        if not gh.ok(8):
            break
        params["page"] = page
        payload = gh.get(f"/repos/{REPO}/actions/runs", params)
        rows.extend(payload.get("workflow_runs", []))
    dedup = {int(r["id"]): r for r in rows if r.get("id")}
    return [r for r in dedup.values() if is_e2e_pr_run(r) and r.get("status") == "completed"]


def list_jobs(gh: GH, run_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 10):
        if not gh.ok(8):
            break
        payload = gh.get(
            f"/repos/{REPO}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": 100, "page": page},
        )
        part = payload.get("jobs", [])
        out.extend(part)
        total = int(payload.get("total_count") or len(out))
        if not part or len(out) >= total:
            break
    return out


def job_key(name: str) -> str:
    return f"job::E2E::{name}"


def observations_mixed_for_sha(tests: dict[str, Any], name: str, sha: str | None) -> bool:
    if not sha:
        return False
    item = tests.get("tests", {}).get(job_key(name), {})
    outcomes: set[str] = set()
    for obs in item.get("observations", []):
        if obs.get("head_sha") != sha:
            continue
        conclusion = (obs.get("conclusion") or "").lower()
        if conclusion == "success":
            outcomes.add("success")
        elif conclusion in {"failure", "timed_out", "startup_failure"}:
            outcomes.add("failure")
    return len(outcomes) > 1


def direct_gate_job(name: str) -> bool:
    lower = (name or "").lower()
    return any(
        lower == prefix or lower.startswith(prefix + " ") or lower.startswith(prefix + " (")
        for prefix in DIRECT_GATE_PREFIXES
    )


def annotations(gh: GH, job_id: int) -> str:
    if not gh.ok(6):
        return ""
    try:
        payload = gh.get(f"/repos/{REPO}/check-runs/{job_id}/annotations", {"per_page": 100})
    except Exception:
        return ""
    pieces: list[str] = []
    for item in payload if isinstance(payload, list) else []:
        for key in ("title", "message", "raw_details"):
            value = str(item.get(key) or "").strip()
            if value and value not in pieces:
                pieces.append(value)
    return "\n".join(pieces)[:250000]


class LogReader:
    def __init__(self, token: str) -> None:
        self.token = token
        self.used = 0

    def read(self, job_id: int) -> str:
        if self.used >= LOG_CAP or not self.token:
            return ""
        self.used += 1
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vllm-ascend-ci-monitor-merge-gate/2",
            "Authorization": "Bearer " + self.token,
        }
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=LOG_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")[-500000:]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            return ""


def failed_step_text(job: dict[str, Any]) -> str:
    return " | ".join(x for x in failed_step_names(job) if x)


def last_matching_line(text: str, pattern: re.Pattern[str]) -> str | None:
    for raw in reversed((text or "").splitlines()[-5000:]):
        line = raw.strip()
        if line and pattern.search(line):
            return line[-400:]
    return None


def diagnose_deterministic_text(text: str) -> tuple[str, str, str | None] | None:
    """Diagnose a deterministic check without treating recovered setup warnings as root cause."""
    if not text:
        return None
    evidence = last_matching_line(text, DETERMINISTIC_CODE_LOG)
    if evidence:
        return "code", "DETERMINISTIC_CHECK_OR_TEST_FAILURE", evidence

    # Focus generic diagnosis on the tail where the failed step actually ran.
    # A transient action-download warning before the job reached this step is
    # not the cause of a later lint/pre-commit failure.
    tail = "\n".join(text.splitlines()[-1500:])
    why, infra, evidence = diagnose_log(tail)
    if not why:
        return None
    if infra and evidence and RECOVERED_ACTION_DOWNLOAD.search(evidence):
        return None
    return ("ci" if infra else "code"), why, evidence


def classify_leaf(
    gh: GH,
    logs: LogReader,
    tests: dict[str, Any],
    job: dict[str, Any],
    sha: str | None,
) -> tuple[str, str, str | None]:
    """Return (kind, reason, evidence), where kind is ci/code/unknown."""
    name = job.get("name") or "Unnamed job"
    conclusion = (job.get("conclusion") or "").lower()
    steps = failed_step_text(job)
    job_id = int(job.get("id") or 0)

    if conclusion == "startup_failure":
        return "ci", "STARTUP_FAILURE", "GitHub Actions startup_failure"

    # PR title is mutable metadata: a same-SHA fail/pass can be caused by an
    # author editing the title, so classify the actual title step before using
    # same-SHA mixed outcomes as flaky evidence.
    if TITLE_STEP.search(steps):
        text = logs.read(job_id)
        evidence = last_matching_line(text, TITLE_POLICY_LOG)
        if evidence:
            return "code", "PR_TITLE_POLICY_FAILURE", evidence
        diagnosed = diagnose_deterministic_text(text)
        if diagnosed:
            return diagnosed

        text = annotations(gh, job_id)
        evidence = last_matching_line(text, TITLE_POLICY_LOG)
        if evidence:
            return "code", "PR_TITLE_POLICY_FAILURE", evidence
        if text:
            why, infra, evidence = diagnose_log(text)
            if why and not (infra and evidence and RECOVERED_ACTION_DOWNLOAD.search(evidence)):
                return ("ci" if infra else "code"), why, evidence
        return "code", "PR_TITLE_POLICY_FAILURE", steps[:400]

    # Same code, same concrete required leaf job, both PASS and FAIL is direct
    # evidence that the merge signal itself is not deterministic. Title checks
    # are excluded above because PR metadata can change without a new SHA.
    if observations_mixed_for_sha(tests, name, sha):
        return "ci", "SAME_SHA_MIXED_OUTCOME", f"{name} failed and passed on the same commit"

    # For deterministic lint/check steps, inspect the final job log before
    # annotations. GitHub annotations can contain recovered setup warnings from
    # earlier in the job and must not override the step that actually failed.
    if DETERMINISTIC_META_STEP.search(steps):
        text = logs.read(job_id)
        diagnosed = diagnose_deterministic_text(text)
        if diagnosed:
            return diagnosed

        text = annotations(gh, job_id)
        evidence = last_matching_line(text, DETERMINISTIC_CODE_LOG)
        if evidence:
            return "code", "DETERMINISTIC_CHECK_OR_TEST_FAILURE", evidence
        if text:
            why, infra, evidence = diagnose_log(text)
            if why and not (infra and evidence and RECOVERED_ACTION_DOWNLOAD.search(evidence)):
                return ("ci" if infra else "code"), why, evidence
        return "code", "DETERMINISTIC_CHECK_OR_TEST_FAILURE", steps[:400]

    text = annotations(gh, job_id)
    if text:
        why, infra, evidence = diagnose_log(text)
        if why:
            return ("ci" if infra else "code"), why, evidence

    # Read only a bounded number of heavy logs, only for failed jobs on the
    # actual merge path.
    text = logs.read(job_id)
    if text:
        why, infra, evidence = diagnose_log(text)
        if why:
            return ("ci" if infra else "code"), why, evidence

    # Strong deterministic verdict steps are valid CI outcomes even without
    # raw logs. select-tests is intentionally excluded from this fallback.
    if CODE_STEP.search(steps):
        return "code", "DETERMINISTIC_CHECK_OR_TEST_FAILURE", steps[:400]

    if conclusion == "timed_out":
        if SETUP_OR_INFRA_STEP.search(steps):
            return "ci", "MERGE_PATH_INFRA_TIMEOUT", steps[:400]
        return "unknown", "MERGE_PATH_TIMEOUT_UNKNOWN", steps[:400] or None

    if SETUP_OR_INFRA_STEP.search(steps):
        return "unknown", "MERGE_PATH_SETUP_FAILURE_UNKNOWN", steps[:400]

    return "unknown", "MERGE_PATH_FAILURE_UNKNOWN", steps[:400] or None


def classify_gate_without_failed_leaf(
    gh: GH,
    logs: LogReader,
    gate: dict[str, Any],
) -> tuple[str, str, str | None]:
    """Classify a gate failure only after reading its own explanation."""
    job_id = int(gate.get("id") or 0)
    text = annotations(gh, job_id)
    if not text:
        text = logs.read(job_id)

    if text:
        match = GATE_POLICY.search(text)
        if match:
            return "policy", "MERGE_POLICY_REQUIRES_READY_LABEL", match.group(0)[:400]
        match = GATE_CONTROL_PLANE.search(text)
        if match:
            return "ci", "CI_GATE_CONTROL_PLANE_FAILURE", match.group(0)[:400]
        why, infra, evidence = diagnose_log(text)
        if why:
            return ("ci" if infra else "code"), why, evidence

    conclusion = (gate.get("conclusion") or "").lower()
    if conclusion == "startup_failure":
        return "ci", "CI_GATE_STARTUP_FAILURE", "ci-gate startup_failure"
    if conclusion == "timed_out":
        return "ci", "CI_GATE_TIMEOUT", failed_step_text(gate)[:400] or None

    # Suspicious but not proven infrastructure: gray, not red.
    return "unknown", "CI_GATE_FAILURE_UNEXPLAINED", failed_step_text(gate)[:400] or None


def ensure_fields(row: dict[str, Any]) -> None:
    row.setdefault("merge_gate_runs", 0)
    row.setdefault("merge_gate_code_failures", 0)
    row.setdefault("merge_gate_policy_failures", 0)
    row.setdefault("merge_blocking_ci_failures", 0)
    row.setdefault("merge_gate_unknown_failures", 0)
    row.setdefault("nonblocking_ci_failures", 0)
    row.setdefault("merge_gate_evidence", [])


def decide(row: dict[str, Any]) -> str:
    ensure_fields(row)
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


def main() -> int:
    now = utcnow()
    history = load(HISTORY, {"hours": []})
    tests = load(TESTS, {"tests": {}})
    rows = history.get("hours", [])
    by_hour = {row.get("hour"): row for row in rows if row.get("hour")}

    start = now - timedelta(hours=LOOKBACK)
    floor = start.replace(minute=0, second=0, microsecond=0)
    for key, row in by_hour.items():
        dt = parse_dt(key)
        if dt and dt >= floor:
            for field in (
                "merge_gate_runs",
                "merge_gate_code_failures",
                "merge_gate_policy_failures",
                "merge_blocking_ci_failures",
                "merge_gate_unknown_failures",
                "nonblocking_ci_failures",
            ):
                row[field] = 0
            row["merge_gate_evidence"] = []

    gh = GH()
    token = os.getenv("UPSTREAM_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    logs = LogReader(token)
    stats = Counter()

    try:
        runs = list_pr_runs(gh, start - timedelta(hours=6), now)
    except Exception as exc:
        print(f"warning: merge-gate run scan failed: {type(exc).__name__}: {exc}")
        return 0

    # Keep temporal order because label changes can legitimately change a gate
    # result on the same SHA.
    runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")

    for run in runs:
        run_id = int(run.get("id") or 0)
        sha = run.get("head_sha")
        try:
            jobs = list_jobs(gh, run_id)
        except Exception:
            continue

        gate_jobs = [job for job in jobs if (job.get("name") or "").lower() == GATE]
        if not gate_jobs:
            continue
        gate = gate_jobs[-1]
        gate_conclusion = (gate.get("conclusion") or "").lower()
        gate_time = (
            parse_dt(gate.get("completed_at"))
            or parse_dt(gate.get("started_at"))
            or parse_dt(run.get("updated_at"))
            or parse_dt(run.get("created_at"))
        )
        if not gate_time or gate_time < floor:
            continue
        key = iso_hour(gate_time)
        row = by_hour.get(key)
        if not row:
            continue
        ensure_fields(row)
        row["merge_gate_runs"] += 1

        if gate_conclusion == "success":
            stats["gate_success"] += 1
            continue
        if gate_conclusion not in {"failure", "timed_out", "startup_failure"}:
            continue

        stats["gate_failure"] += 1
        leaf_failures = [
            job
            for job in jobs
            if (job.get("conclusion") or "").lower() in {"failure", "timed_out", "startup_failure"}
            and (job.get("name") or "").lower() != GATE
            and direct_gate_job(job.get("name") or "")
        ]
        results = [classify_leaf(gh, logs, tests, job, sha) for job in leaf_failures]

        if any(item[0] == "ci" for item in results):
            kind = "ci"
            first = next(item for item in results if item[0] == "ci")
            reason, evidence = first[1], first[2]
        elif results and all(item[0] == "code" for item in results):
            kind = "code"
            reason, evidence = results[0][1], results[0][2]
        elif not leaf_failures:
            kind, reason, evidence = classify_gate_without_failed_leaf(gh, logs, gate)
        else:
            kind = "unknown"
            first = next(
                (item for item in results if item[0] == "unknown"),
                ("unknown", "MERGE_PATH_FAILURE_UNKNOWN", None),
            )
            reason, evidence = first[1], first[2]

        item = {
            "run_id": run_id,
            "sha": sha,
            "reason": reason,
            "evidence": evidence,
            "url": gate.get("html_url") or run.get("html_url"),
        }
        if kind == "ci":
            row["merge_blocking_ci_failures"] += 1
            stats["merge_blocking_ci"] += 1
        elif kind == "code":
            row["merge_gate_code_failures"] += 1
            stats["code_verdict"] += 1
        elif kind == "policy":
            row["merge_gate_policy_failures"] += 1
            stats["policy_verdict"] += 1
        else:
            row["merge_gate_unknown_failures"] += 1
            stats["unknown_gate"] += 1
        if len(row["merge_gate_evidence"]) < 12:
            item["kind"] = kind
            row["merge_gate_evidence"].append(item)

    # A single proven CI-caused required-check blockage is red. Non-gating
    # infra is yellow. Valid PR-code/policy failures remain green.
    for row in rows:
        dt = parse_dt(row.get("hour"))
        if dt and dt >= floor:
            row["status"] = decide(row)

    history["schema_version"] = max(11, int(history.get("schema_version") or 0))
    history["merge_availability_policy"] = {
        "required_ci_check": "ci-gate",
        "required_status_checks_source": "public repository ruleset main",
        "down": "at least one PR ci-gate is blocked by a proven CI/reliability fault rather than PR code or merge policy",
        "degraded": "proven CI infrastructure issue outside the merge-blocking path",
        "unknown": "merge-gate failure exists but evidence cannot distinguish CI/reliability from PR code/policy",
        "healthy": "no proven merge-blocking CI fault; code and ready-label policy failures are valid CI verdicts",
    }
    history["merge_availability_updated_at"] = iso_ts(now)
    save(HISTORY, history)
    print(
        "merge-gate "
        + " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        + f" log_reads={logs.used} api_requests={gh.requests}/{gh.budget}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

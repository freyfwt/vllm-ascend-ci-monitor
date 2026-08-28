#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collect import (
    GH, REPO, DOWNLOAD_LOG, NETWORK_LOG, RUNNER_LOG,
    classify_failure, failed_step_names, is_ci_run, iso_ts, parse_dt,
)

DATA = Path("data")
OUT = DATA / "blockers.json"
STATE = DATA / "blocker_state.json"
SCHEMA = 3
EVENTS = ("push", "schedule", "workflow_dispatch", "repository_dispatch", "workflow_run")
BOOT = int(os.getenv("BLOCKER_BOOTSTRAP_HOURS", "168"))
LOOK = int(os.getenv("BLOCKER_LOOKBACK_HOURS", "8"))
KEEP = int(os.getenv("BLOCKER_STATE_DAYS", "60"))
EVIDENCE_CAP = int(os.getenv("BLOCKER_EVIDENCE_CAP", "64"))
FLOOR = os.getenv("BLOCKER_TRACKING_FLOOR", "2026-08-01T00:00:00Z")
API = "https://api.github.com"
PASS = {"success"}
FAIL = {"failure", "timed_out", "startup_failure"}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TS = re.compile(r"^\s*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*")
ERR = re.compile(r"(traceback|exception|error|failed|fatal|assert|segmentation|timeout|no such file|not found|unavailable|refused|reset by peer|cannot |could not |out of memory|crashloop|evicted|acl_error|runtimeerror|valueerror|typeerror|modulenotfounderror|connectionerror|readtimeout)", re.I)
GEN = re.compile(r"^(?:Error:\s*)?(?:Process completed with exit code \d+\.?|Job failed\.?)$", re.I)
DYN = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,})\b", re.I)
BIG = re.compile(r"(?<![\w.-])\d{5,}(?![\w.-])")


def utcnow(): return datetime.now(timezone.utc)
def load(path, default):
    try: return json.loads(path.read_text()) if path.exists() else default
    except Exception: return default
def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n"); tmp.replace(path)
def norm(text):
    text=ANSI.sub("",TS.sub("",text)).replace("##[error]","").strip(); text=DYN.sub("<id>",text); text=BIG.sub("<num>",text); return re.sub(r"\s+"," ",text)[:500]
def extract_evidence(text,fallback):
    lines=[]
    for raw in (text or "").splitlines()[-4000:]:
        line=norm(raw)
        if line and ERR.search(line) and not GEN.match(line) and line not in lines: lines.append(line)
    lines=lines[-5:]; sig=(lines[-1] if lines else norm(fallback))[:260] or "CI job failure"; return sig,("\n".join(lines)[:1500] if lines else sig)
def issue_id(workflow,job,step,signature): return hashlib.sha1("\x1f".join((workflow.lower(),job.lower(),step.lower(),signature.lower())).encode()).hexdigest()[:16]
def job_key(workflow,job): return f"{workflow}::{job}"
def category(reason,infra):
    if infra:return "infrastructure"
    if reason in {"TEST_FAILURE","TEST_TIMEOUT","BUILD_OR_CHECK_FAILURE"}:return "code"
    return "unresolved"


def main_span(gh,event,start,end,depth=0):
    if not gh.ok(8): return {},False,{"expected":None,"fetched":0,"complete":False,"slices":0}
    span=f"{iso_ts(start)}..{iso_ts(end)}"; params={"event":event,"branch":"main","created":span,"per_page":100,"page":1}; first=gh.get(f"/repos/{REPO}/actions/runs",params); expected=int(first.get("total_count") or 0); rows=first.get("workflow_runs",[])
    if expected>950 and end-start>timedelta(minutes=15) and depth<12:
        mid=start+(end-start)/2; left,lok,lm=main_span(gh,event,start,mid,depth+1); right,rok,rm=main_span(gh,event,mid+timedelta(seconds=1),end,depth+1); left.update(right); ok=lok and rok
        return left,ok,{"expected":(lm.get("expected") or 0)+(rm.get("expected") or 0),"fetched":len(left),"complete":ok,"slices":(lm.get("slices") or 1)+(rm.get("slices") or 1)}
    out={int(r["id"]):r for r in rows if r.get("id")}; pages=min(max(1,math.ceil(expected/100)),10)
    for page in range(2,pages+1):
        if not gh.ok(8):break
        params["page"]=page
        for r in gh.get(f"/repos/{REPO}/actions/runs",params).get("workflow_runs",[]):
            if r.get("id"):out[int(r["id"])]=r
    ok=len(out)>=expected; return out,ok,{"expected":expected,"fetched":len(out),"complete":ok,"slices":1}

def main_runs(gh,start,end):
    merged={}; coverage={}; complete=True
    for event in EVENTS:
        if not gh.ok(15):complete=False;break
        rows,ok,meta=main_span(gh,event,start,end); coverage[event]=meta; complete=complete and ok; merged.update({i:r for i,r in rows.items() if r.get("head_branch")=="main" and is_ci_run(r)})
    rows=[r for r in merged.values() if r.get("status")=="completed" and r.get("conclusion")!="skipped"]; rows.sort(key=lambda r:r.get("updated_at") or r.get("created_at") or ""); return rows,complete,coverage

def list_jobs(gh,run_id):
    out=[]
    for page in range(1,15):
        p=gh.get(f"/repos/{REPO}/actions/runs/{run_id}/jobs",{"filter":"latest","per_page":100,"page":page}); part=p.get("jobs",[]); out+=part
        if not part or len(out)>=int(p.get("total_count") or len(out)):break
    return out

def http_get(url,token="",accept="application/vnd.github+json"):
    headers={"Accept":accept,"X-GitHub-Api-Version":"2022-11-28","User-Agent":"vllm-ascend-ci-monitor/blockers"}
    if token:headers["Authorization"]="Bearer "+token
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20) as response:return response.read()

def annotation_text(event,stats):
    if stats["evidence_attempted"]>=EVIDENCE_CAP:return ""
    job_id=int(event.get("job_id") or 0)
    if not job_id:return ""
    stats["evidence_attempted"]+=1; stats["annotations_attempted"]+=1
    try:
        payload=json.loads(http_get(f"{API}/repos/{REPO}/check-runs/{job_id}/annotations?per_page=100").decode()); stats["annotations_succeeded"]+=1; pieces=[]
        for item in payload if isinstance(payload,list) else []:
            for key in ("title","message","raw_details"):
                v=(item.get(key) or "").strip()
                if v and v not in pieces:pieces.append(v)
        if pieces:stats["annotations_with_text"]+=1;return "\n".join(pieces)[:300000]
    except Exception:stats["annotations_failed"]+=1
    return ""

def token_job_log(event,stats):
    token=os.getenv("UPSTREAM_GITHUB_TOKEN") or ""
    if not token or stats["evidence_attempted"]>=EVIDENCE_CAP:return ""
    job_id=int(event.get("job_id") or 0)
    if not job_id:return ""
    stats["evidence_attempted"]+=1;stats["job_logs_attempted"]+=1
    try:stats["job_logs_succeeded"]+=1;return http_get(f"{API}/repos/{REPO}/actions/jobs/{job_id}/logs",token,"application/octet-stream").decode("utf-8",errors="replace")[-300000:]
    except Exception:stats["job_logs_failed"]+=1;return ""

def rich_evidence(event,stats,sampled=True):
    if not sampled:return "","step_summary"
    annotations=annotation_text(event,stats); full=token_job_log(event,stats) if os.getenv("UPSTREAM_GITHUB_TOKEN") else ""
    if full:return full,"job_log"
    if annotations:return annotations,"annotations"
    return "","step_summary"


def pr_for_sha(gh,sha,first_seen):
    if not sha or not first_seen or not gh.ok(8):return None
    try:pulls=gh.get(f"/repos/{REPO}/commits/{sha}/pulls",{"per_page":100})
    except Exception:return None
    pulls=[p for p in pulls if p.get("merged_at") and p.get("merge_commit_sha")==sha]
    if len(pulls)!=1:return None
    try:pr=gh.get(f"/repos/{REPO}/pulls/{pulls[0]['number']}")
    except Exception:return None
    merged=parse_dt(pr.get("merged_at"));first=parse_dt(first_seen)
    if not merged or not first or first<merged or first-merged>timedelta(hours=6):return None
    files=[]
    try:files=[x.get("filename") for x in gh.get(f"/repos/{REPO}/pulls/{pr['number']}/files",{"per_page":100}) if x.get("filename")][:20]
    except Exception:pass
    return {"number":pr["number"],"title":pr.get("title"),"url":pr.get("html_url"),"author":(pr.get("user") or {}).get("login"),"merged_by":(pr.get("merged_by") or {}).get("login"),"merged_at":pr.get("merged_at"),"merge_commit_sha":pr.get("merge_commit_sha"),"changed_files":files,"confidence":"high","rationale":"previous_3_main_passes_then_first_failure_on_merge_commit_then_reproduced"}

class NoLogGH:
    def ok(self,reserve=0):return False

def compact_event(run,job,workflow):
    conclusion=(job.get("conclusion") or "").lower()
    if conclusion not in PASS|FAIL:return None
    at=job.get("completed_at") or job.get("started_at") or run.get("updated_at") or run.get("created_at")
    if not at:return None
    name=job.get("name") or "Unnamed job"; event={"at":at,"outcome":"success" if conclusion in PASS else "failure","conclusion":conclusion,"sha":run.get("head_sha"),"run_id":int(run.get("id") or 0),"job_id":int(job.get("id") or 0),"workflow":workflow,"job":name,"job_key":job_key(workflow,name),"run_url":run.get("html_url"),"job_url":job.get("html_url") or run.get("html_url")}
    if conclusion in FAIL:
        event["failed_step"]=(failed_step_names(job) or ["(job failure)"])[0];reason,infra,_=classify_failure(NoLogGH(),job,allow_log=False);event["reason"]=reason;event["infra"]=bool(infra)
    return event

def unresolved_segment(history):
    ordered=sorted(history,key=lambda x:x.get("at") or "");recovery=0;streak=0
    for i,event in enumerate(ordered):
        if event.get("outcome")=="success":streak+=1;recovery=i+1 if streak>=3 else recovery
        elif event.get("outcome")=="failure":streak=0
    segment=ordered[recovery:];return segment if any(x.get("outcome")=="failure" for x in segment) else []
def prior_three_pass(history,first_seen):
    earlier=[x for x in sorted(history,key=lambda y:y.get("at") or "") if (x.get("at") or "")<first_seen and x.get("outcome") in {"success","failure"}];return len(earlier)>=3 and all(x.get("outcome")=="success" for x in earlier[-3:])
def issue_from_event(event,stats,sampled=True):
    text,source=rich_evidence(event,stats,sampled);reason=event.get("reason") or "CODE_OR_UNKNOWN_FAILURE";infra=bool(event.get("infra"))
    if text:
        if DOWNLOAD_LOG.search(text):reason,infra="DOWNLOAD",True
        elif NETWORK_LOG.search(text):reason,infra="NETWORK",True
        elif RUNNER_LOG.search(text):reason,infra="RUNNER",True
    fallback=" | ".join((event.get("job") or "Unnamed job",event.get("failed_step") or "(job failure)",event.get("conclusion") or "failure"));sig,excerpt=extract_evidence(text,fallback);ident=issue_id(event.get("workflow") or "Unnamed workflow",event.get("job") or "Unnamed job",event.get("failed_step") or "(job failure)",sig)
    return {"id":ident,"status":"open","category":category(reason,infra),"classification":reason,"workflow":event.get("workflow"),"job":event.get("job"),"job_key":event.get("job_key"),"failed_step":event.get("failed_step"),"signature":sig,"log_excerpt":excerpt,"evidence_source":source,"first_seen":event.get("at"),"first_sha":event.get("sha"),"last_seen":event.get("at"),"occurrences":1,"pass_streak":0,"latest_url":event.get("job_url"),"latest_run_url":event.get("run_url"),"affected_commits":[event["sha"]] if event.get("sha") else [],"affected_runs":[event["run_id"]] if event.get("run_id") else [],"signals":[]},ident

def merge_failure(issue,event,candidate):
    issue["status"]="open";issue.pop("resolved_at",None)
    if (event.get("at") or "")<(issue.get("first_seen") or event.get("at") or ""):issue["first_seen"]=event.get("at");issue["first_sha"]=event.get("sha")
    if (event.get("at") or "")>=(issue.get("last_seen") or ""):
        issue["last_seen"]=event.get("at");issue["latest_url"]=event.get("job_url");issue["latest_run_url"]=event.get("run_url")
        if candidate.get("evidence_source")!="step_summary":issue["log_excerpt"]=candidate.get("log_excerpt");issue["signature"]=candidate.get("signature");issue["evidence_source"]=candidate.get("evidence_source")
    issue["occurrences"]=int(issue.get("occurrences") or 0)+1;issue["pass_streak"]=0;sha=event.get("sha")
    if sha and sha not in issue.setdefault("affected_commits",[]):issue["affected_commits"]=(issue["affected_commits"]+[sha])[-20:]
    rid=event.get("run_id")
    if rid and rid not in issue.setdefault("affected_runs",[]):issue["affected_runs"]=(issue["affected_runs"]+[rid])[-30:]
def close_or_increment(issues,event):
    for issue in issues.values():
        if issue.get("status")!="open" or issue.get("job_key")!=event.get("job_key") or (event.get("at") or "")<=(issue.get("last_seen") or ""):continue
        issue["pass_streak"]=int(issue.get("pass_streak") or 0)+1
        if issue["pass_streak"]>=3:issue["status"]="resolved";issue["resolved_at"]=event.get("at")
def reset_job_issues(issues,key):
    for issue in issues.values():
        if issue.get("status")=="open" and issue.get("job_key")==key:issue["pass_streak"]=0

def bootstrap_open_issues(gh,state,stats):
    issues=state["issues"];histories=state["job_history"];candidates=[]
    for key,history in histories.items():
        segment=unresolved_segment(history)
        if any(x.get("outcome")=="failure" for x in segment):candidates.append((key,segment))
    candidates.sort(key=lambda item:max((x.get("at") or "" for x in item[1]),default=""),reverse=True)
    for key,segment in candidates:
        failures=[x for x in segment if x.get("outcome")=="failure"];passes_after=0
        for event in reversed(segment):
            if event.get("outcome")=="success":passes_after+=1
            elif event.get("outcome")=="failure":break
        remaining=EVIDENCE_CAP-stats["evidence_attempted"]
        if len(failures)>1 and remaining>=2:samples=[failures[0],failures[-1]];verified_edges=True
        else:samples=[failures[-1]];verified_edges=len(failures)==1
        built=[(issue_from_event(event,stats,stats["evidence_attempted"]<EVIDENCE_CAP)[0],event) for event in samples]
        if len(built)==2 and built[0][0]["id"]==built[1][0]["id"]:
            issue=built[0][0];issue["occurrences"]=len(failures);issue["last_seen"]=failures[-1].get("at");issue["latest_url"]=failures[-1].get("job_url");issue["latest_run_url"]=failures[-1].get("run_url");issue["pass_streak"]=passes_after;issue["affected_commits"]=list(dict.fromkeys(x.get("sha") for x in failures if x.get("sha")))[-20:];issue["affected_runs"]=list(dict.fromkeys(x.get("run_id") for x in failures if x.get("run_id")))[-30:]
            if built[1][0].get("evidence_source")!="step_summary":issue["log_excerpt"]=built[1][0].get("log_excerpt");issue["evidence_source"]=built[1][0].get("evidence_source")
            issues[issue["id"]]=issue
            if issue.get("classification") in {"TEST_FAILURE","TEST_TIMEOUT"} and len(failures)>=2 and verified_edges and issue.get("evidence_source")!="step_summary" and prior_three_pass(histories[key],issue["first_seen"]):
                pr=pr_for_sha(gh,issue.get("first_sha"),issue.get("first_seen"))
                if pr:issue["introduced_by"]=pr
        else:
            for issue,_ in built:issue["pass_streak"]=passes_after;issues[issue["id"]]=issue
        if len(failures)>len(samples):
            for issue,_ in built:issue["bootstrap_failure_count"]=len(failures);issue["evidence_sampling"]="first_and_latest_when_budget_allows"

def public_issue(issue):
    keys=("id","status","category","classification","workflow","job","failed_step","signature","log_excerpt","evidence_source","first_seen","last_seen","occurrences","bootstrap_failure_count","evidence_sampling","pass_streak","resolved_at","latest_url","latest_run_url","affected_commits","affected_runs","signals","introduced_by");return {k:issue.get(k) for k in keys if issue.get(k) is not None}

def run():
    parser=argparse.ArgumentParser();parser.add_argument("--since");parser.add_argument("--rebuild",action="store_true");args=parser.parse_args();current=utcnow();state=load(STATE,{});fresh=args.rebuild or int(state.get("schema_version") or 0)<SCHEMA
    if fresh:state={"schema_version":SCHEMA,"seen":{},"issues":{},"job_history":{}}
    for key in ("seen","issues","job_history"):state.setdefault(key,{})
    if args.since:raw=args.since if "T" in args.since else args.since+"T00:00:00Z";start=parse_dt(raw) or current-timedelta(hours=BOOT)
    elif not state.get("last_scan_at"):start=max(parse_dt(FLOOR) or current-timedelta(hours=BOOT),current-timedelta(hours=BOOT))
    else:start=current-timedelta(hours=LOOK)
    gh=GH();errors=[];stats={"evidence_attempted":0,"annotations_attempted":0,"annotations_succeeded":0,"annotations_with_text":0,"annotations_failed":0,"job_logs_attempted":0,"job_logs_succeeded":0,"job_logs_failed":0}
    try:runs,complete,coverage=main_runs(gh,start,current)
    except Exception as exc:runs,complete,coverage=[],False,{};errors.append(f"runs: {type(exc).__name__}: {exc}")
    issues=state["issues"];batch=defaultdict(set);run_count=job_count=0
    for run_obj in runs:
        if not gh.ok(15):complete=False;errors.append("request budget reached");break
        rid=int(run_obj.get("id") or 0)
        try:jobs=list_jobs(gh,rid)
        except Exception as exc:complete=False;errors.append(f"jobs:{rid}: {type(exc).__name__}: {exc}");continue
        run_count+=1;workflow=run_obj.get("name") or "Unnamed workflow"
        for job in jobs:
            jid=str(job.get("id") or "")
            if not jid or jid in state["seen"]:continue
            event=compact_event(run_obj,job,workflow);seen_at=job.get("completed_at") or job.get("started_at") or run_obj.get("updated_at") or iso_ts(current);state["seen"][jid]=seen_at
            if event is None:continue
            job_count+=1;key=event["job_key"];history=state["job_history"].setdefault(key,[]);history.append(event);batch[(key,event.get("sha") or "")].add(event["outcome"])
            if fresh:continue
            if event["outcome"]=="success":close_or_increment(issues,event);continue
            reset_job_issues(issues,key);candidate,ident=issue_from_event(event,stats,stats["evidence_attempted"]<EVIDENCE_CAP);existing=issues.get(ident)
            if existing:merge_failure(existing,event,candidate);issue=existing
            else:issues[ident]=candidate;issue=candidate
            if issue.get("classification") in {"TEST_FAILURE","TEST_TIMEOUT"} and issue.get("occurrences",0)>=2 and issue.get("evidence_source")!="step_summary" and prior_three_pass(history,issue["first_seen"]) and not issue.get("introduced_by"):
                pr=pr_for_sha(gh,issue.get("first_sha"),issue.get("first_seen"))
                if pr:issue["introduced_by"]=pr
    if fresh:bootstrap_open_issues(gh,state,stats)
    for (key,sha),outcomes in batch.items():
        if sha and outcomes=={"success","failure"}:
            for issue in issues.values():
                if issue.get("status")=="open" and issue.get("job_key")==key and sha in issue.get("affected_commits",[]):
                    signals=issue.setdefault("signals",[])
                    if "same_commit_mixed_outcomes" not in signals:signals.append("same_commit_mixed_outcomes")
                    if issue.get("category")!="infrastructure":issue["category"]="flaky";issue["classification"]="FLAKY";issue.pop("introduced_by",None)
    cutoff=current-timedelta(days=KEEP);state["seen"]={k:v for k,v in state["seen"].items() if (parse_dt(v) or current)>=cutoff}
    for key,history in list(state["job_history"].items()):state["job_history"][key]=[item for item in history if (parse_dt(item.get("at")) or current)>=cutoff][-500:]
    open_issues=[public_issue(i) for i in issues.values() if i.get("status")=="open"];resolved=[public_issue(i) for i in issues.values() if i.get("status")=="resolved"];open_issues.sort(key=lambda i:(-int(i.get("occurrences") or 0),i.get("first_seen") or ""));resolved.sort(key=lambda i:i.get("resolved_at") or "",reverse=True);resolved=resolved[:120]
    output={"schema_version":SCHEMA,"updated_at":iso_ts(current),"upstream_repo":REPO,"scope":{"branch":"main","close_after_consecutive_passes":3,"pass_definition":"job conclusion == success","pr_attribution":"high-confidence test regressions with concrete error evidence only"},"analysis":{"complete":complete,"window_start":iso_ts(start),"window_end":iso_ts(current),"runs":run_count,"jobs":job_count,"api_requests":gh.requests,"request_budget":gh.budget,"evidence_requests":stats,"coverage":coverage,"errors":errors[-10:]},"stats":{"open":len(open_issues),"attributed":sum(bool(i.get("introduced_by")) for i in open_issues),"flaky":sum(i.get("category")=="flaky" for i in open_issues),"infrastructure":sum(i.get("category")=="infrastructure" for i in open_issues),"resolved":len(resolved)},"open":open_issues,"resolved":resolved}
    state.update(schema_version=SCHEMA,updated_at=iso_ts(current),last_scan_at=iso_ts(current));save(OUT,output);save(STATE,state);print(f"main_runs={run_count} jobs={job_count} open={len(open_issues)} resolved={len(resolved)} attributed={output['stats']['attributed']} annotations={stats['annotations_with_text']}/{stats['annotations_attempted']} logs={stats['job_logs_succeeded']}/{stats['job_logs_attempted']} requests={gh.requests}/{gh.budget} complete={complete}")
    for error in errors:print("warning:",error,file=sys.stderr)
    return 0

if __name__=="__main__":raise SystemExit(run())

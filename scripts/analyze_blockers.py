#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collect import (GH, REPO, FAILURE_CONCLUSIONS, SUCCESS, DOWNLOAD_LOG, NETWORK_LOG, RUNNER_LOG,
                     classify_failure, failed_step_names, is_ci_run, iso_ts, parse_dt, _run_span)

DATA=Path('data'); OUT=DATA/'blockers.json'; STATE=DATA/'blocker_state.json'; SCHEMA=1
EVENTS=('push','schedule','workflow_dispatch','repository_dispatch','workflow_run')
BOOT=int(os.getenv('BLOCKER_BOOTSTRAP_HOURS','168')); LOOK=int(os.getenv('BLOCKER_LOOKBACK_HOURS','8'))
KEEP=int(os.getenv('BLOCKER_STATE_DAYS','60')); FLOOR=os.getenv('BLOCKER_TRACKING_FLOOR','2026-08-01T00:00:00Z')
ANSI=re.compile(r'\x1b\[[0-9;]*[A-Za-z]'); TS=re.compile(r'^\s*\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*')
ERR=re.compile(r'(traceback|exception|error|failed|fatal|assert|segmentation|timeout|no such file|not found|unavailable|refused|reset by peer|cannot |could not |out of memory|crashloop|evicted|acl_error|runtimeerror|valueerror|typeerror|modulenotfounderror|connectionerror|readtimeout)',re.I)
GEN=re.compile(r'^(?:Error:\s*)?(?:Process completed with exit code \d+\.?|Job failed\.?)$',re.I)
DYN=re.compile(r'\b(?:0x[0-9a-f]+|[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,})\b',re.I); BIG=re.compile(r'(?<![\w.-])\d{5,}(?![\w.-])')

def now(): return datetime.now(timezone.utc)
def load(p,d):
    try:return json.loads(p.read_text()) if p.exists() else d
    except Exception:return d
def save(p,v): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def norm(s):
    s=ANSI.sub('',TS.sub('',s)).replace('##[error]','').strip(); s=DYN.sub('<id>',s); s=BIG.sub('<num>',s); return re.sub(r'\s+',' ',s)[:500]
def evidence(text,fallback):
    xs=[]
    for raw in (text or '').splitlines()[-4000:]:
        s=norm(raw)
        if s and ERR.search(s) and not GEN.match(s) and s not in xs: xs.append(s)
    xs=xs[-5:]; sig=(xs[-1] if xs else norm(fallback))[:260] or 'CI job failure'; return sig,'\n'.join(xs)[:1500] or sig
def iid(w,j,st,sig): return hashlib.sha1('\x1f'.join((w.lower(),j.lower(),st.lower(),sig.lower())).encode()).hexdigest()[:16]
def jkey(w,j): return f'{w}::{j}'
def cat(reason,infra):
    if infra:return 'infrastructure'
    if reason in {'TEST_FAILURE','TEST_TIMEOUT','BUILD_OR_CHECK_FAILURE'}:return 'code'
    return 'unresolved'
def main_runs(gh,start,end):
    out={}; cov={}; complete=True
    for ev in EVENTS:
        if not gh.ok(15): complete=False; break
        rows,ok,meta=_run_span(gh,ev,start,end); cov[ev]=meta; complete &= ok
        out.update({i:r for i,r in rows.items() if r.get('head_branch')=='main' and is_ci_run(r)})
    rows=[r for r in out.values() if r.get('status')=='completed' and r.get('conclusion')!='skipped']
    rows.sort(key=lambda r:r.get('updated_at') or r.get('created_at') or ''); return rows,complete,cov
def jobs(gh,rid):
    out=[]
    for page in range(1,15):
        p=gh.get(f'/repos/{REPO}/actions/runs/{rid}/jobs',{'filter':'latest','per_page':100,'page':page}); part=p.get('jobs',[]); out+=part
        if not part or len(out)>=int(p.get('total_count') or len(out)):break
    return out
def log(gh,job):
    try:return gh.text(f"/repos/{REPO}/actions/jobs/{int(job['id'])}/logs")[-300000:] if gh.ok(8) else ''
    except Exception:return ''
def pr_for_sha(gh,sha,seen):
    if not sha or not gh.ok(8):return None
    try: ps=gh.get(f'/repos/{REPO}/commits/{sha}/pulls',{'per_page':100})
    except Exception:return None
    ps=[p for p in ps if p.get('merged_at') and p.get('merge_commit_sha')==sha]
    if len(ps)!=1:return None
    try:p=gh.get(f"/repos/{REPO}/pulls/{ps[0]['number']}")
    except Exception:return None
    m=parse_dt(p.get('merged_at')); f=parse_dt(seen)
    if not m or not f or f<m or f-m>timedelta(hours=6):return None
    fs=[]
    try:fs=[x.get('filename') for x in gh.get(f"/repos/{REPO}/pulls/{p['number']}/files",{'per_page':100}) if x.get('filename')][:20]
    except Exception:pass
    return {'number':p['number'],'title':p.get('title'),'url':p.get('html_url'),'author':(p.get('user') or {}).get('login'),
            'merged_by':(p.get('merged_by') or {}).get('login'),'merged_at':p.get('merged_at'),'merge_commit_sha':p.get('merge_commit_sha'),
            'changed_files':fs,'confidence':'high','rationale':'previous_3_main_passes_then_first_failure_on_merge_commit_then_reproduced'}
def prior3(state,issue):
    xs=[x for x in state['job_history'].get(issue['job_key'],[]) if x.get('at','')<issue['first_seen'] and x.get('outcome') in {'success','failure'}]
    return len(xs)>=3 and all(x['outcome']=='success' for x in xs[-3:])
def attribute(gh,state,issue):
    if issue.get('introduced_by') or issue.get('status')!='open' or issue.get('classification') not in {'TEST_FAILURE','TEST_TIMEOUT'}:return
    if issue.get('occurrences',0)<2 or not issue.get('failed_step') or not prior3(state,issue):return
    p=pr_for_sha(gh,issue.get('first_sha'),issue['first_seen'])
    if p:issue['introduced_by']=p
def public(x):
    ks=('id','status','category','classification','workflow','job','failed_step','signature','log_excerpt','first_seen','last_seen','occurrences','pass_streak','resolved_at','latest_url','latest_run_url','affected_commits','affected_runs','signals','introduced_by')
    return {k:x.get(k) for k in ks if x.get(k) is not None}

def run():
    ap=argparse.ArgumentParser(); ap.add_argument('--since'); ap.add_argument('--rebuild',action='store_true'); a=ap.parse_args(); t=now()
    state=load(STATE,{'schema_version':SCHEMA,'seen':{},'issues':{},'job_history':{}})
    if a.rebuild:state={'schema_version':SCHEMA,'seen':{},'issues':{},'job_history':{}}
    for k,d in [('seen',{}),('issues',{}),('job_history',{})]:state.setdefault(k,d)
    if a.since:start=parse_dt(a.since if 'T' in a.since else a.since+'T00:00:00Z') or t-timedelta(hours=BOOT)
    elif not state.get('last_scan_at'):start=max(parse_dt(FLOOR) or t-timedelta(hours=BOOT),t-timedelta(hours=BOOT))
    else:start=t-timedelta(hours=LOOK)
    gh=GH(); errors=[]
    try:runs,complete,cov=main_runs(gh,start,t)
    except Exception as e:runs,complete,cov=[],False,{};errors.append(f'runs: {e}')
    issues=state['issues']; batch=defaultdict(set); nr=nj=0
    for r in runs:
        if not gh.ok(15):complete=False;errors.append('request budget reached');break
        rid=int(r.get('id') or 0)
        try: js=jobs(gh,rid)
        except Exception as e:errors.append(f'jobs:{rid}: {e}');complete=False;continue
        nr+=1; w=r.get('name') or 'Unnamed workflow'
        for j in js:
            jid=str(j.get('id') or '')
            if not jid or jid in state['seen']:continue
            c=(j.get('conclusion') or '').lower(); at=j.get('completed_at') or j.get('started_at') or r.get('updated_at') or iso_ts(t)
            if c not in SUCCESS|FAILURE_CONCLUSIONS:state['seen'][jid]=at;continue
            name=j.get('name') or 'Unnamed job'; sha=r.get('head_sha'); jk=jkey(w,name); hist=state['job_history'].setdefault(jk,[])
            ev={'at':at,'outcome':'success' if c in SUCCESS else 'failure','sha':sha,'run_id':rid,'job_id':int(j.get('id') or 0)}
            if c in SUCCESS:
                hist.append(ev);batch[(jk,sha or '')].add('success')
                for q in issues.values():
                    if q.get('status')=='open' and q.get('job_key')==jk and at>(q.get('last_seen') or ''):
                        q['pass_streak']=q.get('pass_streak',0)+1
                        if q['pass_streak']>=3:q['status']='resolved';q['resolved_at']=at
                state['seen'][jid]=at;nj+=1;continue
            step=(failed_step_names(j) or ['(job failure)'])[0]; reason,infra,_=classify_failure(gh,j,allow_log=False); txt=log(gh,j)
            if DOWNLOAD_LOG.search(txt):reason,infra='DOWNLOAD',True
            elif NETWORK_LOG.search(txt):reason,infra='NETWORK',True
            elif RUNNER_LOG.search(txt):reason,infra='RUNNER',True
            sig,ex=evidence(txt,' | '.join((name,step,c))); id_=iid(w,name,step,sig); q=issues.setdefault(id_,{'id':id_,'status':'open','category':cat(reason,infra),'classification':reason,'workflow':w,'job':name,'job_key':jk,'failed_step':step,'signature':sig,'log_excerpt':ex,'first_seen':at,'first_sha':sha,'occurrences':0,'pass_streak':0,'affected_commits':[],'affected_runs':[],'signals':[]})
            if q.get('status')=='resolved':q['status']='open';q.pop('resolved_at',None)
            q.update(last_seen=at,latest_url=j.get('html_url') or r.get('html_url'),latest_run_url=r.get('html_url'),log_excerpt=ex or q.get('log_excerpt'));q['occurrences']=q.get('occurrences',0)+1;q['pass_streak']=0
            if sha and sha not in q['affected_commits']:q['affected_commits']=(q['affected_commits']+[sha])[-20:]
            if rid not in q['affected_runs']:q['affected_runs']=(q['affected_runs']+[rid])[-30:]
            hist.append({**ev,'issue_id':id_,'signature':sig});batch[(jk,sha or '')].add('failure')
            for z in issues.values():
                if z.get('status')=='open' and z.get('job_key')==jk:z['pass_streak']=0
            state['seen'][jid]=at;nj+=1
    for (jk,sha),outs in batch.items():
        if sha and outs=={'success','failure'}:
            for q in issues.values():
                if q.get('status')=='open' and q.get('job_key')==jk and sha in q.get('affected_commits',[]):
                    if 'same_commit_mixed_outcomes' not in q['signals']:q['signals'].append('same_commit_mixed_outcomes')
                    if q['category']!='infrastructure':q['category']='flaky';q['classification']='FLAKY'
    for q in sorted(issues.values(),key=lambda x:x.get('first_seen','')):attribute(gh,state,q)
    cut=t-timedelta(days=KEEP);state['seen']={k:v for k,v in state['seen'].items() if (parse_dt(v) or t)>=cut}
    for k,xs in list(state['job_history'].items()):state['job_history'][k]=[x for x in xs if (parse_dt(x.get('at')) or t)>=cut][-400:]
    op=[public(x) for x in issues.values() if x.get('status')=='open'];res=[public(x) for x in issues.values() if x.get('status')=='resolved']
    op.sort(key=lambda x:(-x.get('occurrences',0),x.get('first_seen','')));res.sort(key=lambda x:x.get('resolved_at',''),reverse=True);res=res[:120]
    out={'schema_version':SCHEMA,'updated_at':iso_ts(t),'upstream_repo':REPO,'scope':{'branch':'main','close_after_consecutive_passes':3,'pr_attribution':'high-confidence test regressions only'},'analysis':{'complete':complete,'window_start':iso_ts(start),'window_end':iso_ts(t),'runs':nr,'jobs':nj,'api_requests':gh.requests,'request_budget':gh.budget,'coverage':cov,'errors':errors[-10:]},'stats':{'open':len(op),'attributed':sum(bool(x.get('introduced_by')) for x in op),'flaky':sum(x.get('category')=='flaky' for x in op),'infrastructure':sum(x.get('category')=='infrastructure' for x in op),'resolved':len(res)},'open':op,'resolved':res}
    state.update(schema_version=SCHEMA,updated_at=iso_ts(t),last_scan_at=iso_ts(t));save(OUT,out);save(STATE,state)
    print(f'main_runs={nr} jobs={nj} open={len(op)} resolved={len(res)} attributed={out["stats"]["attributed"]} requests={gh.requests}/{gh.budget} complete={complete}')
    for e in errors:print('warning:',e,file=sys.stderr)
    return 0
if __name__=='__main__':raise SystemExit(run())

#!/usr/bin/env python3
from __future__ import annotations

import json, math, os, re, sys, urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO=os.getenv('UPSTREAM_REPO','vllm-project/vllm-ascend')
ROOT='https://api.github.com'; DATA=Path('data')
HISTORY=DATA/'history.json'; TESTS=DATA/'tests.json'; STATE=DATA/'state.json'
RETENTION=int(os.getenv('RETENTION_DAYS','90')); OBS=int(os.getenv('OBSERVATION_DAYS','30'))
BOOT=int(os.getenv('BOOTSTRAP_HOURS','24')); LOOKBACK=int(os.getenv('NORMAL_LOOKBACK_HOURS','3'))
DETAIL_HOURS=int(os.getenv('DETAIL_LOOKBACK_HOURS','6')); TIMEOUT=int(os.getenv('REQUEST_TIMEOUT','20'))
ANON_BUDGET=int(os.getenv('ANON_REQUEST_BUDGET','54')); AUTH_BUDGET=int(os.getenv('AUTH_REQUEST_BUDGET','1200'))
EVENTS=('pull_request','pull_request_target','push','schedule','workflow_dispatch','repository_dispatch','workflow_call','workflow_run','merge_group')
GOOD={'success','neutral','skipped'}
PATTERNS=[
 ('DOWNLOAD',re.compile(r'install|download|dependenc|pip|apt|yum|wget|curl|checkout|pull image|docker pull|setup python|cache',re.I)),
 ('NETWORK',re.compile(r'network|dns|connection|timeout|timed out|resolve|proxy|socket|http\s*[45]\d\d',re.I)),
 ('RUNNER',re.compile(r'runner|self[- ]hosted|machine|pod|k8s|kubernetes|docker|container|environment|device|npu',re.I)),
 ('TEST',re.compile(r'test|pytest|unittest|accuracy|acceptance|performance|perf|benchmark|bench|eval',re.I)),
]
NON_CI=re.compile(
 r'(bot[_ -]|stale|label(er)?|merge[_ -]?conflict|issue[_ -]?(manage|triage)|handle /|command|'
 r'auto[_ -]?merge|assign(er)?|welcome|pr[_ -]?close|cancel[_ -]?(runs?|jobs?)|cancel (runs?|jobs?))',re.I)
PROB_POLICY=re.compile(
 r'(performance|\bperf\b|benchmark|accuracy|acceptance|pass.?rate|precision|evaluation|\beval\b|'
 r'性能|精度|采信)',re.I)
ARTIFACT_ONLY=re.compile(r'\bartifact(s)?\b',re.I)
POLICY_HELPER=re.compile(r'(^|[/ :(\-_])(generate|prepare|setup|matrix|merge|upload|download|collect)(\b|[/ :)\-_])',re.I)

def is_ci_text(text): return not bool(NON_CI.search(text or ''))
def is_ci_run(r): return is_ci_text((r.get('path') or '')+' '+(r.get('name') or ''))
def is_policy_prob(workflow,name):
    text=(workflow or '')+' '+(name or '')
    return (bool(PROB_POLICY.search(text)) and not bool(ARTIFACT_ONLY.search(name or ''))
            and not bool(POLICY_HELPER.search(name or '')))
def now(): return datetime.now(timezone.utc)
def dt(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace('Z','+00:00'))
    except ValueError: return None
def ih(x): return x.astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0).isoformat().replace('+00:00','Z')
def it(x): return x.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
def load(p,d):
    try: return json.loads(p.read_text()) if p.exists() else d
    except (OSError,json.JSONDecodeError): return d
def save(p,v):
    p.parent.mkdir(parents=True,exist_ok=True); q=p.with_suffix(p.suffix+'.tmp'); q.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n'); q.replace(p)
def bad(c): return bool(c) and c not in GOOD
def outcome(c): return 'success' if c in GOOD else ('failure' if c else None)

class GH:
    def __init__(self):
        self.token=os.getenv('UPSTREAM_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN') or ''
        self.rejected=False; self.requests=0; self.fallbacks=0
    @property
    def auth(self): return bool(self.token) and not self.rejected
    @property
    def budget(self): return AUTH_BUDGET if self.auth else ANON_BUDGET
    def ok(self,reserve=0): return self.requests < self.budget-reserve
    def request(self,url,use_auth=True):
        h={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'vllm-ascend-ci-monitor/3'}
        if use_auth and self.auth: h['Authorization']='Bearer '+self.token
        self.requests+=1
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=TIMEOUT) as r: return r.read()
        except urllib.error.HTTPError as e:
            if use_auth and self.auth and e.code in (401,403,404):
                self.rejected=True; self.fallbacks+=1; return self.request(url,False)
            raise
    def get(self,path,params=None):
        q='?'+urllib.parse.urlencode(params) if params else ''
        return json.loads(self.request(ROOT+path+q).decode())

def _run_span(g,event,start,end,depth=0):
    if not g.ok(8): return {},False,{'expected':None,'fetched':0,'complete':False,'slices':0}
    span=f'{it(start)}..{it(end)}'
    first=g.get(f'/repos/{REPO}/actions/runs',{'event':event,'created':span,'per_page':100,'page':1})
    expected=int(first.get('total_count') or 0); rows=first.get('workflow_runs',[])
    if expected>950 and (end-start)>timedelta(minutes=15) and depth<12:
        mid=start+(end-start)/2
        left,lc,lm=_run_span(g,event,start,mid,depth+1)
        right,rc,rm=_run_span(g,event,mid+timedelta(seconds=1),end,depth+1)
        left.update(right)
        return left,lc and rc,{
            'expected':(lm.get('expected') or 0)+(rm.get('expected') or 0),
            'fetched':len(left),'complete':lc and rc,
            'slices':(lm.get('slices') or 1)+(rm.get('slices') or 1)
        }
    out={int(r['id']):r for r in rows if r.get('id')}
    pages=max(1,math.ceil(expected/100)); pages=min(pages,10)
    for page in range(2,pages+1):
        if not g.ok(8): break
        p=g.get(f'/repos/{REPO}/actions/runs',{'event':event,'created':span,'per_page':100,'page':page})
        for r in p.get('workflow_runs',[]):
            if r.get('id'): out[int(r['id'])]=r
    complete=len(out)>=expected
    return out,complete,{'expected':expected,'fetched':len(out),'complete':complete,'slices':1}

def list_runs(g,start,end):
    out={}; coverage={}; complete=True
    for event in EVENTS:
        rows,ok,meta=_run_span(g,event,start,end)
        out.update(rows); coverage[event]=meta; complete &= ok
    return list(out.values()),complete,coverage

def list_jobs(g,rid):
    out=[]
    for page in range(1,20):
        if not g.ok(): break
        p=g.get(f'/repos/{REPO}/actions/runs/{rid}/jobs',{'filter':'latest','per_page':100,'page':page})
        rows=p.get('jobs',[]); out+=rows
        if not rows or len(out)>=int(p.get('total_count') or len(out)): break
    return out

def step_failures(j): return [s.get('name') or '' for s in j.get('steps',[]) if bad(s.get('conclusion'))]
def reason(j):
    c=(j.get('conclusion') or '').lower()
    if c=='timed_out': return 'TIMEOUT'
    if c=='cancelled': return 'CANCELLED'
    text=' | '.join([j.get('name') or '',*step_failures(j)])
    for name,p in PATTERNS:
        if p.search(text): return name
    return 'UNKNOWN'
def rkey(r): return 'workflow::'+(r.get('path') or r.get('name') or 'unknown')
def jkey(w,n): return f'job::{w}::{n}'

def migrate(tests,state):
    old=int(tests.get('schema_version') or 0)
    state.setdefault('seen_run_ids',{}); state.setdefault('seen_job_ids',{})
    if old < 2:
        new={}
        for x in tests.get('tests',{}).values():
            w=x.get('workflow') or 'Unnamed workflow'; n=x.get('name') or 'Unnamed job'; k=jkey(w,n)
            y=new.setdefault(k,{'kind':'job','workflow':w,'name':n,'probabilistic':False,'observations':[]})
            y['observations']+=x.get('observations',[])
        tests['tests']=new
    if old < 3:
        for x in tests.get('tests',{}).values():
            x['probabilistic']=False; x.pop('first_detected_at',None); x.pop('probability_reason',None)
    if old < 5:
        tests['tests']={k:x for k,x in tests.get('tests',{}).items() if is_ci_text(k+' '+(x.get('workflow') or '')+' '+(x.get('name') or ''))}
    if old < 8:
        for x in tests.get('tests',{}).values():
            if x.get('probability_reason')=='policy_probability_sensitive' and not is_policy_prob(x.get('workflow'),x.get('name')):
                x['probabilistic']=False; x.pop('first_detected_at',None); x.pop('probability_reason',None)
        tests['schema_version']=8; state['schema_version']=8
        return True
    return False

def ensure(tests,k,kind,w,n):
    return tests.setdefault('tests',{}).setdefault(k,{'kind':kind,'workflow':w,'name':n,'probabilistic':False,'observations':[]})
def observe_run(tests,state,r):
    rid=str(r.get('id')); seen=state['seen_run_ids']; o=outcome(r.get('conclusion'))
    if not rid or rid=='None' or rid in seen or not o: return
    w=r.get('name') or 'Unnamed workflow'; x=ensure(tests,rkey(r),'workflow',w,'(workflow aggregate)'); at=r.get('updated_at') or r.get('created_at') or it(now())
    x['observations'].append({'run_id':rid,'at':at,'outcome':o,'conclusion':r.get('conclusion'),'head_sha':r.get('head_sha')}); seen[rid]=at
def observe_job(tests,state,w,j,sha):
    jid=str(j.get('id')); seen=state['seen_job_ids']; o=outcome(j.get('conclusion'))
    if not jid or jid=='None' or jid in seen or not o: return
    n=j.get('name') or 'Unnamed job'; x=ensure(tests,jkey(w,n),'job',w,n); at=j.get('completed_at') or j.get('started_at') or it(now())
    x['observations'].append({'job_id':jid,'at':at,'outcome':o,'conclusion':j.get('conclusion'),'head_sha':sha}); seen[jid]=at

def recompute(x,n):
    cutoff=n-timedelta(days=OBS); obs=[o for o in x.get('observations',[]) if (dt(o.get('at')) or n)>=cutoff][-240:]; obs.sort(key=lambda o:o.get('at','')); x['observations']=obs
    seq=[o['outcome'] for o in obs if o.get('outcome') in ('success','failure')]; succ=seq.count('success'); fail=seq.count('failure')
    bysha=defaultdict(set)
    if x.get('kind')=='job':
        for o in obs:
            c=o.get('conclusion')
            if c=='success' and o.get('head_sha'): bysha[o['head_sha']].add('success')
            elif c in ('failure','timed_out') and o.get('head_sha'): bysha[o['head_sha']].add('failure')
    same_sha=x.get('kind')=='job' and any(len(v)>1 for v in bysha.values())
    policy=x.get('kind')=='job' and is_policy_prob(x.get('workflow'),x.get('name'))
    detected=same_sha or policy; old=bool(x.get('probabilistic'))
    if detected and not old:
        x['first_detected_at']=it(n); x['probability_reason']='policy_probability_sensitive' if policy else 'same_commit_mixed_outcomes'
    elif policy:
        x['probability_reason']='policy_probability_sensitive'
    x['probabilistic']=old or detected; x['samples_30d']=len(seq); x['successes_30d']=succ; x['failures_30d']=fail; x['pass_rate_30d']=round(succ/len(seq),4) if seq else None

def bucket(h): return {'hour':ih(h),'status':'unknown','coverage':'complete','runs':0,'jobs':0,'failed_runs':0,'failed_jobs':0,'probabilistic_workflows':0,'probabilistic_jobs':0,'active_runs':0,'reasons':{},'failures':[],'probabilistic':[]}
def add_prob(b,k,x):
    if any(z.get('key')==k for z in b['probabilistic']): return
    b['probabilistic'].append({'key':k,'kind':x.get('kind'),'workflow':x.get('workflow'),'job':x.get('name'),'pass_rate_30d':x.get('pass_rate_30d'),'reason':x.get('probability_reason')})
def prune(state,n):
    cutoff=n-timedelta(days=OBS+2)
    for f in ('seen_run_ids','seen_job_ids'):
        state[f]={k:v for k,v in state.get(f,{}).items() if (dt(v) or n)>=cutoff}

def main():
    n=now(); history=load(HISTORY,{'schema_version':8,'hours':[]}); tests=load(TESTS,{'schema_version':8,'tests':{}}); state=load(STATE,{'schema_version':8,'seen_run_ids':{},'seen_job_ids':{}})
    legacy=migrate(tests,state); bootstrap=legacy or int(history.get('schema_version') or 0)<8 or not history.get('hours'); hours=BOOT if bootstrap else LOOKBACK
    end=n.replace(minute=0,second=0,microsecond=0); start=end-timedelta(hours=hours); buckets={}; cur=start
    while cur<end: buckets[ih(cur)]=bucket(cur); cur+=timedelta(hours=1)
    g=GH(); errors=[]
    try: runs,complete,cov=list_runs(g,start-timedelta(hours=2),n)
    except Exception as e: runs=[]; complete=False; cov={}; errors.append(f'runs: {type(e).__name__}: {e}')
    runs.sort(key=lambda r:r.get('updated_at') or r.get('created_at') or '',reverse=True)
    runs=[r for r in runs if is_ci_run(r) and not (r.get('status')=='completed' and r.get('conclusion')=='skipped')]
    for r in runs: observe_run(tests,state,r)
    for x in tests.get('tests',{}).values(): recompute(x,n)
    byid={}
    for r in runs:
        rid=int(r.get('id') or 0)
        if not rid: continue
        byid[rid]=r; created=dt(r.get('created_at')); updated=dt(r.get('updated_at'))
        if r.get('status')=='completed':
            b=buckets.get(ih(updated or created)) if (updated or created) else None
            if not b: continue
            b['runs']+=1
            if bad(r.get('conclusion')):
                b['failed_runs']+=1; b['reasons']['WORKFLOW_FAILURE']=b['reasons'].get('WORKFLOW_FAILURE',0)+1
                if len(b['failures'])<20: b['failures'].append({'workflow':r.get('name') or 'Unnamed workflow','job':'(workflow aggregate)','conclusion':r.get('conclusion') or 'unknown','reason':'WORKFLOW_FAILURE','failed_steps':[],'url':r.get('html_url')})
        elif created:
            cur=max(created.replace(minute=0,second=0,microsecond=0),start)
            while cur<end:
                if ih(cur) in buckets: buckets[ih(cur)]['active_runs']+=1
                cur+=timedelta(hours=1)
    detail_cut=n-timedelta(hours=DETAIL_HOURS); candidates=[r for r in runs if r.get('status')=='completed' and (dt(r.get('updated_at')) or n)>=detail_cut]
    candidates.sort(key=lambda r:(0 if bad(r.get('conclusion')) else 1,-(dt(r.get('updated_at')) or n).timestamp())); max_detail=300 if g.auth else 24; cache={}
    for r in candidates[:max_detail]:
        if not g.ok(): break
        rid=int(r.get('id') or 0)
        try: jobs=list_jobs(g,rid); cache[rid]=jobs
        except Exception as e: errors.append(f'jobs:{rid}: {type(e).__name__}: {e}'); continue
        w=r.get('name') or 'Unnamed workflow'
        for j in jobs: observe_job(tests,state,w,j,r.get('head_sha'))
    for x in tests.get('tests',{}).values(): recompute(x,n)
    unstable_jobs={k for k,x in tests.get('tests',{}).items() if x.get('kind')=='job' and x.get('probabilistic')}
    for rid,jobs in cache.items():
        r=byid.get(rid,{}); w=r.get('name') or 'Unnamed workflow'; rb=buckets.get(ih(dt(r.get('updated_at')))) if dt(r.get('updated_at')) else None
        for j in jobs:
            event=dt(j.get('completed_at')) or dt(j.get('started_at')); b=buckets.get(ih(event)) if event else rb
            if not b: continue
            b['jobs']+=1; c=j.get('conclusion'); k=jkey(w,j.get('name') or 'Unnamed job')
            if bad(c):
                why=reason(j); b['failed_jobs']+=1; b['reasons'][why]=b['reasons'].get(why,0)+1
                if len(b['failures'])<20: b['failures'].append({'workflow':w,'job':j.get('name') or 'Unnamed job','conclusion':c or 'unknown','reason':why,'failed_steps':step_failures(j)[:4],'url':j.get('html_url') or r.get('html_url')})
            if k in unstable_jobs: b['probabilistic_jobs']+=1; add_prob(b,k,tests['tests'][k])
    for b in buckets.values():
        if not complete: b['coverage']='partial'
        if b['failed_runs'] or b['failed_jobs'] or b['probabilistic_jobs']: b['status']='down'
        elif b['coverage']=='partial' and (b['runs'] or b['active_runs']): b['status']='unknown'
        elif b['active_runs']: b['status']='degraded'
        elif b['runs']: b['status']='healthy'
    old={x.get('hour'):x for x in history.get('hours',[]) if x.get('hour')}
    for k,b in buckets.items():
        if b['runs'] or b['active_runs'] or k not in old or not errors: old[k]=b
    cutoff=n-timedelta(days=RETENTION); rows=[v for k,v in sorted(old.items()) if (dt(k) or n)>=cutoff]
    prune(state,n)
    history.update({'schema_version':8,'updated_at':it(n),'upstream_repo':REPO,'policy':{'any_failed_workflow_or_job_is_unavailable':True,'probability_sensitive_job_presence_is_unavailable':True,'degraded_counts_as_unavailable':True,'unknown_excluded_from_availability':True},'collector':{'authenticated':g.auth,'auth_fallbacks':g.fallbacks,'api_requests':g.requests,'request_budget':g.budget,'run_listing_complete':complete,'event_coverage':cov,'detail_runs':len(cache),'errors':errors[-10:]},'hours':rows})
    tests.update({'schema_version':8,'updated_at':it(n),'upstream_repo':REPO,'tests':dict(sorted(tests.get('tests',{}).items(),key=lambda kv:(not bool(kv[1].get('probabilistic')),0 if kv[1].get('kind')=='workflow' else 1,kv[0].lower())))})
    state.update({'schema_version':8,'updated_at':it(n)}); save(HISTORY,history); save(TESTS,tests); save(STATE,state)
    counts=Counter(x['status'] for x in buckets.values()); print(f'runs={len(runs)} detail_runs={len(cache)} requests={g.requests}/{g.budget} auth={g.auth} complete={complete} buckets={dict(counts)} unstable_jobs={len(unstable_jobs)}')
    for e in errors: print('warning:',e,file=sys.stderr)

if __name__=='__main__': main()

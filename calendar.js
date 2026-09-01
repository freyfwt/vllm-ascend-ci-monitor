(() => {
  const root = document.getElementById('calendarRoot');
  if (!root) return;

  const text = {
    zh: {
      tab:'可用性日历', title:'PR Merge Gate 可用性日历',
      sub:'按 UTC 小时追踪：只要有一个真实 CI/可靠性故障导致正常 PR 的 required ci-gate 无法通过，该小时就是红色不可用。',
      mon:['周一','周二','周三','周四','周五','周六','周日'], prev:'上个月', next:'下个月',
      availability:'Merge Gate 可用率', coverage:'判定覆盖率', healthy:'绿灯小时', degraded:'降级小时', down:'不可用小时', unknown:'未知小时',
      mergeBlock:'阻塞合入的 CI 故障', gateUnknown:'Gate 未归因', gatePolicy:'合入策略失败', gateRuns:'ci-gate Runs',
      infra:'非阻塞基础设施故障', code:'代码/测试失败（有效判决）', unresolved:'其他未归因', flaky:'Flaky signals',
      noDay:'该日期暂无历史数据。', hour:'小时', status:'状态', causes:'原因 / 证据', runs:'Runs', jobs:'Jobs',
      partial:'未知 / 证据不足', updated:'数据更新时间', tracked:'追踪天数', timezone:'时间基准：UTC',
      legendHealthy:'可用：CI 正常给出结论', legendDown:'不可用：CI 故障阻塞合入', legendDegraded:'降级：CI 故障但未阻塞合入', legendUnknown:'未知：证据不足',
      policy:'Policy', codeKind:'代码判决', ciKind:'CI 故障', unknownKind:'未归因'
    },
    en: {
      tab:'Availability Calendar', title:'PR Merge-Gate Availability Calendar',
      sub:'UTC-hour tracking: an hour is unavailable if even one real CI/reliability fault prevents a normal PR from satisfying required ci-gate.',
      mon:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'], prev:'Previous month', next:'Next month',
      availability:'Merge-gate availability', coverage:'Decision coverage', healthy:'Green hours', degraded:'Degraded hours', down:'Unavailable hours', unknown:'Unknown hours',
      mergeBlock:'Merge-blocking CI faults', gateUnknown:'Unattributed gate failures', gatePolicy:'Merge-policy failures', gateRuns:'ci-gate runs',
      infra:'Non-blocking infrastructure faults', code:'Code/test failures (valid verdicts)', unresolved:'Other unresolved', flaky:'Flaky signals',
      noDay:'No historical data for this date.', hour:'Hour', status:'Status', causes:'Cause / evidence', runs:'Runs', jobs:'Jobs',
      partial:'Unknown / insufficient evidence', updated:'Data updated', tracked:'Days tracked', timezone:'Time basis: UTC',
      legendHealthy:'Available: CI produced a valid verdict', legendDown:'Unavailable: CI fault blocked merge', legendDegraded:'Degraded: CI fault did not block merge', legendUnknown:'Unknown: insufficient evidence',
      policy:'Policy', codeKind:'Code verdict', ciKind:'CI fault', unknownKind:'Unattributed'
    }
  };

  let calendar=null, summary=null, month=null, selected=null;
  const langKey=()=>document.documentElement.lang.startsWith('zh')?'zh':'en';
  const tr=k=>text[langKey()][k]??k;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const percent=v=>v==null?'—':`${(Number(v)*100).toFixed(1)}%`;
  const statusName=s=>{
    const zh={healthy:'可用',down:'不可用',degraded:'降级',partial:'未知',unknown:'未知'};
    const en={healthy:'Available',down:'Unavailable',degraded:'Degraded',partial:'Unknown',unknown:'Unknown'};
    return (langKey()==='zh'?zh:en)[s]||s||(langKey()==='zh'?'未知':'Unknown');
  };

  const style=document.createElement('style');
  style.textContent=`
    #calendarRoot{display:grid;gap:14px}.cal-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow)}
    .cal-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.cal-head h2{margin:0}.cal-nav{display:flex;gap:7px}
    .cal-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.cal-stat{border:1px solid var(--line);border-radius:12px;padding:13px;background:var(--card)}.cal-stat strong{display:block;font-size:20px}.cal-stat span{color:var(--muted);font-size:12px}
    .cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px}.cal-week{font-size:11px;color:var(--muted);text-align:center;padding:5px}
    .cal-day{min-height:86px;border:1px solid var(--line);border-radius:10px;padding:8px;background:var(--card);color:var(--text);text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:4px}.cal-day:hover{border-color:var(--accent)}.cal-day.empty{visibility:hidden}.cal-day:disabled{cursor:default;opacity:.45}
    .cal-day.healthy{background:var(--okbg)}.cal-day.down{background:var(--badbg)}.cal-day.degraded{background:var(--warnbg)}.cal-day.partial,.cal-day.unknown{background:var(--unkbg)}.cal-day.selected{outline:2px solid var(--accent);outline-offset:1px}
    .cal-num{font-weight:750}.cal-rate{font-size:12px}.cal-cov{font-size:10px;color:var(--muted)}.cal-small{font-size:10px;color:var(--muted);margin-top:auto}
    .cal-timeline{display:grid;grid-template-columns:repeat(24,minmax(8px,1fr));gap:4px;margin:15px 0}.cal-hour{height:36px;border-radius:5px}.cal-hour.healthy{background:var(--ok)}.cal-hour.down{background:var(--bad)}.cal-hour.degraded{background:var(--warn)}.cal-hour.partial,.cal-hour.unknown{background:var(--unk)}
    .cal-hour-labels{display:grid;grid-template-columns:repeat(24,minmax(8px,1fr));gap:4px;color:var(--muted);font-size:9px;text-align:center}.cal-detail-table{width:100%;min-width:980px}.cal-reason{font-size:12px;color:var(--muted)}
    .cal-legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:12px}.cal-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
    @media(max-width:760px){.cal-summary{grid-template-columns:repeat(2,1fr)}.cal-day{min-height:72px;padding:6px}.cal-small{display:none}.cal-grid{gap:4px}}
  `;
  document.head.appendChild(style);

  root.innerHTML=`
    <div class="cal-card"><div class="cal-head"><div><h2 id="calTitle"></h2><div class="muted" id="calSub"></div></div><div class="cal-nav"><button class="btn" id="calPrev" type="button"></button><button class="btn" id="calNext" type="button"></button></div></div><div class="cal-summary" id="calOverall"></div></div>
    <div class="cal-card"><div class="cal-head"><h2 id="calMonthTitle"></h2><div class="muted" id="calTimezone"></div></div><div class="cal-grid" id="calWeek"></div><div class="cal-grid" id="calGrid"></div><div class="cal-legend" id="calLegend"></div></div>
    <div class="cal-card" id="calDetail"><div class="muted">${esc(tr('noDay'))}</div></div>`;

  const byDate=()=>new Map((calendar?.days||[]).map(d=>[d.date,d]));
  const monthKey=d=>`${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,'0')}`;
  const parseMonth=key=>new Date(`${key}-01T00:00:00Z`);
  const addMonth=(key,delta)=>{const d=parseMonth(key);d.setUTCMonth(d.getUTCMonth()+delta);return monthKey(d)};
  const minMonth=()=>(calendar?.tracking_start||'2026-08-01').slice(0,7);
  const maxMonth=()=>(calendar?.days?.at(-1)?.date||new Date().toISOString().slice(0,10)).slice(0,7);

  function renderTexts(){
    const tab=document.getElementById('calendarTab');if(tab)tab.textContent=tr('tab');
    document.getElementById('calTitle').textContent=tr('title');document.getElementById('calSub').textContent=tr('sub');document.getElementById('calPrev').textContent=tr('prev');document.getElementById('calNext').textContent=tr('next');document.getElementById('calTimezone').textContent=tr('timezone');
    document.getElementById('calWeek').innerHTML=tr('mon').map(x=>`<div class="cal-week">${esc(x)}</div>`).join('');
    document.getElementById('calLegend').innerHTML=`<span><i style="background:var(--ok)"></i>${esc(tr('legendHealthy'))}</span><span><i style="background:var(--bad)"></i>${esc(tr('legendDown'))}</span><span><i style="background:var(--warn)"></i>${esc(tr('legendDegraded'))}</span><span><i style="background:var(--unk)"></i>${esc(tr('legendUnknown'))}</span>`;
  }

  function renderOverall(){
    const o=summary?.overall||{};
    const cards=[[percent(o.availability),tr('availability')],[percent(o.coverage),tr('coverage')],[`${o.down_hours??'—'}h`,tr('down')],[`${o.degraded_hours??'—'}h`,tr('degraded')]];
    document.getElementById('calOverall').innerHTML=cards.map(([v,l])=>`<div class="cal-stat"><strong>${esc(v)}</strong><span>${esc(l)}</span></div>`).join('');
  }

  function renderMonth(){
    if(!month)month=maxMonth();const d=parseMonth(month);
    document.getElementById('calMonthTitle').textContent=new Intl.DateTimeFormat(langKey()==='zh'?'zh-CN':'en-GB',{year:'numeric',month:'long',timeZone:'UTC'}).format(d);
    document.getElementById('calPrev').disabled=month<=minMonth();document.getElementById('calNext').disabled=month>=maxMonth();
    const first=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),1)),daysInMonth=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth()+1,0)).getUTCDate(),offset=(first.getUTCDay()+6)%7,map=byDate();
    let html=Array.from({length:offset},()=>'<div class="cal-day empty"></div>').join('');
    for(let day=1;day<=daysInMonth;day++){
      const date=`${month}-${String(day).padStart(2,'0')}`,info=map.get(date),disabled=!info,cls=info?.status||'unknown';
      const label=info?`${statusName(cls)} · ${tr('availability')} ${percent(info.availability)} · ${tr('coverage')} ${percent(info.coverage)}`:tr('noDay');
      html+=`<button type="button" class="cal-day ${esc(cls)} ${selected===date?'selected':''}" data-date="${date}" ${disabled?'disabled':''} title="${esc(label)}"><span class="cal-num">${day}</span><span class="cal-rate">${info?percent(info.availability):'—'}</span><span class="cal-cov">cov ${info?percent(info.coverage):'—'}</span><span class="cal-small">${info?`✓${info.healthy_hours||0} · !${info.down_hours||0} · ~${info.degraded_hours||0} · ?${info.unknown_hours||0}`:''}</span></button>`;
    }
    document.getElementById('calGrid').innerHTML=html;document.querySelectorAll('.cal-day[data-date]:not(:disabled)').forEach(btn=>btn.addEventListener('click',()=>selectDay(btn.dataset.date)));
  }

  function significantRows(hours){return(hours||[]).filter(h=>h.status!=='healthy'||(h.merge_gate_evidence||[]).length||(h.merge_blocking_ci_failures||0)||(h.merge_gate_unknown_failures||0)||(h.infra_failures||0))}
  function kindLabel(k){return k==='ci'?tr('ciKind'):k==='code'?tr('codeKind'):k==='policy'?tr('policy'):tr('unknownKind')}

  async function selectDay(date,push=true){
    selected=date;month=date.slice(0,7);renderMonth();const box=document.getElementById('calDetail');box.innerHTML=`<div class="muted">${esc(date)}…</div>`;
    try{const r=await fetch(`data/days/${date}.json?t=${Date.now()}`);if(!r.ok)throw new Error(r.status);const day=await r.json();renderDay(day);if(push)history.replaceState(null,'',`#day=${date}`)}catch(e){box.innerHTML=`<div class="muted">${esc(tr('noDay'))}</div>`}
  }

  function renderDay(day){
    const m=day.metrics||{},hours=day.hours||[],box=document.getElementById('calDetail');
    const stats=[[percent(m.availability),tr('availability')],[percent(m.coverage),tr('coverage')],[`${m.healthy_hours||0}h`,tr('healthy')],[`${m.down_hours||0}h`,tr('down')],[`${m.degraded_hours||0}h`,tr('degraded')],[`${m.unknown_hours||0}h`,tr('unknown')],[m.merge_blocking_ci_failures||0,tr('mergeBlock')],[m.merge_gate_unknown_failures||0,tr('gateUnknown')]];
    const timeline=hours.map(h=>`<div class="cal-hour ${esc(h.status||'unknown')}" title="${esc(`${h.hour?.slice(11,13)||'??'}:00 · ${statusName(h.status)} · ${tr('mergeBlock')} ${h.merge_blocking_ci_failures||0} · ${tr('gateUnknown')} ${h.merge_gate_unknown_failures||0}`)}"></div>`).join('');
    const labels=hours.map((_,i)=>`<div>${String(i).padStart(2,'0')}</div>`).join('');
    const rows=significantRows(hours).map(h=>{
      const gate=(h.merge_gate_evidence||[]).slice(0,5).map(e=>`<a href="${esc(e.url||'#')}" target="_blank" rel="noreferrer">${esc(kindLabel(e.kind))}: ${esc(e.reason||'ci-gate')}</a>${e.evidence?` — ${esc(e.evidence)}`:''}`).join('<br>');
      const reasons=Object.entries(h.reasons||{}).map(([k,v])=>`${esc(k)} × ${v}`).join(' · ');
      const failures=(h.failures||[]).slice(0,2).map(f=>`<a href="${esc(f.url||'#')}" target="_blank" rel="noreferrer">${esc(f.job||f.workflow||'CI')}</a>`).join(' · ');
      const evidence=[gate,reasons,failures].filter(Boolean).join('<br>')||'—';
      return `<tr><td><code>${esc((h.hour||'').slice(11,16))}</code></td><td><span class="pill ${esc(h.status||'unknown')}">${esc(statusName(h.status))}</span></td><td>${h.merge_gate_runs||0}</td><td>${h.merge_blocking_ci_failures||0}</td><td>${h.merge_gate_policy_failures||0}</td><td>${h.merge_gate_code_failures||0}</td><td>${h.merge_gate_unknown_failures||0}</td><td>${h.infra_failures||0}</td><td class="cal-reason">${evidence}</td></tr>`;
    }).join('');
    box.innerHTML=`<div class="cal-head"><div><h2>${esc(day.date)} · ${esc(statusName(day.status))}</h2><div class="muted">${esc(tr('timezone'))} · ${esc(tr('updated'))}: ${esc(day.updated_at||'—')}</div></div></div><div class="cal-summary">${stats.map(([v,l])=>`<div class="cal-stat"><strong>${esc(v)}</strong><span>${esc(l)}</span></div>`).join('')}</div><div class="cal-timeline">${timeline}</div><div class="cal-hour-labels">${labels}</div><div class="table-wrap" style="margin-top:18px"><table class="cal-detail-table"><thead><tr><th>${esc(tr('hour'))}</th><th>${esc(tr('status'))}</th><th>${esc(tr('gateRuns'))}</th><th>${esc(tr('mergeBlock'))}</th><th>${esc(tr('gatePolicy'))}</th><th>${esc(tr('codeKind'))}</th><th>${esc(tr('gateUnknown'))}</th><th>${esc(tr('infra'))}</th><th>${esc(tr('causes'))}</th></tr></thead><tbody>${rows||`<tr><td colspan="9" class="muted">—</td></tr>`}</tbody></table></div>`;
  }

  async function init(){
    renderTexts();try{const stamp=Date.now();[calendar,summary]=await Promise.all([fetch(`data/calendar.json?t=${stamp}`).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}),fetch(`data/summary.json?t=${stamp}`).then(r=>r.ok?r.json():null)])}catch(e){root.innerHTML=`<div class="cal-card muted">${esc(tr('noDay'))}</div>`;return}
    const hash=location.hash.match(/^#day=(\d{4}-\d{2}-\d{2})$/);selected=hash?.[1]||calendar.days?.at(-1)?.date||null;month=(selected||calendar.tracking_start).slice(0,7);renderTexts();renderOverall();renderMonth();if(selected)selectDay(selected,false);
  }

  document.getElementById('calPrev').addEventListener('click',()=>{month=addMonth(month,-1);renderMonth()});document.getElementById('calNext').addEventListener('click',()=>{month=addMonth(month,1);renderMonth()});
  const langBtn=document.getElementById('lang');if(langBtn)langBtn.addEventListener('click',()=>setTimeout(()=>{renderTexts();renderOverall();renderMonth();if(selected)selectDay(selected,false)},0));
  window.addEventListener('hashchange',()=>{const m=location.hash.match(/^#day=(\d{4}-\d{2}-\d{2})$/);if(m)selectDay(m[1],false)});
  init();
})();

// The overview script is intentionally small and inline in index.html. Override
// only its availability semantics here so overview and calendar cannot drift.
(() => {
  if(typeof L==='undefined')return;
  Object.assign(L.zh,{
    subtitle:'公开 GitHub Actions 的 PR Merge Gate 可用性监控',availability:'Merge Gate 可用率',coverage:'判定覆盖率',healthyHours:'绿灯小时',badHours:'不可用小时',
    incidents:'近期 CI 可用性事件',noIncidents:'最近没有观测到会阻塞 PR 合入的 CI 故障。',
    prob:'Flaky signals',probState:'Flaky / 不稳定',normalState:'普通检查',basis:'可靠性证据',
    policyTitle:'CI 可用性判定标准',policyIntro:'Availability 只回答：正常 PR 能否获得可靠的 required ci-gate 结论。代码/测试真实失败或缺 ready-* label 都是有效判决，不算 CI 故障。',
    validVerdictTitle:'代码或合入策略失败 ≠ CI 不可用',validVerdictBody:'ruff、mypy、pytest、编译错误，以及 ci-gate 正确要求 ready-* label，都是 CI 正常工作时给出的有效判决。',
    unavailableTitle:'CI 自身卡住 required ci-gate = 不可用',unavailableBody:'只要一个正常 PR 因 Runner、网络、下载、容器、设备、控制面异常或 required leaf job 的真实不稳定而无法通过 ci-gate，该小时就是红色不可用。',
    policyTableSub:'红灯按“是否阻塞真实 PR 合入”判定，而不是按失败数量判定。',
    policyFootnote:'无法区分 PR 代码/策略问题与 CI 自身故障时标灰色 Unknown；页面同时显示判定覆盖率，Unknown 不会被一个孤立的 100% 可用率隐藏。',
    mergeBlock:'阻塞合入 CI 故障',gatePolicy:'合入策略判决',gateUnknown:'Gate 未归因'
  });
  Object.assign(L.en,{
    subtitle:'PR merge-gate availability monitoring of public GitHub Actions',availability:'Merge-gate availability',coverage:'Decision coverage',healthyHours:'Green hours',badHours:'Unavailable hours',
    incidents:'Recent CI availability events',noIncidents:'No CI fault that blocked PR merging was observed recently.',
    prob:'Flaky signals',probState:'Flaky / unstable',normalState:'Normal check',basis:'Reliability evidence',
    policyTitle:'CI availability policy',policyIntro:'Availability answers one question: can a normal PR obtain a reliable required ci-gate verdict? Real code/test failures and missing ready-* labels are valid verdicts, not CI outages.',
    validVerdictTitle:'Code or merge-policy failure ≠ CI outage',validVerdictBody:'ruff, mypy, pytest, compile failures and ci-gate correctly requiring ready-* labels are valid verdicts produced by working CI.',
    unavailableTitle:'CI itself blocks required ci-gate = unavailable',unavailableBody:'If even one normal PR cannot satisfy ci-gate because of runner, network, download, container, device, control-plane failure or real instability in a required leaf job, the hour is unavailable.',
    policyTableSub:'Red is based on actual merge blocking, not a failure-count threshold.',
    policyFootnote:'If evidence cannot distinguish PR code/policy from CI failure, the hour is gray Unknown. Decision coverage is shown alongside availability so unknown time cannot be hidden behind a misleading 100%.',
    mergeBlock:'Merge-blocking CI faults',gatePolicy:'Merge-policy verdicts',gateUnknown:'Unattributed gate failures'
  });

  POLICY.zh=[
    ['Required ci-gate 被 CI 自身故障卡住','Runner/网络/下载/容器/设备/CI 控制面导致正常 PR 无法完成 required check','bad','哪怕该小时只有一笔，只要真实阻塞合入，就算不可用'],
    ['Required leaf job 同 SHA 结果翻转','同一 PR 代码、同一 required leaf job 既 FAIL 又 PASS','bad','相同代码得到不一致 merge signal，属于 CI 可靠性故障'],
    ['非 merge-path 基础设施故障','Nightly/非 required job 的 Runner 或网络故障','unknown','不直接阻塞 PR 合入，页面显示 Degraded 黄灯而不是红灯'],
    ['PR 代码/测试真实失败','ruff、mypy、pytest、compile/build 明确发现代码问题','good','CI 正确拒绝 PR，说明 merge gate 正常工作'],
    ['缺少 ready-* label','ci-gate 提示 selected tests require ready-precise / ready-all / ready-a5','good','这是项目合入策略判决，不是 CI 故障'],
    ['Gate 失败但证据不足','无法判断是 PR/策略还是 CI 自身原因','unknown','保持灰色，不猜测'],
    ['无有效 CI 活动','该小时没有完成的有效 CI 证据','unknown','没有证据不能冒充绿灯']
  ];
  POLICY.en=[
    ['Required ci-gate blocked by CI itself','Runner/network/download/container/device/control-plane fault prevents a normal PR from completing the required check','bad','One real merge-blocking CI fault is enough to make the hour unavailable'],
    ['Required leaf job flips on the same SHA','Same PR code and required leaf job produce both FAIL and PASS','bad','Identical code produced an inconsistent merge signal'],
    ['Non-merge-path infrastructure fault','Runner/network fault in nightly or another non-required job','unknown','Does not directly block PR merge; shown as Degraded yellow rather than red'],
    ['Real PR code/test failure','ruff, mypy, pytest, compile/build clearly identifies a code problem','good','CI correctly rejected the PR, so the merge gate is working'],
    ['Missing ready-* label','ci-gate says selected tests require ready-precise / ready-all / ready-a5','good','This is a repository merge-policy verdict, not a CI fault'],
    ['Gate failure with insufficient evidence','Cannot distinguish PR/policy from CI fault','unknown','Keep it gray rather than guessing'],
    ['No valid CI activity','No completed valid CI evidence in the hour','unknown','No evidence must not become a green hour']
  ];

  renderToday=function(hours){
    const now=new Date(),today=now.toISOString().slice(0,10),expected=now.getUTCHours(),rows=hours.filter(x=>(x.hour||'').slice(0,10)===today),c={healthy:0,down:0,degraded:0,unknown:0};
    rows.forEach(x=>c[x.status]=(c[x.status]||0)+1);const observed=c.healthy+c.down+c.degraded,available=c.healthy+c.degraded;
    $('availability').textContent=observed?pct(available/observed):'—';$('healthyHours').textContent=`${c.healthy}h`;$('badHours').textContent=`${c.down}h`;$('coverage').textContent=expected?pct(observed/expected):'—';
  };
  renderCurrent=function(hours){
    const x=hours.at(-1);if(!x){$('status').textContent='UNKNOWN';$('meta').textContent=t('noData');return}
    $('dot').className=`dot ${x.status||'unknown'}`;$('status').textContent=statusLabel(x.status).toUpperCase();
    const a=[fmt(x.hour),`${t('runs')}: ${x.runs||0}`,`ci-gate: ${x.merge_gate_runs||0}`,`${L[lang].mergeBlock}: ${x.merge_blocking_ci_failures||0}`,`${L[lang].gatePolicy}: ${x.merge_gate_policy_failures||0}`,`${L[lang].gateUnknown}: ${x.merge_gate_unknown_failures||0}`];if(x.coverage==='partial')a.push(t('partial'));$('meta').innerHTML=a.map(v=>`<span>${esc(v)}</span>`).join('');
  };
  renderTimeline=function(hours){
    const map=new Map(hours.map(x=>[x.hour,x])),latest=hours.length?new Date(hours.at(-1).hour):new Date(Date.now()-3600e3),rows=[];for(let i=23;i>=0;i--){const d=new Date(latest.getTime()-i*3600e3),k=hourKey(d);rows.push(map.get(k)||{hour:k,status:'unknown'})}
    $('timeline').innerHTML=rows.map(x=>{const title=`${fmt(x.hour)} | ${statusLabel(x.status)} | ci-gate ${x.merge_gate_runs||0} | ${L[lang].mergeBlock}: ${x.merge_blocking_ci_failures||0} | ${L[lang].gatePolicy}: ${x.merge_gate_policy_failures||0} | ${L[lang].gateUnknown}: ${x.merge_gate_unknown_failures||0}`;return `<div><div class="hour ${x.status||'unknown'}" title="${esc(title)}"></div><div class="hlabel">${hfmt(x.hour)}</div></div>`}).join('');
  };
  renderIncidents=function(hours){
    const cutoff=Date.now()-7*864e5,a=hours.filter(x=>new Date(x.hour).getTime()>=cutoff&&['down','degraded'].includes(x.status)).sort((a,b)=>b.hour.localeCompare(a.hour)).slice(0,12);if(!a.length){$('incidents').innerHTML=`<div class="muted">${esc(t('noIncidents'))}</div>`;return}
    $('incidents').innerHTML=a.map(x=>{const ev=(x.merge_gate_evidence||[]).filter(e=>e.kind==='ci').slice(0,2),details=ev.map(e=>`${e.reason}${e.evidence?` — ${e.evidence}`:''}`).join(' · ')||Object.entries(x.reasons||{}).map(([k,v])=>`${k} × ${v}`).join(' · ')||t('none');return `<div class="incident"><div><strong>${esc(fmt(x.hour))}</strong></div><div><span class="pill ${esc(x.status)}">${esc(statusLabel(x.status))}</span></div><div>${esc(details)}</div></div>`}).join('');
  };
})();

(() => {
  const root = document.getElementById('calendarRoot');
  if (!root) return;

  const text = {
    zh: {
      tab: '可用性日历', title: 'CI 可用性日历', sub: '从 2026-08-01 开始，按 UTC 自然日长期追踪。点击日期查看完整 24 小时详情。',
      mon: ['周一','周二','周三','周四','周五','周六','周日'],
      prev:'上个月', next:'下个月', availability:'已观测可用率', healthy:'可用小时', down:'不可用小时',
      unknown:'未知小时', infra:'基础设施故障', code:'代码/测试失败（有效判决）', unresolved:'未能归因',
      prob:'概率敏感 Jobs', noDay:'该日期暂无历史数据。', details:'小时明细', hour:'小时', status:'状态',
      causes:'原因 / 证据', runs:'Runs', jobs:'Jobs', open:'打开日期', partial:'部分观测', updated:'数据更新时间',
      dayHealthy:'全天可用', dayDown:'存在不可用', dayPartial:'部分未知', dayUnknown:'暂无可靠数据',
      today:'今天', overall:'累计统计', tracked:'追踪天数', observed:'已观测小时', timezone:'时间基准：UTC'
    },
    en: {
      tab:'Availability Calendar', title:'CI Availability Calendar', sub:'Long-term UTC-day tracking from 2026-08-01. Select any date for the full 24-hour record.',
      mon:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],
      prev:'Previous month', next:'Next month', availability:'Observed availability', healthy:'Available hours', down:'Unavailable hours',
      unknown:'Unknown hours', infra:'Infrastructure failures', code:'Code/test failures (valid verdicts)', unresolved:'Unresolved',
      prob:'Probability-sensitive jobs', noDay:'No historical data for this date.', details:'Hourly details', hour:'Hour', status:'Status',
      causes:'Cause / evidence', runs:'Runs', jobs:'Jobs', open:'Open date', partial:'Partial coverage', updated:'Data updated',
      dayHealthy:'Fully available', dayDown:'Unavailable observed', dayPartial:'Partially unknown', dayUnknown:'No reliable data',
      today:'Today', overall:'Cumulative', tracked:'Days tracked', observed:'Observed hours', timezone:'Time basis: UTC'
    }
  };

  let calendar = null, summary = null, month = null, selected = null;
  const langKey = () => document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
  const tr = k => text[langKey()][k] ?? k;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const percent = v => v == null ? '—' : `${(Number(v) * 100).toFixed(1)}%`;
  const statusName = s => {
    const l = langKey();
    const map = l === 'zh'
      ? {healthy:'可用',down:'不可用',partial:'部分未知',unknown:'未知',degraded:'降级'}
      : {healthy:'Available',down:'Unavailable',partial:'Partial',unknown:'Unknown',degraded:'Degraded'};
    return map[s] || s || map.unknown;
  };

  const style = document.createElement('style');
  style.textContent = `
    #calendarRoot{display:grid;gap:14px}
    .cal-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .cal-stat{border:1px solid var(--line);border-radius:12px;padding:13px;background:var(--card)}
    .cal-stat strong{display:block;font-size:20px}.cal-stat span{color:var(--muted);font-size:12px}
    .cal-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow)}
    .cal-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
    .cal-head h2{margin:0}.cal-nav{display:flex;gap:7px}
    .cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px}
    .cal-week{font-size:11px;color:var(--muted);text-align:center;padding:5px}
    .cal-day{min-height:82px;border:1px solid var(--line);border-radius:10px;padding:8px;background:var(--card);color:var(--text);text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:5px}
    .cal-day:hover{border-color:var(--accent)}.cal-day.empty{visibility:hidden}.cal-day:disabled{cursor:default;opacity:.45}
    .cal-day.healthy{background:var(--okbg)}.cal-day.down{background:var(--badbg)}.cal-day.partial{background:var(--warnbg)}.cal-day.unknown{background:var(--unkbg)}
    .cal-num{font-weight:750}.cal-rate{font-size:12px}.cal-small{font-size:10px;color:var(--muted);margin-top:auto}
    .cal-day.selected{outline:2px solid var(--accent);outline-offset:1px}
    .cal-detail-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}
    .cal-detail-head h2{margin:0}.cal-timeline{display:grid;grid-template-columns:repeat(24,minmax(8px,1fr));gap:4px;margin:15px 0}
    .cal-hour{height:36px;border-radius:5px}.cal-hour.healthy{background:var(--ok)}.cal-hour.down{background:var(--bad)}.cal-hour.unknown{background:var(--unk)}.cal-hour.degraded,.cal-hour.partial{background:var(--warn)}
    .cal-hour-labels{display:grid;grid-template-columns:repeat(24,minmax(8px,1fr));gap:4px;color:var(--muted);font-size:9px;text-align:center}
    .cal-detail-table{width:100%;min-width:760px}.cal-reason{font-size:12px;color:var(--muted)}
    .cal-legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:12px}.cal-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
    @media(max-width:760px){.cal-summary{grid-template-columns:repeat(2,1fr)}.cal-day{min-height:66px;padding:6px}.cal-rate{font-size:11px}.cal-small{display:none}.cal-grid{gap:4px}.cal-timeline,.cal-hour-labels{overflow:hidden}}
  `;
  document.head.appendChild(style);

  root.innerHTML = `
    <div class="cal-card">
      <div class="cal-head">
        <div><h2 id="calTitle"></h2><div class="muted" id="calSub"></div></div>
        <div class="cal-nav"><button class="btn" id="calPrev" type="button"></button><button class="btn" id="calNext" type="button"></button></div>
      </div>
      <div class="cal-summary" id="calOverall"></div>
    </div>
    <div class="cal-card">
      <div class="cal-head"><h2 id="calMonthTitle"></h2><div class="muted" id="calTimezone"></div></div>
      <div class="cal-grid" id="calWeek"></div>
      <div class="cal-grid" id="calGrid"></div>
      <div class="cal-legend" id="calLegend"></div>
    </div>
    <div class="cal-card" id="calDetail"><div class="muted">${esc(tr('noDay'))}</div></div>
  `;

  const byDate = () => new Map((calendar?.days || []).map(d => [d.date, d]));
  const monthKey = d => `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,'0')}`;
  const parseMonth = key => new Date(`${key}-01T00:00:00Z`);
  const addMonth = (key, delta) => { const d = parseMonth(key); d.setUTCMonth(d.getUTCMonth()+delta); return monthKey(d); };
  const minMonth = () => (calendar?.tracking_start || '2026-08-01').slice(0,7);
  const maxMonth = () => (calendar?.days?.at(-1)?.date || new Date().toISOString().slice(0,10)).slice(0,7);

  function renderTexts() {
    const tab = document.getElementById('calendarTab'); if (tab) tab.textContent = tr('tab');
    document.getElementById('calTitle').textContent = tr('title');
    document.getElementById('calSub').textContent = tr('sub');
    document.getElementById('calPrev').textContent = tr('prev');
    document.getElementById('calNext').textContent = tr('next');
    document.getElementById('calTimezone').textContent = tr('timezone');
    document.getElementById('calWeek').innerHTML = tr('mon').map(x => `<div class="cal-week">${esc(x)}</div>`).join('');
    document.getElementById('calLegend').innerHTML = `<span><i style="background:var(--ok)"></i>${esc(statusName('healthy'))}</span><span><i style="background:var(--bad)"></i>${esc(statusName('down'))}</span><span><i style="background:var(--warn)"></i>${esc(tr('partial'))}</span><span><i style="background:var(--unk)"></i>${esc(statusName('unknown'))}</span>`;
  }

  function renderOverall() {
    const o = summary?.overall || {};
    document.getElementById('calOverall').innerHTML = [[percent(o.availability), tr('availability')],[summary?.days_tracked ?? (calendar?.days?.length || 0), tr('tracked')],[o.observed_hours ?? '—', tr('observed')],[o.down_hours ?? '—', tr('down')]].map(([v,l]) => `<div class="cal-stat"><strong>${esc(v)}</strong><span>${esc(l)}</span></div>`).join('');
  }

  function renderMonth() {
    if (!month) month = maxMonth();
    const d = parseMonth(month);
    document.getElementById('calMonthTitle').textContent = new Intl.DateTimeFormat(langKey()==='zh'?'zh-CN':'en-GB',{year:'numeric',month:'long',timeZone:'UTC'}).format(d);
    document.getElementById('calPrev').disabled = month <= minMonth();
    document.getElementById('calNext').disabled = month >= maxMonth();
    const first = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
    const daysInMonth = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth()+1, 0)).getUTCDate();
    const offset = (first.getUTCDay()+6)%7;
    const map = byDate();
    let html = Array.from({length:offset},()=>'<div class="cal-day empty"></div>').join('');
    for (let day=1; day<=daysInMonth; day++) {
      const date = `${month}-${String(day).padStart(2,'0')}`;
      const info = map.get(date), disabled = !info, cls = info?.status || 'unknown';
      const label = info ? `${statusName(cls)} · ${tr('availability')} ${percent(info.availability)}` : tr('noDay');
      html += `<button type="button" class="cal-day ${esc(cls)} ${selected===date?'selected':''}" data-date="${date}" ${disabled?'disabled':''} title="${esc(label)}"><span class="cal-num">${day}</span><span class="cal-rate">${info ? percent(info.availability) : '—'}</span><span class="cal-small">${info ? `${info.healthy_hours||0}h / ${info.down_hours||0}h / ?${info.unknown_hours||0}h` : ''}</span></button>`;
    }
    document.getElementById('calGrid').innerHTML = html;
    document.querySelectorAll('.cal-day[data-date]:not(:disabled)').forEach(btn => btn.addEventListener('click',()=>selectDay(btn.dataset.date)));
  }

  function significantRows(hours) { return (hours || []).filter(h => h.status !== 'healthy' || (h.code_failures||0) || (h.infra_failures||0) || (h.probabilistic_jobs||0)); }

  async function selectDay(date, push=true) {
    selected = date; month = date.slice(0,7); renderMonth();
    const box = document.getElementById('calDetail'); box.innerHTML = `<div class="muted">${esc(date)}…</div>`;
    try {
      const r = await fetch(`data/days/${date}.json?t=${Date.now()}`); if (!r.ok) throw new Error(r.status);
      const day = await r.json(); renderDay(day); if (push) history.replaceState(null,'',`#day=${date}`);
    } catch (e) { box.innerHTML = `<div class="muted">${esc(tr('noDay'))}</div>`; }
  }

  function renderDay(day) {
    const m = day.metrics || {}, hours = day.hours || [], box = document.getElementById('calDetail');
    const stats = [[percent(m.availability),tr('availability')],[`${m.healthy_hours||0}h`,tr('healthy')],[`${m.down_hours||0}h`,tr('down')],[`${m.unknown_hours||0}h`,tr('unknown')],[m.infra_failures||0,tr('infra')],[m.code_failures||0,tr('code')],[m.unresolved_failures||0,tr('unresolved')],[m.probabilistic_jobs||0,tr('prob')]];
    const timeline = hours.map(h=>`<div class="cal-hour ${esc(h.status||'unknown')}" title="${esc(`${h.hour?.slice(11,13)||'??'}:00 · ${statusName(h.status)} · ${tr('infra')} ${h.infra_failures||0} · ${tr('code')} ${h.code_failures||0}`)}"></div>`).join('');
    const labels = hours.map((_,i)=>`<div>${String(i).padStart(2,'0')}</div>`).join('');
    const rows = significantRows(hours).map(h => {
      const reasons = Object.entries(h.reasons||{}).map(([k,v])=>`${k} × ${v}`).join(' · ');
      const failures = (h.failures||[]).slice(0,3).map(f=>`<a href="${esc(f.url||'#')}" target="_blank" rel="noreferrer">${esc(f.job||f.workflow||'CI')}</a>`).join(' · ');
      const prob = (h.probabilistic||[]).slice(0,3).map(p=>esc(p.job||p.workflow||'')).join(' · ');
      const evidence = [reasons,failures,prob].filter(Boolean).join(' · ') || '—';
      return `<tr><td><code>${esc((h.hour||'').slice(11,16))}</code></td><td><span class="pill ${esc(h.status||'unknown')}">${esc(statusName(h.status))}</span></td><td>${h.runs||0}</td><td>${h.jobs||0}</td><td>${h.infra_failures||0}</td><td>${h.code_failures||0}</td><td>${h.unresolved_failures||0}</td><td>${h.probabilistic_jobs||0}</td><td class="cal-reason">${evidence}</td></tr>`;
    }).join('');
    box.innerHTML = `<div class="cal-detail-head"><div><h2>${esc(day.date)} · ${esc(statusName(day.status))}</h2><div class="muted">${esc(tr('timezone'))} · ${esc(tr('updated'))}: ${esc(day.updated_at||'—')}</div></div></div><div class="cal-summary" style="margin-top:14px">${stats.map(([v,l])=>`<div class="cal-stat"><strong>${esc(v)}</strong><span>${esc(l)}</span></div>`).join('')}</div><div class="cal-timeline">${timeline}</div><div class="cal-hour-labels">${labels}</div><div class="table-wrap" style="margin-top:18px"><table class="cal-detail-table"><thead><tr><th>${esc(tr('hour'))}</th><th>${esc(tr('status'))}</th><th>${esc(tr('runs'))}</th><th>${esc(tr('jobs'))}</th><th>${esc(tr('infra'))}</th><th>${esc(tr('code'))}</th><th>${esc(tr('unresolved'))}</th><th>${esc(tr('prob'))}</th><th>${esc(tr('causes'))}</th></tr></thead><tbody>${rows || `<tr><td colspan="9" class="muted">—</td></tr>`}</tbody></table></div>`;
  }

  async function init() {
    renderTexts();
    try {
      const stamp=Date.now();
      [calendar,summary]=await Promise.all([fetch(`data/calendar.json?t=${stamp}`).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}),fetch(`data/summary.json?t=${stamp}`).then(r=>r.ok?r.json():null)]);
    } catch (e) { root.innerHTML = `<div class="cal-card muted">${esc(tr('noDay'))}</div>`; return; }
    const hash = location.hash.match(/^#day=(\d{4}-\d{2}-\d{2})$/);
    selected = hash?.[1] || calendar.days?.at(-1)?.date || null;
    month = (selected || calendar.tracking_start).slice(0,7);
    renderTexts(); renderOverall(); renderMonth(); if (selected) selectDay(selected,false);
  }

  document.getElementById('calPrev').addEventListener('click',()=>{month=addMonth(month,-1);renderMonth()});
  document.getElementById('calNext').addEventListener('click',()=>{month=addMonth(month,1);renderMonth()});
  const langBtn=document.getElementById('lang'); if(langBtn) langBtn.addEventListener('click',()=>setTimeout(()=>{renderTexts();renderOverall();renderMonth();if(selected)selectDay(selected,false)},0));
  window.addEventListener('hashchange',()=>{const m=location.hash.match(/^#day=(\d{4}-\d{2}-\d{2})$/);if(m)selectDay(m[1],false)});
  init();
})();

from pathlib import Path

p = Path('index.html')
s = p.read_text()

old_nav = '<nav class="tabs" aria-label="Dashboard tabs"><button class="tab active" data-tab="overview" data-i18n="tabOverview"></button><button class="tab" data-tab="policy" data-i18n="tabPolicy"></button></nav>'
new_nav = '<nav class="tabs" aria-label="Dashboard tabs"><button class="tab active" data-tab="overview" data-i18n="tabOverview"></button><button class="tab" data-tab="calendar" id="calendarTab">可用性日历</button><button class="tab" data-tab="policy" data-i18n="tabPolicy"></button></nav>'
if old_nav in s:
    s = s.replace(old_nav, new_nav, 1)

old_panel = '</section>\n<footer><span id="updated"></span>'
new_panel = '</section>\n<section id="calendarPanel" class="panel"><div id="calendarRoot"></div></section>\n<footer><span id="updated"></span>'
if 'id="calendarPanel"' not in s and old_panel in s:
    s = s.replace(old_panel, new_panel, 1)

old_active = "let lang=localStorage.getItem('lang')||(navigator.language.startsWith('zh')?'zh':'en'),H=null,T=null,activeTab=location.hash==='#policy'?'policy':'overview';"
new_active = "let lang=localStorage.getItem('lang')||(navigator.language.startsWith('zh')?'zh':'en'),H=null,T=null,activeTab=location.hash==='#policy'?'policy':(location.hash==='#calendar'||location.hash.startsWith('#day='))?'calendar':'overview';"
if old_active in s:
    s = s.replace(old_active, new_active, 1)

old_set = "function setTab(name){activeTab=name;document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$('overviewPanel').classList.toggle('active',name==='overview');$('policyPanel').classList.toggle('active',name==='policy');history.replaceState(null,'',name==='policy'?'#policy':'#overview')}"
new_set = "function setTab(name){activeTab=name;document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$('overviewPanel').classList.toggle('active',name==='overview');$('policyPanel').classList.toggle('active',name==='policy');$('calendarPanel').classList.toggle('active',name==='calendar');const day=location.hash.startsWith('#day=');const hash=name==='policy'?'#policy':name==='calendar'?(day?location.hash:'#calendar'):'#overview';history.replaceState(null,'',hash)}"
if old_set in s:
    s = s.replace(old_set, new_set, 1)

old_hash = "window.addEventListener('hashchange',()=>setTab(location.hash==='#policy'?'policy':'overview'));"
new_hash = "window.addEventListener('hashchange',()=>{const h=location.hash;setTab(h==='#policy'?'policy':(h==='#calendar'||h.startsWith('#day='))?'calendar':'overview')});"
if old_hash in s:
    s = s.replace(old_hash, new_hash, 1)

if '<script src="calendar.js"></script>' not in s:
    s = s.replace('</body>', '<script src="calendar.js"></script>\n</body>', 1)

p.write_text(s)

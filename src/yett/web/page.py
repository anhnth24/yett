"""Trang console self-contained (không asset ngoài): sidebar 6 màn + chat + duyệt approval.

Mọi màn lấy dữ liệu THẬT qua /api/* (meta, usage, traces, subagents, projects, secrets,
config). Màn không có nguồn thật (job theo lịch) hiện trạng thái rỗng trung thực."""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>yett</title>
<style>
/* yett design tokens — light professional, self-contained (Inter nếu có, ngược lại system-ui). */
:root { --bg:#F6F6F3; --side:#FBFBF9; --card:#FFFFFF; --wash:#F1F1EB; --track:#EFEFE8;
        --line:#E3E3DD; --hair:#F1F1EB;
        --ink:#171712; --body:#26261F; --sub:#5c5c53; --mut:#8a8a7f; --faint:#a5a599;
        --acc:#1B6B52; --acc-wash:#F0F5F2; --warn:#C7930A; --warn-line:#E8D9A0; --danger:#B4442F; --idle:#D5D5CB;
        --font-ui:'Inter',system-ui,'Segoe UI',Roboto,sans-serif;
        --font-mono:ui-monospace,'Cascadia Code',Consolas,monospace;
        --r-control:8px; --r-card:12px; --shadow-card:0 1px 2px rgba(20,20,10,.04); }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.55 var(--font-ui); background:var(--bg); color:var(--body); height:100vh; overflow:hidden; }
@keyframes yettPulse { 0%,100%{opacity:1} 50%{opacity:.35} }
.app { display:flex; height:100vh; overflow:hidden; }

/* sidebar */
.side { width:224px; flex-shrink:0; display:flex; flex-direction:column; padding:16px 12px;
  border-right:1px solid var(--line); background:var(--side); }
.brand { display:flex; align-items:center; gap:9px; padding:2px 10px 16px; }
.brand .bt { font-size:15px; font-weight:700; letter-spacing:-.01em; color:var(--ink); line-height:1.15; }
.brand .bs { font-size:10px; color:var(--faint); letter-spacing:.07em; font-weight:500; }
nav { display:flex; flex-direction:column; gap:2px; }
.nav-item { display:flex; align-items:center; gap:10px; padding:8px 12px; border-radius:8px; cursor:pointer;
  font-size:13.5px; color:var(--sub); font-weight:500; }
.nav-item:hover { background:var(--wash); }
.nav-item.on { background:var(--wash); color:var(--ink); font-weight:600; }
.nav-item .nsub { margin-left:auto; font-size:10.5px; color:var(--faint); }
.side-foot { margin-top:auto; padding:12px 14px; border-radius:10px; background:var(--wash);
  font-size:12px; color:var(--mut); line-height:1.9; }
.side-foot .r { display:flex; align-items:center; gap:7px; }
.side-foot .dot { width:6px; height:6px; border-radius:50%; background:var(--acc); }

/* main + topbar */
.main { flex:1; display:flex; flex-direction:column; min-width:0; }
.top { height:58px; flex-shrink:0; display:flex; align-items:center; gap:10px; padding:0 24px;
  border-bottom:1px solid var(--line); background:var(--side); }
.top .ttl { font-size:16px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.top .sub { font-size:12px; color:var(--faint); }
.tpills { margin-left:auto; display:flex; gap:8px; }
.pill { display:flex; align-items:center; gap:8px; padding:5px 13px; border-radius:999px; background:var(--card);
  border:1px solid var(--line); font-size:12.5px; color:var(--sub); }
.livedot { width:6px; height:6px; border-radius:50%; background:var(--acc); animation:yettPulse 2.4s ease-in-out infinite; }
.approvals { display:flex; flex-direction:column; gap:10px; padding:0 24px; }
.approvals:not(:empty) { padding:14px 24px 0; }

/* generic screen view */
#view { flex:1; overflow-y:auto; padding:20px 24px 40px; display:none; }
.tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:14px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-card); padding:14px 16px; }
.cap { font-size:10.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--faint); font-weight:600; }
.stat { font-size:22px; font-weight:700; color:var(--ink); margin:6px 0 2px; letter-spacing:-.01em; word-break:break-word; }
.tmeta { font-size:11.5px; color:var(--mut); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-card); padding:16px 18px; margin-bottom:12px; }
.ct { font-size:13.5px; font-weight:700; color:var(--ink); margin-bottom:10px; }
.hrow { display:flex; align-items:center; gap:9px; padding:9px 0; border-bottom:1px solid var(--hair);
  font-size:12.5px; color:var(--body); }
.hrow:last-child { border-bottom:0; }
.grow { flex:1; } .k { color:var(--sub); } .v { color:var(--ink); font-weight:600; }
.mono { font-family:var(--font-mono); font-size:11.5px; color:var(--faint); }
.empty { padding:14px 0; color:var(--faint); font-size:12.5px; }
.note { background:var(--acc-wash); border:1px solid var(--line); border-radius:var(--r-card);
  padding:12px 15px; font-size:12.5px; color:var(--sub); margin-bottom:14px; line-height:1.5; }
.chips { display:flex; gap:5px; flex-wrap:wrap; margin-top:5px; }
.chip { font-family:var(--font-mono); font-size:10.5px; padding:2px 8px; border-radius:5px;
  background:var(--wash); color:var(--sub); }
.subrow { display:flex; gap:11px; padding:11px 0; border-bottom:1px solid var(--hair); }
.subrow:last-child { border-bottom:0; }
.subav { width:34px; height:34px; border-radius:50%; flex-shrink:0; }
.badge { display:inline-flex; align-items:center; gap:6px; font-size:11.5px; font-weight:500; color:var(--mut); }
.badge i { width:6px; height:6px; border-radius:50%; background:var(--idle); display:inline-block; }
.badge.b-ok i, .badge.b-running i { background:var(--acc); }
.badge.b-danger i { background:var(--danger); } .badge.b-warn i { background:var(--warn); }
.tbl { width:100%; border-collapse:collapse; font-size:12.5px; }
.tbl th { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); font-size:10.5px;
  letter-spacing:.07em; text-transform:uppercase; color:var(--faint); font-weight:600; }
.tbl td { text-align:left; padding:11px 8px; border-bottom:1px solid var(--hair); color:var(--body); vertical-align:middle; }

/* chat */
#chatwrap { flex:1; display:flex; flex-direction:column; min-height:0; }
#log { flex:1; overflow-y:auto; padding:20px 24px; display:flex; flex-direction:column; gap:10px; }
.msg { max-width:720px; padding:11px 15px; border-radius:14px; white-space:pre-wrap; word-wrap:break-word;
  font-size:13.5px; line-height:1.6; }
.user { align-self:flex-end; background:var(--ink); color:#fff; }
.agent { align-self:flex-start; background:var(--card); border:1px solid var(--line); color:var(--body); }
.meta { align-self:flex-start; color:var(--faint); font-size:11.5px; font-family:var(--font-mono); }
.err { align-self:flex-start; background:#FBEEEA; border:1px solid #EAD0C8; color:var(--danger); }
form { display:flex; gap:10px; padding:14px 24px 18px; background:var(--side); border-top:1px solid var(--line); }
textarea.in { flex:1; resize:none; background:var(--card); color:var(--body); border:1px solid var(--line);
  border-radius:var(--r-control); padding:10px 12px; font:inherit; font-size:13px; height:44px; }
textarea.in:focus { outline:none; border-color:var(--acc); }
button.send { background:var(--acc); color:#fff; border:0; border-radius:var(--r-control); padding:0 20px;
  font:inherit; font-size:12.5px; font-weight:600; cursor:pointer; }
button.send:hover { filter:brightness(1.08); } button.send:disabled { opacity:.5; cursor:default; }

/* approval banner */
.approve { background:var(--card); border:1px solid var(--warn-line); box-shadow:var(--shadow-card);
  padding:12px 16px 14px; border-radius:var(--r-card); }
.approve b { color:var(--ink); font-weight:600; font-size:13px; }
.approve code { display:block; white-space:pre-wrap; margin:6px 0 10px; color:var(--mut);
  font-family:var(--font-mono); font-size:12px; }
.approve button { margin-right:8px; border:0; border-radius:var(--r-control); padding:7px 16px; cursor:pointer;
  font:inherit; font-size:12.5px; font-weight:600; }
.approve .mut { color:var(--faint); font-size:11.5px; margin-top:6px; }
.ok { background:var(--acc); color:#fff; } .no { background:var(--card); color:var(--sub); border:1px solid var(--line); }

/* modal chi tiết trace */
.modal { display:none; position:fixed; inset:0; background:rgba(20,20,10,.32); z-index:50;
  align-items:flex-start; justify-content:center; padding:40px 20px; }
.modal.on { display:flex; }
.sheet { background:var(--card); border:1px solid var(--line); border-radius:14px;
  box-shadow:0 20px 60px rgba(20,20,10,.2); width:760px; max-width:100%; max-height:80vh;
  display:flex; flex-direction:column; overflow:hidden; }
.sheet-h { display:flex; align-items:center; padding:14px 18px; border-bottom:1px solid var(--line);
  font-weight:700; color:var(--ink); font-size:14px; }
.sheet-h button { margin-left:auto; background:transparent; border:0; font-size:16px; cursor:pointer; color:var(--mut); }
#mbody { padding:14px 18px; overflow-y:auto; }
.span { padding:10px 0; border-bottom:1px solid var(--hair); }
.span:last-child { border-bottom:0; }
.span-h { display:flex; align-items:center; gap:8px; font-size:12.5px; }
.span-k { font-size:10px; letter-spacing:.06em; text-transform:uppercase; color:var(--faint);
  background:var(--wash); padding:2px 7px; border-radius:5px; font-weight:600; }
.span-attr { margin:5px 0 0 4px; font-family:var(--font-mono); font-size:11px; color:var(--mut);
  white-space:pre-wrap; word-break:break-word; }
.runrow { cursor:pointer; } .runrow:hover { background:var(--wash); }
.jin { background:var(--card); color:var(--body); border:1px solid var(--line); border-radius:var(--r-control);
  padding:8px 10px; font:inherit; font-size:12.5px; min-width:0; }
.jin:focus { outline:none; border-color:var(--acc); }
.jbtn { background:var(--card); color:var(--sub); border:1px solid var(--line); border-radius:6px;
  padding:4px 10px; font:inherit; font-size:11.5px; cursor:pointer; }
.jbtn:hover { background:var(--wash); }

@media (max-width:940px) { .tiles { grid-template-columns:repeat(2,1fr); } .grid2 { grid-template-columns:1fr; } }
</style></head><body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <svg width="26" height="26" viewBox="0 0 24 24"><rect x="1.5" y="1.5" width="21" height="21" rx="5.5" fill="#171712"/><path d="M6.5 19V11.5a5.5 5.5 0 0 1 11 0V19" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/><path d="M12 9v8" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>
      <div><div class="bt">yett</div><div class="bs">AGENT HARNESS · ON-PREM</div></div>
    </div>
    <nav id="nav"></nav>
    <div class="side-foot">
      <div class="r"><span class="dot"></span><span id="fsandbox">sandbox</span></div>
      <div class="r"><span class="dot"></span>Policy Gate: fail-closed</div>
    </div>
  </aside>
  <main class="main">
    <div class="top">
      <div class="ttl" id="ttl">Tổng quan</div>
      <div class="sub" id="tsub">Overview</div>
      <div class="tpills">
        <span class="pill"><span class="livedot"></span><span id="prov">…</span></span>
        <span class="pill" id="cost">Chi phí: …</span>
      </div>
    </div>
    <div class="approvals" id="approvals"></div>
    <div id="chatwrap">
      <div id="pbar"></div>
      <div id="log"></div>
      <form id="f"><textarea class="in" id="in" placeholder="Nhập tin nhắn... (Enter gửi, Shift+Enter xuống dòng)"></textarea>
      <button type="button" class="send" id="stop" style="display:none;background:var(--danger)">Dừng</button>
      <button class="send" id="send">Gửi</button></form>
    </div>
    <div id="view"></div>
  </main>
</div>
<div id="modal" class="modal"><div class="sheet"><div class="sheet-h"><span id="mtitle">Trace</span><button id="mclose">✕</button></div><div id="mbody"></div></div></div>
<script>
const NAV = [
  { id:'overview', label:'Tổng quan', sub:'Overview' },
  { id:'agents',   label:'Agent',     sub:'Runs · subagents · scheduler' },
  { id:'projects', label:'Project',   sub:'Workspace & progress' },
  { id:'creds',    label:'Credential',sub:'Secret store' },
  { id:'config',   label:'Cấu hình',  sub:'harness.yaml' },
  { id:'chat',     label:'Chat',      sub:'Talk to your agent' },
];
const $ = (id) => document.getElementById(id);
const log=$('log'), input=$('in'), form=$('f'), btn=$('send'), view=$('view'),
  chatwrap=$('chatwrap'), approvals=$('approvals');
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
async function j(u){ const r=await fetch(u); return r.json(); }
function badge(s){ s=s||'idle'; const m={done:'ok',ok:'ok',running:'running',failed:'danger',
  error:'danger',denied:'warn',cancelled:'warn'}; const k=m[s]||'idle';
  return '<span class="badge b-'+k+'"><i></i>'+esc(String(s))+'</span>'; }
function row(k,v){ return '<div class="hrow"><span class="k">'+k+'</span><span class="grow"></span><span class="v">'+v+'</span></div>'; }
function empty(t){ return '<div class="empty">'+esc(t)+'</div>'; }
function runRow(x){ const id=x.trace_id||'';
  return '<div class="hrow runrow" data-tid="'+esc(id)+'"><span class="mono">'+esc(id.slice(0,8))+
    '</span><span class="grow"></span>'+badge((x.attrs||{}).status)+
    '<span style="color:var(--faint);font-size:11px;margin-left:8px">xem →</span></div>'; }
async function openTrace(id){
  $('mtitle').textContent='Trace '+id.slice(0,8);
  $('mbody').innerHTML='<div class="empty">Đang tải…</div>';
  $('modal').classList.add('on');
  const d=await j('/api/trace?id='+encodeURIComponent(id));
  const spans=d.spans||[];
  if(!spans.length){ $('mbody').innerHTML=empty('Không có span.'); return; }
  $('mbody').innerHTML=spans.map(s=>{
    const dur=(s.end_ts&&s.start_ts)?((s.end_ts-s.start_ts)*1000).toFixed(0)+'ms':'—';
    const a=s.attrs||{}; const st=a.status?badge(a.status):'';
    const keys=Object.keys(a).filter(k=>k!=='status');
    const attrs=keys.length?'<div class="span-attr">'+esc(keys.map(k=>k+': '+JSON.stringify(a[k])).join('\\n'))+'</div>':'';
    return '<div class="span"><div class="span-h"><span class="span-k">'+esc(s.kind||'')+'</span><span class="v">'+
      esc(s.name||'')+'</span><span class="grow"></span>'+st+'<span class="mono" style="margin-left:8px">'+dur+'</span></div>'+attrs+'</div>';
  }).join('');
}

// ---- scheduler helpers ----
// Render theo tz cấu hình (khớp spec người dùng gõ), KHÔNG theo tz trình duyệt — nếu không
// job "0 9 * * *" (giờ Asia/Ho_Chi_Minh) sẽ hiện lệch trên máy đặt tz khác.
function fmtTs(t, tz){ if(!t) return '—';
  const opt={hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'};
  if(tz){ try{ return new Date(t*1000).toLocaleString('vi-VN',{...opt,timeZone:tz}); }catch(e){} }
  try{ return new Date(t*1000).toLocaleString('vi-VN',opt); }catch(e){ return String(t); } }
function jrow(jb, tz){ return '<tr><td class="v">'+esc(jb.id)+(jb.running?' '+badge('running'):'')+'</td>'+
  '<td class="mono">'+esc(jb.spec)+'</td><td>'+fmtTs(jb.next_run,tz)+'</td><td class="mono">'+fmtTs(jb.last_run,tz)+
  '</td><td style="text-align:right;white-space:nowrap"><button class="jbtn" data-run="'+esc(jb.id)+
  '">Chạy</button> <button class="jbtn" data-del="'+esc(jb.id)+'">Xóa</button></td></tr>'; }
async function addJob(){ const id=$('jid').value.trim(), spec=$('jspec').value.trim(), prompt=$('jprompt').value.trim();
  const msg=$('jmsg'); if(!(id&&spec&&prompt)){ msg.textContent='cần id, spec, prompt'; return; }
  const r=await postJSON('/api/jobs',{id,spec,prompt,session:'main'});
  if(r.error){ msg.textContent='❌ '+r.error; } else renderAgents(); }
async function jobAction(u,id){ await postJSON(u,{id}); renderAgents(); }

// ---- builder sinh YAML (form → snippet để dán vào Cấu hình; KHÔNG tự ghi để tránh
//      mất comment / trùng key im lặng làm mất entry cũ; Save đã validate + hot-reload) ----
function csvArr(s){ return (s||'').split(',').map(x=>x.trim()).filter(Boolean); }
function yList(a){ return '['+a.map(x=>JSON.stringify(x)).join(', ')+']'; }
const CFIELDS={
  host:[['name','id (uat-01)'],['address','address (10.0.0.5)'],['user','user (deploy)'],
    ['port','port (22)'],['auth','tên secret keyfile (ssh_uat01)'],['tier','tier: uat | restricted'],
    ['vpn_required','vpn (tùy chọn)'],['log_paths','log_paths, phẩy (tùy chọn)'],['deploy_script','deploy_script (tùy chọn)']],
  vpn:[['name','id (fortinet-hn)'],['cred_secret','tên secret (vpn_fortinet)']],
  db:[['name','id (uat)'],['driver','postgres|mysql|sqlserver|sqlite'],['dsn_secret','tên secret DSN (uat_dsn)']],
};
function renderCfields(){ const k=$('ckind').value;
  $('cfields').innerHTML=CFIELDS[k].map(f=>'<input class="jin cf" data-k="'+f[0]+'" placeholder="'+f[1]+'">').join(''); }
function credYaml(k,v){
  if(k==='host'){ let s='remote:\\n  hosts:\\n    '+(v.name||'HOST')+':\\n      address: '+(v.address||'')+
      '\\n      user: '+(v.user||'')+'\\n      port: '+(v.port||'22')+'\\n      auth: keyfile:'+(v.auth||'SECRET_NAME')+
      '\\n      tier: '+(v.tier||'uat');
    if(v.vpn_required) s+='\\n      vpn_required: '+v.vpn_required;
    if(v.log_paths) s+='\\n      log_paths: '+yList(csvArr(v.log_paths));
    if(v.deploy_script) s+='\\n      deploy_script: '+v.deploy_script;
    return s; }
  if(k==='vpn') return 'remote:\\n  vpn_profiles:\\n    '+(v.name||'VPN')+': { cred_secret: '+(v.cred_secret||'SECRET_NAME')+' }';
  return 'databases:\\n  '+(v.name||'DB')+': { driver: '+(v.driver||'postgres')+', dsn_secret: '+(v.dsn_secret||'SECRET_NAME')+', readonly: true }';
}
function projYaml(v){ let s='projects:\\n  '+(v.name||'PROJECT')+':\\n    path: '+(v.path||'/path/to/project');
  if(v.hosts) s+='\\n    hosts: '+yList(csvArr(v.hosts));
  if(v.databases) s+='\\n    databases: '+yList(csvArr(v.databases));
  return s; }

// ---- nav / routing ----
let current='overview';
function buildNav(){
  $('nav').innerHTML = NAV.map(n=>'<div class="nav-item" data-id="'+n.id+'"><span>'+n.label+'</span></div>').join('');
  document.querySelectorAll('.nav-item').forEach(el=>el.onclick=()=>show(el.dataset.id));
}
function show(id){
  current=id;
  const n = NAV.find(x=>x.id===id) || NAV[0];
  $('ttl').textContent=n.label; $('tsub').textContent=n.sub;
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('on',el.dataset.id===id));
  const isChat = id==='chat';
  chatwrap.style.display = isChat?'flex':'none';
  view.style.display = isChat?'none':'block';
  if(id==='overview') renderOverview();
  else if(id==='agents') renderAgents();
  else if(id==='projects') renderProjects();
  else if(id==='creds') renderCreds();
  else if(id==='config') renderConfig();
}

// ---- screens (dữ liệu thật) ----
async function renderOverview(){
  view.innerHTML='<div class="empty">Đang tải…</div>';
  const [m,u,t] = await Promise.all([j('/api/meta'),j('/api/usage'),j('/api/traces')]);
  let total=0; for(const k in (u.rows||{})) total+=u.rows[k].cost_usd||0;
  const b=m.budget||{}, sb=m.sandbox||{}, c=m.counts||{};
  const tiles=[
    ['Chi phí phiên','$'+total.toFixed(4), b.monthly_alert_usd!=null?('ngưỡng $'+b.monthly_alert_usd):'—'],
    ['Provider', esc(m.provider||'—'), esc(m.model||'')],
    ['Sandbox', esc(sb.backend||'—'), 'network: '+esc(sb.network||'—')],
    ['Vòng tối đa', String(b.max_loop_iterations||'—'), (b.context_token_budget||0).toLocaleString()+' token ctx'],
  ];
  let h='<div class="tiles">'+tiles.map(x=>'<div class="tile"><div class="cap">'+x[0]+'</div><div class="stat">'+x[1]+'</div><div class="tmeta">'+x[2]+'</div></div>').join('')+'</div>';
  h+='<div class="grid2"><div class="card"><div class="ct">Trạng thái hệ thống</div>'+
    row('Chế độ duyệt',esc(m.approval_mode||'—'))+row('Timezone',esc(m.timezone||'—'))+
    row('Skills', m.skills_enabled?'bật':'tắt')+row('Subagents', m.subagents_enabled?'bật':'tắt')+
    row('Tool đang bật', String(c.tools||0))+
    row('Project / DB / Host', (c.projects||0)+' / '+(c.databases||0)+' / '+(c.hosts||0))+'</div>';
  const tr=(t.traces||[]);
  h+='<div class="card"><div class="ct">Lượt gần đây</div>';
  h+= tr.length ? tr.slice(0,8).map(runRow).join('')
                : empty('Chưa có lượt chạy. Gửi tin ở tab Chat để tạo trace.');
  h+='</div></div>';
  view.innerHTML=h;
}
async function renderAgents(){
  view.innerHTML='<div class="empty">Đang tải…</div>';
  const [sa,t,jb,m] = await Promise.all([j('/api/subagents'),j('/api/traces'),j('/api/jobs'),j('/api/meta')]);
  const subs=sa.subagents||[], tr=t.traces||[], jobs=jb.jobs||[], tz=(m&&m.timezone)||null;
  let h='<div class="card"><div class="ct">Subagent</div>';
  if(!subs.length) h+=empty('Chưa có subagent def nào trong {workspace}/agents/*.md.');
  else h+=subs.map((s,i)=>{
    const grad = i%2 ? 'radial-gradient(circle at 38% 32%,#8FC4EC,#173E66)' : 'radial-gradient(circle at 38% 32%,#8FD9AE,#1B5E3C)';
    return '<div class="subrow"><span class="subav" style="background:'+grad+'"></span>'+
      '<div class="grow"><div class="v">'+esc(s.name)+'</div>'+
      '<div class="tmeta" style="margin:2px 0 4px">'+esc(s.description||'')+'</div>'+
      '<div class="chips">'+(s.toolset||[]).map(x=>'<span class="chip">'+esc(x)+'</span>').join('')+'</div></div>'+
      '<span class="mono" style="text-align:right">'+(s.max_iterations||0)+' vòng<br>'+((s.token_budget||0)/1000)+'k token</span></div>';
  }).join('');
  h+='</div>';
  h+='<div class="card"><div class="ct">Lượt chạy (trace)</div>';
  h+= tr.length ? tr.slice(0,20).map(runRow).join('')
                : empty('Chưa có lượt chạy nào.');
  h+='</div>';
  h+='<div class="card"><div class="ct">Job theo lịch</div>'+
    '<div style="display:grid;grid-template-columns:0.8fr 1.5fr 2fr auto;gap:8px">'+
    '<input id="jid" class="jin" placeholder="id (db-health)">'+
    '<input id="jspec" class="jin" placeholder="every:3600 | cron:0 8 * * 1 | at:2026-07-06T17:00">'+
    '<input id="jprompt" class="jin" placeholder="prompt agent chạy">'+
    '<button class="send" id="jadd">Thêm</button></div>'+
    '<div id="jmsg" style="font-size:11.5px;color:var(--danger);min-height:15px;margin:6px 0 4px"></div>';
  h+= jobs.length
    ? '<table class="tbl"><tr><th>Job</th><th>Spec</th><th>Kế tiếp ('+esc(tz||'local')+')</th><th>Lần trước</th><th></th></tr>'+jobs.map(j=>jrow(j,tz)).join('')+'</table>'
    : empty('Chưa có job. Thêm ở trên (spec: at: / every: / cron:).');
  h+='</div>';
  view.innerHTML=h;
  const jadd=$('jadd'); if(jadd) jadd.onclick=addJob;
  view.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>jobAction('/api/jobs/run',b.dataset.run));
  view.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>jobAction('/api/jobs/delete',b.dataset.del));
}
async function renderProjects(){
  view.innerHTML='<div class="empty">Đang tải…</div>';
  const p=await j('/api/projects');
  let h='<div class="note">workspace_root: <b style="color:var(--ink)">'+esc(p.workspace_root||'—')+'</b></div>';
  const ps=p.projects||[];
  if(!ps.length){ h+='<div class="card">'+empty('Chưa đăng ký project nào trong config (mục projects).')+'</div>'; }
  else h+=ps.map(r=>'<div class="card"><div class="hrow" style="border:0;padding:0">'+
    '<div class="grow"><div class="v" style="font-size:15px">'+esc(r.name)+'</div>'+
    '<div class="mono">'+esc(r.path)+'</div>'+
    (((r.hosts&&r.hosts.length)||(r.databases&&r.databases.length))
      ? '<div class="chips" style="margin-top:6px">'+
        (r.hosts||[]).map(x=>'<span class="chip">ssh: '+esc(x)+'</span>').join('')+
        (r.databases||[]).map(x=>'<span class="chip">db: '+esc(x)+'</span>').join('')+'</div>'
      : '')+'</div>'+
    (r.exists?badge('ok'):badge('danger'))+'<span style="font-size:11.5px;color:var(--faint);margin:0 10px 0 6px">'+(r.exists?'tồn tại':'không thấy path')+'</span>'+
    '<button class="jbtn" data-proj="'+esc(r.name)+'">Mở trong Chat</button>'+
    '</div></div>').join('');
  h+='<div class="card"><div class="ct">Thêm Project (sinh YAML)</div>'+
    '<div class="note" style="margin:0 0 10px">Điền → Sinh YAML → dán vào mục <span class="chip">projects:</span> trong tab Cấu hình rồi Lưu (áp dụng ngay). hosts/databases là TÊN tham chiếu remote.hosts / databases.</div>'+
    '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px">'+
    '<input id="pn" class="jin" placeholder="id (go-learn)"><input id="pp" class="jin" placeholder="path (/mnt/d/work/go-learn)">'+
    '<input id="ph" class="jin" placeholder="hosts: tên, phẩy — tùy chọn"><input id="pd" class="jin" placeholder="databases: tên, phẩy — tùy chọn"></div>'+
    '<div style="margin-top:10px;display:flex;gap:8px"><button class="send" id="pgen">Sinh YAML</button>'+
    '<button class="jbtn" id="pgo">Mở tab Cấu hình</button></div>'+
    '<pre id="pyaml" style="display:none;background:var(--side);border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px;font:12px/1.5 var(--font-mono);white-space:pre-wrap;color:var(--body)"></pre>'+
    '</div>';
  view.innerHTML=h;
  view.querySelectorAll('[data-proj]').forEach(b=>b.onclick=()=>setProject(b.dataset.proj));
  $('pgen').onclick=()=>{ const v={name:$('pn').value.trim(),path:$('pp').value.trim(),
    hosts:$('ph').value.trim(),databases:$('pd').value.trim()};
    const pre=$('pyaml'); pre.style.display='block'; pre.textContent=projYaml(v); };
  $('pgo').onclick=()=>show('config');
}
let activeProject=null;
function setProject(name){ activeProject=name; renderPbar(); show('chat'); }
function renderPbar(){ const b=$('pbar');
  if(!activeProject){ b.innerHTML=''; b.style.padding='0'; return; }
  b.style.padding='10px 24px 0';
  b.innerHTML='<span class="pill" style="gap:8px">Project: <b style="color:var(--ink)">'+esc(activeProject)+
    '</b> <span id="pclr" style="cursor:pointer;color:var(--mut)">✕</span></span>';
  $('pclr').onclick=()=>{ activeProject=null; renderPbar(); }; }
async function renderCreds(){
  view.innerHTML='<div class="empty">Đang tải…</div>';
  const [s,m]=await Promise.all([j('/api/secrets'),j('/api/meta')]);
  let h='<div class="note">Giá trị secret <b style="color:var(--ink)">không bao giờ</b> vào context của model, span, log hay checkpoint. UI chỉ hiển thị <b style="color:var(--ink)">tên</b> secret.</div>';
  if(m.provider_inline_key) h+='<div class="note" style="background:#FCF7E8;border-color:var(--warn-line)">Provider đang dùng <b style="color:var(--ink)">api_key inline</b> trong harness.yaml (không qua secret store). Khuyến nghị chuyển sang env <span class="chip">YETT_SECRET_LLM_KEY</span> + api_key_secret.</div>';
  const rows=s.secrets||[];
  h+='<div class="card">';
  if(!rows.length) h+=empty('Config không tham chiếu secret có tên nào.');
  else h+='<table class="tbl"><tr><th>Secret</th><th>Dùng bởi</th><th>Trạng thái</th></tr>'+
    rows.map(c=>'<tr><td class="mono" style="color:var(--ink);font-weight:600">'+esc(c.name)+'</td><td>'+esc(c.source)+'</td><td>'+(c.isset?badge('ok')+' <span style="font-size:11.5px;color:var(--faint)">đã đặt</span>':badge('danger')+' <span style="font-size:11.5px;color:var(--faint)">thiếu</span>')+'</td></tr>').join('')+'</table>';
  h+='</div>';
  h+='<div class="card"><div class="ct">Thêm Host / VPN / DB (sinh YAML)</div>'+
    '<div class="note" style="margin:0 0 10px">Điền → <b style="color:var(--ink)">Sinh YAML</b> → dán vào đúng mục trong tab Cấu hình rồi Lưu (áp dụng ngay). '+
    'VALUE secret đặt qua env <span class="chip">YETT_SECRET_&lt;TÊN&gt;</span> — KHÔNG nhập ở đây.</div>'+
    '<select id="ckind" class="jin" style="margin-bottom:8px"><option value="host">Host / SSH</option><option value="vpn">VPN</option><option value="db">Database</option></select>'+
    '<div id="cfields" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px"></div>'+
    '<div style="margin-top:10px;display:flex;gap:8px"><button class="send" id="cgen">Sinh YAML</button>'+
    '<button class="jbtn" id="cgo">Mở tab Cấu hình</button></div>'+
    '<pre id="cyaml" style="display:none;background:var(--side);border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px;font:12px/1.5 var(--font-mono);white-space:pre-wrap;color:var(--body)"></pre>'+
    '</div>';
  view.innerHTML=h;
  renderCfields();
  $('ckind').onchange=renderCfields;
  $('cgen').onclick=()=>{ const v={}; view.querySelectorAll('.cf').forEach(i=>v[i.dataset.k]=i.value.trim());
    const pre=$('cyaml'); pre.style.display='block'; pre.textContent=credYaml($('ckind').value,v); };
  $('cgo').onclick=()=>show('config');
}
async function renderConfig(){
  view.innerHTML='<div class="empty">Đang tải…</div>';
  const c=await j('/api/config');
  view.innerHTML='<div style="color:var(--faint);font-size:12px;margin-bottom:10px">File: '+esc(c.path||'?')+
    ' · secret hiển thị <span class="chip">[REDACTED]</span>, lưu sẽ giữ nguyên value thật trên đĩa</div>'+
    '<textarea id="cfgtext" spellcheck="false" style="width:100%;min-height:58vh;resize:vertical;background:var(--side);'+
    'color:var(--body);border:1px solid var(--line);border-radius:8px;padding:12px;font:13px/1.6 var(--font-mono)"></textarea>'+
    '<div style="margin-top:10px;display:flex;gap:10px;align-items:center"><button class="send" id="cfgsave">Lưu cấu hình</button>'+
    '<span id="cfgmsg" style="font-size:12.5px;color:var(--acc)"></span></div>';
  $('cfgtext').value=c.text||'';
  $('cfgsave').onclick=saveCfg;
}
async function saveCfg(){
  const msg=$('cfgmsg'); msg.style.color='var(--mut)'; msg.textContent='Đang lưu...';
  try{ const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:$('cfgtext').value})}); const jj=await r.json();
    if(jj.error){ msg.style.color='var(--danger)'; msg.textContent='❌ '+jj.error; }
    else{ msg.style.color='var(--acc)'; msg.textContent='✓ '+(jj.note||'Đã lưu'); }
  }catch(e){ msg.style.color='var(--danger)'; msg.textContent='Lỗi: '+e; }
}

// ---- chat ----
function add(cls,text){ const d=document.createElement('div'); d.className='msg '+cls; d.textContent=text;
  log.appendChild(d); log.scrollTop=log.scrollHeight; return d; }
function metaLine(text){ const d=document.createElement('div'); d.className='meta'; d.textContent=text;
  log.appendChild(d); log.scrollTop=log.scrollHeight; }
async function postJSON(u,b){ const r=await fetch(u,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return r.json(); }
async function send(msg, resumeId){
  const tid = resumeId || ('turn-'+Date.now());
  if(!resumeId) add('user',msg);
  input.value=''; btn.disabled=true; $('stop').style.display='inline-block';
  try{ const jj=await postJSON('/api/chat',{message:msg,session:'main',turn_id:tid,project:activeProject});
    if(jj.error){ add('err','Lỗi: '+jj.error); }
    else if(jj.status==='canceled'){
      metaLine('[đã hủy · '+jj.iterations+' vòng · trace '+(jj.trace_id||'').slice(0,8)+']');
      addResume(tid,msg); }
    else{ add('agent',jj.text||'(trống)');
      metaLine('['+jj.status+' · '+jj.iterations+' vòng · trace '+(jj.trace_id||'').slice(0,8)+']'); }
  }catch(e){ add('err','Lỗi kết nối: '+e); }
  finally{ btn.disabled=false; $('stop').style.display='none'; input.focus(); refreshCost(); } }
function addResume(tid,msg){ const d=document.createElement('div'); d.className='meta';
  const b=document.createElement('button'); b.className='jbtn'; b.textContent='↻ Resume lượt này';
  b.style.cssText='color:var(--acc);border-color:var(--acc)';
  b.onclick=()=>{ d.remove(); send(msg,tid); };
  d.appendChild(b); log.appendChild(d); log.scrollTop=log.scrollHeight; }
$('stop').onclick=()=>{ postJSON('/api/cancel',{session:'main'}).catch(()=>{}); };
form.addEventListener('submit',e=>{ e.preventDefault(); const m=input.value.trim(); if(m) send(m); });
input.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault();
  const m=input.value.trim(); if(m) send(m); } });

// ---- approval (poll, hiện ở mọi màn) ----
const seen=new Set();
function showApproval(p){
  if(seen.has(p.id)) return; seen.add(p.id);
  const d=document.createElement('div'); d.className='approve';
  const args=Object.entries(p.args||{}).map(([k,v])=>k+': '+v).join('\\n');
  d.innerHTML='<b>⚠️ Cần bạn duyệt</b> — tool <b>'+esc(p.tool)+'</b>';
  const code=document.createElement('code'); code.textContent=args; d.appendChild(code);
  const ok=document.createElement('button'); ok.className='ok'; ok.textContent='Duyệt';
  const no=document.createElement('button'); no.className='no'; no.textContent='Từ chối';
  ok.onclick=()=>decide(p.id,true,d); no.onclick=()=>decide(p.id,false,d);
  d.appendChild(ok); d.appendChild(no); approvals.appendChild(d);
}
async function decide(id,approved,el){ el.querySelectorAll('button').forEach(b=>b.disabled=true);
  await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id,approved})});
  el.innerHTML+='<div class="mut">→ '+(approved?'đã duyệt':'đã từ chối')+'</div>'; }
async function pollPending(){ try{ const jj=await j('/api/pending'); (jj.pending||[]).forEach(showApproval); }catch(e){} }
setInterval(pollPending,1000);

// ---- cost + provider pills ----
async function refreshCost(){ try{ const u=await j('/api/usage'); let t=0;
  for(const k in (u.rows||{})) t+=u.rows[k].cost_usd||0;
  $('cost').textContent='Chi phí: $'+t.toFixed(4); }catch(e){} }
async function boot(){
  try{ const m=await j('/api/meta'); $('prov').textContent=m.provider+' · '+m.model;
    const sb=m.sandbox||{}; $('fsandbox').textContent=sb.backend+(sb.network?(' · '+sb.network):''); }catch(e){}
  refreshCost();
}
document.addEventListener('click',e=>{ const r=e.target.closest('.runrow');
  if(r&&r.dataset.tid) openTrace(r.dataset.tid); });
$('mclose').onclick=()=>$('modal').classList.remove('on');
$('modal').onclick=e=>{ if(e.target.id==='modal') $('modal').classList.remove('on'); };
buildNav(); boot(); show('overview'); pollPending();
</script></body></html>"""

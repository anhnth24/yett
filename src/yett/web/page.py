"""Trang chat self-contained (không asset ngoài): chat + duyệt approval + panel traces/cost."""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>yett</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#252a34; --txt:#e6e8ec; --mut:#9aa4b2;
        --acc:#4f8cff; --usr:#1e2836; --agt:#1b2119; --err:#3a1e1e; --warn:#3a3320; }
* { box-sizing:border-box; } body { margin:0; font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
  background:var(--bg); color:var(--txt); height:100vh; display:flex; flex-direction:column; }
header { padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--line);
  display:flex; align-items:center; gap:12px; }
header b { font-size:16px; } header .mut { color:var(--mut); font-size:13px; }
header button.tab { background:transparent; color:var(--mut); border:1px solid var(--line);
  border-radius:6px; padding:4px 10px; cursor:pointer; font:inherit; }
header button.tab.on { color:var(--txt); border-color:var(--acc); }
#log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
.msg { max-width:820px; padding:10px 14px; border-radius:10px; white-space:pre-wrap; word-wrap:break-word; }
.user { align-self:flex-end; background:var(--usr); }
.agent { align-self:flex-start; background:var(--agt); }
.meta { align-self:flex-start; color:var(--mut); font-size:12px; }
.err { background:var(--err); }
.approve { align-self:flex-start; background:var(--warn); border:1px solid #6b5d1f; max-width:820px;
  padding:12px 14px; border-radius:10px; }
.approve b { color:#ffd76a; } .approve code { display:block; white-space:pre-wrap; margin:6px 0;
  padding:6px 8px; background:#0c0d10; border-radius:6px; font-size:13px; }
.approve button { margin-right:8px; border:0; border-radius:6px; padding:6px 14px; cursor:pointer; font:inherit; }
.ok { background:#2c7a4b; color:#fff; } .no { background:#8a3232; color:#fff; }
form { display:flex; gap:8px; padding:12px 16px; background:var(--panel); border-top:1px solid var(--line); }
textarea { flex:1; resize:none; background:var(--bg); color:var(--txt); border:1px solid var(--line);
  border-radius:8px; padding:10px; font:inherit; height:44px; }
button.send { background:var(--acc); color:#fff; border:0; border-radius:8px; padding:0 18px; font:inherit; cursor:pointer; }
button.send:disabled { opacity:.5; cursor:default; }
#panel { display:none; padding:16px; overflow-y:auto; }
#panel table { width:100%; border-collapse:collapse; font-size:13px; }
#panel th,#panel td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
#panel h3 { margin:16px 0 8px; }
</style></head><body>
<header><b>yett</b><span class="mut" id="prov"></span>
  <span class="mut" id="cost" style="margin-left:auto"></span>
  <button class="tab on" id="tabChat">Chat</button>
  <button class="tab" id="tabPanel">Traces / Cost</button>
  <button class="tab" id="tabCfg">Cấu hình</button>
</header>
<div id="log"></div>
<div id="panel"></div>
<div id="cfg" style="display:none; padding:16px; overflow-y:auto; flex-direction:column;">
  <div class="mut" id="cfgpath" style="margin-bottom:8px"></div>
  <textarea id="cfgtext" spellcheck="false" style="width:100%; flex:1; min-height:60vh; resize:vertical;
    background:#0c0d10; color:#e6e8ec; border:1px solid #252a34; border-radius:8px; padding:12px;
    font:13px/1.5 ui-monospace,Consolas,monospace;"></textarea>
  <div style="margin-top:10px; display:flex; gap:10px; align-items:center;">
    <button class="send" id="cfgsave">Lưu cấu hình</button>
    <span class="mut" id="cfgmsg"></span>
  </div>
</div>
<form id="f"><textarea id="in" placeholder="Nhập tin nhắn... (Enter gửi, Shift+Enter xuống dòng)"></textarea>
<button class="send" id="send">Gửi</button></form>
<script>
const log=document.getElementById('log'), input=document.getElementById('in'),
  form=document.getElementById('f'), btn=document.getElementById('send'),
  panel=document.getElementById('panel');
const seen=new Set();  // approval id đã hiển thị
function add(cls,text){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
function meta(text){const d=document.createElement('div');d.className='meta';d.textContent=text;
  log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function refreshCost(){try{const r=await fetch('/api/usage');const j=await r.json();
  let t=0;for(const k in j.rows)t+=j.rows[k].cost_usd;
  document.getElementById('cost').textContent='Chi phí: $'+t.toFixed(4);}catch(e){}}
// --- Approval polling ---
function showApproval(p){
  if(seen.has(p.id))return; seen.add(p.id);
  const d=document.createElement('div');d.className='approve';
  const args=Object.entries(p.args).map(([k,v])=>k+': '+v).join('\\n');
  d.innerHTML='<b>⚠️ Cần bạn duyệt</b> — tool <b>'+p.tool+'</b>';
  const code=document.createElement('code');code.textContent=args;d.appendChild(code);
  const ok=document.createElement('button');ok.className='ok';ok.textContent='Duyệt';
  const no=document.createElement('button');no.className='no';no.textContent='Từ chối';
  ok.onclick=()=>decide(p.id,true,d); no.onclick=()=>decide(p.id,false,d);
  d.appendChild(ok);d.appendChild(no);log.appendChild(d);log.scrollTop=log.scrollHeight;
}
async function decide(id,approved,el){el.querySelectorAll('button').forEach(b=>b.disabled=true);
  await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id,approved})});
  el.innerHTML+='<div class="mut">→ '+(approved?'đã duyệt':'đã từ chối')+'</div>';}
async function pollPending(){try{const r=await fetch('/api/pending');const j=await r.json();
  (j.pending||[]).forEach(showApproval);}catch(e){}}
setInterval(pollPending,1000);
// --- Chat ---
async function send(msg){add('user',msg);input.value='';btn.disabled=true;
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,session:'main'})});const j=await r.json();
    if(j.error){add('err','Lỗi: '+j.error);}else{add('agent',j.text||'(trống)');
      meta('['+j.status+' · '+j.iterations+' vòng · trace '+(j.trace_id||'').slice(0,8)+']');}
  }catch(e){add('err','Lỗi kết nối: '+e);} finally{btn.disabled=false;input.focus();refreshCost();}}
form.addEventListener('submit',e=>{e.preventDefault();const m=input.value.trim();if(m)send(m);});
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();
  const m=input.value.trim();if(m)send(m);}});
// --- Tabs ---
const tc=document.getElementById('tabChat'), tp=document.getElementById('tabPanel'),
  tcfg=document.getElementById('tabCfg'), cfg=document.getElementById('cfg');
function tabs(on){[tc,tp,tcfg].forEach(t=>t.classList.remove('on'));on.classList.add('on');}
tc.onclick=()=>{tabs(tc);log.style.display='flex';panel.style.display='none';cfg.style.display='none';form.style.display='flex';};
tp.onclick=async()=>{tabs(tp);log.style.display='none';form.style.display='none';cfg.style.display='none';
  panel.style.display='block';await loadPanel();};
tcfg.onclick=async()=>{tabs(tcfg);log.style.display='none';form.style.display='none';panel.style.display='none';
  cfg.style.display='flex';await loadCfg();};
async function loadCfg(){try{const j=await(await fetch('/api/config')).json();
  document.getElementById('cfgtext').value=j.text||'';
  document.getElementById('cfgpath').textContent='File: '+(j.path||'?')+'  (secret nên để ở secrets/ hoặc env)';
  document.getElementById('cfgmsg').textContent='';}catch(e){}}
document.getElementById('cfgsave').onclick=async()=>{const msg=document.getElementById('cfgmsg');
  msg.textContent='Đang lưu...';
  try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:document.getElementById('cfgtext').value})});const j=await r.json();
    msg.textContent=j.error?('❌ '+j.error):('✓ '+(j.note||'Đã lưu'));}catch(e){msg.textContent='Lỗi: '+e;}};
async function loadPanel(){let h='<h3>Chi phí theo provider</h3><table><tr><th>Provider</th><th>Cost (USD)</th><th>Calls</th></tr>';
  try{const u=await(await fetch('/api/usage')).json();for(const k in u.rows){const v=u.rows[k];
    h+='<tr><td>'+k+'</td><td>$'+v.cost_usd.toFixed(4)+'</td><td>'+v.calls+'</td></tr>';}}catch(e){}
  h+='</table><h3>Trace gần đây</h3><table><tr><th>Trace</th><th>Trạng thái</th></tr>';
  try{const t=await(await fetch('/api/traces')).json();(t.traces||[]).forEach(x=>{
    h+='<tr><td>'+x.trace_id.slice(0,8)+'</td><td>'+(x.attrs.status||'?')+'</td></tr>';});}catch(e){}
  h+='</table>';panel.innerHTML=h;}
fetch('/api/health').then(r=>r.json()).then(j=>{document.getElementById('prov').textContent=
  j.provider+' · '+j.model;}).catch(()=>{});
refreshCost();input.focus();
</script></body></html>"""

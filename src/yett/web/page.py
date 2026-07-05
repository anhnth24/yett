"""Trang chat self-contained (không asset ngoài) cho web UI local."""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>yett</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#252a34; --txt:#e6e8ec; --mut:#9aa4b2;
        --acc:#4f8cff; --usr:#1e2836; --agt:#1b2119; --err:#3a1e1e; }
* { box-sizing:border-box; } body { margin:0; font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
  background:var(--bg); color:var(--txt); height:100vh; display:flex; flex-direction:column; }
header { padding:10px 16px; background:var(--panel); border-bottom:1px solid var(--line);
  display:flex; align-items:center; gap:12px; }
header b { font-size:16px; } header .mut { color:var(--mut); font-size:13px; }
#log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
.msg { max-width:820px; padding:10px 14px; border-radius:10px; white-space:pre-wrap; word-wrap:break-word; }
.user { align-self:flex-end; background:var(--usr); }
.agent { align-self:flex-start; background:var(--agt); }
.meta { align-self:flex-start; color:var(--mut); font-size:12px; }
.err { background:var(--err); }
form { display:flex; gap:8px; padding:12px 16px; background:var(--panel); border-top:1px solid var(--line); }
textarea { flex:1; resize:none; background:var(--bg); color:var(--txt); border:1px solid var(--line);
  border-radius:8px; padding:10px; font:inherit; height:44px; }
button { background:var(--acc); color:#fff; border:0; border-radius:8px; padding:0 18px; font:inherit; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; }
</style></head><body>
<header><b>yett</b><span class="mut" id="prov"></span><span class="mut" id="cost" style="margin-left:auto"></span></header>
<div id="log"></div>
<form id="f"><textarea id="in" placeholder="Nhập tin nhắn... (Enter gửi, Shift+Enter xuống dòng)"></textarea>
<button id="send">Gửi</button></form>
<script>
const log=document.getElementById('log'), input=document.getElementById('in'),
  form=document.getElementById('f'), btn=document.getElementById('send');
function add(cls,text){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
function meta(text){const d=document.createElement('div');d.className='meta';d.textContent=text;
  log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function refreshCost(){try{const r=await fetch('/api/usage');const j=await r.json();
  let t=0;for(const k in j.rows)t+=j.rows[k].cost_usd;
  document.getElementById('cost').textContent='Chi phí phiên: $'+t.toFixed(4);}catch(e){}}
async function send(msg){add('user',msg);input.value='';btn.disabled=true;
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,session:'main'})});const j=await r.json();
    if(j.error){add('err','Lỗi: '+j.error);}else{add('agent',j.text||'(trống)');
      meta('['+j.status+' · '+j.iterations+' vòng · trace '+(j.trace_id||'').slice(0,8)+']');}
  }catch(e){add('err','Lỗi kết nối: '+e);} finally{btn.disabled=false;input.focus();refreshCost();}}
form.addEventListener('submit',e=>{e.preventDefault();const m=input.value.trim();if(m)send(m);});
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();
  const m=input.value.trim();if(m)send(m);}});
fetch('/api/health').then(r=>r.json()).then(j=>{document.getElementById('prov').textContent=
  j.provider+' · '+j.model;}).catch(()=>{});
refreshCost();input.focus();
</script></body></html>"""

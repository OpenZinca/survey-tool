#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Survey Tool 1.0
# A single-file, self-hosted survey engine.
#
# Copyright (c) 2026 Zinca Inc.
# An OpenZinca Project — https://github.com/OpenZinca
# Released under the MIT License (see LICENSE).
#
# Features
#   - Single Python file, SQLite storage, no external services required
#   - Question types: radio, checkbox (with optional cap and follow-up),
#     1-5 scale, dropdown select, free text, email field, tap-to-rank
#   - Conditional questions (show_if / show_if_any / show_any) and
#     early-exit screening answers (end_if)
#   - Bilingual out of the box (English canonical + optional overlay
#     for a second language; French included as the example)
#   - Auto-saved partial responses, marked "partial" until submitted
#   - Response funnel counters (views / starts / completes)
#   - Anonymous respondent codes: date-time + region + sequence
#   - Admin dashboard: totals, funnel, per-question distributions,
#     full CSV export with complete question and option text
#   - Admin login with per-IP lockout after repeated failures
#
# Configuration (environment variables)
#   SURVEY_ADMIN_PASS   required — admin dashboard password
#   SURVEY_HOST         optional — public hostname for the survey;
#                       when set together with ADMIN_HOST, routing is
#                       host-based. When unset, admin lives at /admin.
#   SURVEY_ADMIN_HOST   optional — hostname for the admin dashboard
#   SURVEY_DB           optional — path to the SQLite file
#
# Run:  uvicorn app:app --host 127.0.0.1 --port 8800
# ---------------------------------------------------------------------------
import json, os, sqlite3, time, secrets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("SURVEY_DB", os.path.join(BASE, "survey.db"))
SURVEY_HOST = os.environ.get("SURVEY_HOST", "")
ADMIN_HOST = os.environ.get("SURVEY_ADMIN_HOST", "")
ADMIN_PASS = os.environ.get("SURVEY_ADMIN_PASS", "")
if not ADMIN_PASS:
    raise SystemExit("Set SURVEY_ADMIN_PASS before starting the server.")
LOCK_FAILS = 3          # failed logins allowed before lockout
LOCK_SECS = 300         # lockout duration in seconds
BRAND = '<a href="https://github.com/OpenZinca" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">Powered by Zinca Inc. · An OpenZinca Project</a>'

app = FastAPI()

def db():
    # WAL + busy timeout so concurrent submits never see "database is locked"
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=8000")
    return c

with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY, ts INTEGER, tok TEXT UNIQUE, lang TEXT, data TEXT, code TEXT, status TEXT DEFAULT 'complete')")
    c.execute("CREATE TABLE IF NOT EXISTS counters(k TEXT PRIMARY KEY, n INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS lockout(ip TEXT PRIMARY KEY, fails INTEGER, until INTEGER, last INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS sessions(tok TEXT PRIMARY KEY, ts INTEGER)")
    for k in ("view", "start", "complete"):
        c.execute("INSERT OR IGNORE INTO counters(k,n) VALUES(?,0)", (k,))

def bump(k):
    # Funnel counter increment. Never call inside another open transaction.
    with db() as c: c.execute("UPDATE counters SET n=n+1 WHERE k=?", (k,))

def client_ip(req: Request) -> str:
    # Honour the proxy header when behind a reverse proxy / CDN
    return req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "?")

def host_of(req: Request) -> str:
    return (req.headers.get("host") or "").split(":")[0].lower()

# ===========================================================================
# QUESTIONNAIRE DEFINITION
#
# Replace the demo content below with your own questions.
# Item fields:
#   sec           section header (display only)
#   id            unique answer key (required for questions)
#   t             type: radio | check | scale | select | text | rank
#   req           1 = required (required questions get visible numbering)
#   q             question text (English canonical)
#   o             option list (radio/check/select/rank)
#   max           checkbox cap, e.g. "pick up to 3"
#   top           follow-up shown when 2+ boxes are checked: pick the ONE
#   lo / hi       scale anchor labels
#   note          small helper line under the question
#   note_ph       placeholder for the per-question optional note box
#   end_if        list of option indexes that end the survey politely
#   end_msg       message shown when end_if triggers
#   show_if       [qid, index]        show only when that answer is chosen
#   show_if_any   [qid, [indexes]]    show when any of those are chosen
#   show_any      [[qid,[idx]], ...]  show when any listed condition holds
#   req_if_shown  1 = required whenever visible (for conditional items)
#   email         1 = validate value as an email address (text items)
# ===========================================================================
Q = [
 {"sec": "Section one"},
 {"id": "q1", "t": "radio", "req": 1, "q": "Demo screening question — which option describes you?", "o": [
   "Option that continues the survey", "Second option that continues", "Option that ends the survey"],
  "end_if": [2], "end_msg": "Thank you for your interest. This survey is not aimed at your situation."},
 {"id": "q2", "t": "check", "req": 1, "max": 3, "q": "Demo checkbox question — pick up to 3.", "o": [
   "Choice A", "Choice B", "Choice C", "Choice D", "Choice E"],
  "top": "Of the ones you picked, which ONE matters most?"},
 {"id": "q3", "t": "scale", "req": 1, "q": "Demo 1-5 scale question.", "lo": "Not at all", "hi": "Very much"},
 {"id": "q4", "t": "radio", "req": 1, "q": "Demo branching question.", "o": ["Path A", "Path B", "Other"]},
 {"id": "q4b", "t": "select", "req": 0, "show_if": ["q4", 2], "q": "(If other) Demo dropdown shown conditionally:", "o": [
   "Dropdown item 1", "Dropdown item 2", "Dropdown item 3"]},
 {"sec": "Section two"},
 {"id": "q5", "t": "rank", "req": 1, "q": "Demo rank question — tap in order of priority.", "o": [
   "Priority item 1", "Priority item 2", "Priority item 3", "Priority item 4"]},
 {"id": "q6", "t": "text", "req": 0, "q": "Demo open text question. (optional)"},
 {"id": "q7", "t": "radio", "req": 1, "q": "Would you like to receive the results by email?", "o": [
   "Yes, please", "No thanks"]},
 {"id": "q8", "t": "text", "email": 1, "req": 0, "req_if_shown": 1, "show_any": [["q7", [0]]],
  "q": "(If yes) Your email — used only for what you chose above:"},
]

# Second-language overlay, keyed by question id. Any field present here
# replaces the English one when the visitor switches language.
FR = {
 "_secs": {"Section one": "Première section", "Section two": "Deuxième section"},
 "q1": {"q": "Question de sélection (démo) — quelle option vous décrit ?", "o": [
   "Option qui poursuit le sondage", "Deuxième option qui poursuit", "Option qui met fin au sondage"],
   "end_msg": "Merci de votre intérêt. Ce sondage ne vise pas votre situation."},
 "q2": {"q": "Question à cases (démo) — jusqu'à 3 choix.", "o": [
   "Choix A", "Choix B", "Choix C", "Choix D", "Choix E"],
   "top": "Parmi vos choix, lequel compte LE plus ?"},
 "q3": {"q": "Question d'échelle 1-5 (démo).", "lo": "Pas du tout", "hi": "Beaucoup"},
 "q4": {"q": "Question à embranchement (démo).", "o": ["Voie A", "Voie B", "Autre"]},
 "q4b": {"q": "(Si autre) Liste déroulante conditionnelle (démo) :", "o": [
   "Élément 1", "Élément 2", "Élément 3"]},
 "q5": {"q": "Question de classement (démo) — touchez dans l'ordre de priorité.", "o": [
   "Priorité 1", "Priorité 2", "Priorité 3", "Priorité 4"]},
 "q6": {"q": "Question ouverte (démo). (facultatif)"},
 "q7": {"q": "Aimeriez-vous recevoir les résultats par courriel ?", "o": ["Oui, volontiers", "Non merci"]},
 "q8": {"q": "(Si oui) Votre courriel — utilisé uniquement pour ce que vous avez choisi ci-dessus :"},
}

# Text shown above the agree checkbox. Replace with your own study
# information and consent wording.
INTRO_EN = ("<b>Survey introduction.</b><br>"
 "Describe here what the survey is for, who runs it, how long it takes, "
 "and how the answers will be used and protected.")
INTRO_FR = ("<b>Présentation du sondage.</b><br>"
 "Décrivez ici l'objet du sondage, qui le mène, sa durée, "
 "et comment les réponses seront utilisées et protégées.")

# Interface strings.
UI = {
 "en": {"title": "Survey", "sub": "A few minutes · Anonymous",
   "agree": "I have read the above and agree to take part.", "submit": "Submit",
   "err": "Please answer this question.", "err_email": "Please enter a valid email address.",
   "note_ph": "Anything to add? (optional)", "done_h": "Thank you!",
   "done_p": "Your answers were recorded.",
   "dup": "This device has already submitted a response. Thank you!",
   "select": "Select…", "fail": "Something went wrong — please try again."},
 "fr": {"title": "Sondage", "sub": "Quelques minutes · Anonyme",
   "agree": "J'ai lu ce qui précède et j'accepte de participer.", "submit": "Envoyer",
   "err": "Veuillez répondre à cette question.", "err_email": "Veuillez entrer une adresse courriel valide.",
   "note_ph": "Quelque chose à ajouter ? (facultatif)", "done_h": "Merci !",
   "done_p": "Vos réponses ont été enregistrées.",
   "dup": "Cet appareil a déjà soumis une réponse. Merci !",
   "select": "Choisir…", "fail": "Une erreur est survenue — veuillez réessayer."},
}

# ---------------------------------------------------------------------------
# Survey page (single self-contained HTML document)
# ---------------------------------------------------------------------------
def survey_page() -> str:
    payload = json.dumps({"Q": Q, "FR": FR, "CEN": INTRO_EN, "CFR": INTRO_FR, "UI": UI})
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Survey</title>
<style>
:root{--bg:#f7f5f0;--card:#fff;--ink:#232a28;--sub:#68716d;--line:#e4e1d8;--acc:#2f6f5e;--acc2:#eaf3f0;--warn:#b3532f}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
.wrap{max-width:640px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:24px;margin:14px 0 6px}
.sub{color:var(--sub);font-size:14px;margin-bottom:10px}
.lang{display:flex;gap:8px;margin:6px 0 8px}
.lang button{padding:6px 14px;border:1px solid var(--line);background:#fff;border-radius:999px;font-size:13px;cursor:pointer}
.lang button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:12px 0}
.sec{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--acc);margin:22px 0 4px}
.q{font-size:16px;font-weight:600;margin-bottom:10px}
.qn{color:var(--acc);margin-right:6px}
.note{color:var(--sub);font-size:12.5px;margin-top:-6px;margin-bottom:8px}
label.opt{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin:6px 0;cursor:pointer;font-size:15px;background:#fff}
label.opt:has(input:checked){border-color:var(--acc);background:var(--acc2)}
input[type=radio],input[type=checkbox]{margin-top:3px;accent-color:var(--acc)}
select,textarea,input[type=text],input[type=email]{width:100%;border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;background:#fff}
textarea{min-height:90px}
.notebox{margin-top:8px}
.notebox input{font-size:14px;padding:8px 10px;background:#fbfaf7}
.scale{display:flex;gap:6px;justify-content:space-between;margin:8px 0 2px}
.scale button{flex:1;padding:10px 0;border:1px solid var(--line);border-radius:10px;background:#fff;font-size:15px;cursor:pointer}
.scale button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.scalelab{display:flex;justify-content:space-between;font-size:12px;color:var(--sub)}
.err{color:var(--warn);font-size:13px;display:none;margin-top:6px}
.bar{position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:5}
.prog{height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.prog i{display:block;height:100%;background:var(--acc);width:0%}
.btn{display:block;width:100%;border:none;background:var(--acc);color:#fff;border-radius:12px;padding:14px;font-size:16px;font-weight:600;cursor:pointer;margin-top:18px}
.done{text-align:center;padding:40px 10px}
.done h2{margin-bottom:10px}
.consent{font-size:14.5px}
.consent label{display:flex;gap:10px;margin-top:12px;font-weight:600;cursor:pointer}
.brand{text-align:center;color:var(--sub);font-size:12px;margin-top:26px}
</style></head><body><div class="wrap">
<h1 id="ttl"></h1><div class="sub" id="sb"></div>
<div class="lang"><button id="len">English</button><button id="lfr">Français</button></div>
<div class="bar"><div class="prog"><i id="pf"></i></div></div>
<div id="consent" class="card consent"></div>
<form id="f" style="display:none" onsubmit="return false"></form>
<div id="endmsg" class="card" style="display:none"></div>
<div id="done" class="done" style="display:none"><h2 id="dh"></h2><p id="dp"></p></div>
<div class="brand">__BRAND__</div>
<script>
const D=__PAYLOAD__;const Q=D.Q,FR=D.FR,UI=D.UI;
let L=localStorage.getItem('svt_lang')||'en';
const $=s=>document.querySelector(s);const A={};const N={};let started=false,agreed=false;
fetch('/api/view',{method:'POST'});
// Stable numbering: required questions get visible numbers, in order.
const NUM={};let _n=0;for(const it of Q){if(it.id&&it.req){_n++;NUM[it.id]=_n;}}
function T(item,field){ // localized field lookup with English fallback
  if(L==='fr'&&FR[item.id]&&FR[item.id][field]!==undefined)return FR[item.id][field];
  return item[field];}
function secT(name){return L==='fr'?(FR._secs[name]||name):name;}
function setLang(l){L=l;localStorage.setItem('svt_lang',l);paint();}
$('#len').addEventListener('click',()=>setLang('en'));
$('#lfr').addEventListener('click',()=>setLang('fr'));
function paint(){
  $('#len').className=L==='en'?'on':'';$('#lfr').className=L==='fr'?'on':'';
  $('#ttl').textContent=UI[L].title;$('#sb').textContent=UI[L].sub;
  $('#dh').textContent=UI[L].done_h;$('#dp').textContent=UI[L].done_p;
  const cs=$('#consent');
  if(!agreed){cs.style.display='block';cs.innerHTML=(L==='fr'?D.CFR:D.CEN)+
    '<label><input type="checkbox" id="agree"> '+UI[L].agree+'</label>';
    $('#agree').addEventListener('change',e=>{if(e.target.checked){agreed=true;
      if(!started){started=true;fetch('/api/start',{method:'POST'});}paint();}});}
  else{cs.style.display='none';$('#f').style.display='block';render();}
  prog();}
function shouldShow(item){
  if(item.show_if){const [qid,idx]=item.show_if;return A[qid]===idx;}
  if(item.show_if_any){const [qid,idxs]=item.show_if_any;return idxs.includes(A[qid]);}
  if(item.show_any){return item.show_any.some(([qid,idxs])=>idxs.includes(A[qid]));}
  return true;}
function renderTop(item,card){
  // follow-up "pick the ONE" panel, shown once 2+ checkboxes are ticked
  let tp=card.querySelector('.topwrap');
  const sel=A[item.id]||[];
  if(sel.length<2){if(tp)tp.remove();return;}
  if(!tp){tp=document.createElement('div');tp.className='topwrap';tp.style.cssText='margin-top:10px;padding:10px 12px;border:1px dashed var(--acc);border-radius:10px';card.appendChild(tp);}
  const opts=(L==='fr'&&FR[item.id]&&FR[item.id].o)||item.o;
  const topq=(L==='fr'&&FR[item.id]&&FR[item.id].top)||item.top;
  tp.innerHTML='<div style="font-weight:600;font-size:14px;margin-bottom:6px">'+topq+'</div>';
  sel.forEach(i=>{const l=document.createElement('label');l.className='opt';
    l.innerHTML='<input type="radio" name="'+item.id+'_top"> <span>'+opts[i]+'</span>';
    l.querySelector('input').checked=(A[item.id+'_top']===i);
    l.querySelector('input').addEventListener('change',()=>{A[item.id+'_top']=i;prog();});
    tp.appendChild(l);});}
function render(){
  const f=$('#f');f.innerHTML='';
  for(const item of Q){
    if(item.sec){const d=document.createElement('div');d.className='sec';d.textContent=secT(item.sec);f.appendChild(d);continue;}
    if(!shouldShow(item))continue;
    const c=document.createElement('div');c.className='card';c.id='c_'+item.id;
    const qtext=T(item,'q'),note=T(item,'note');
    c.innerHTML='<div class="q">'+(NUM[item.id]?('<span class="qn">Q'+NUM[item.id]+'.</span>'):'')+qtext+(item.req_if_shown?' <span style="color:#b00020">*</span>':'')+'</div>'+(note?'<div class="note">'+note+'</div>':'');
    const opts=T(item,'o')||item.o;
    if(item.t==='radio'){opts.forEach((o,i)=>{const l=document.createElement('label');l.className='opt';
      l.innerHTML='<input type="radio" name="'+item.id+'"> <span>'+o+'</span>';
      l.querySelector('input').checked=(A[item.id]===i);
      l.querySelector('input').addEventListener('change',()=>{A[item.id]=i;maybeEnd(item,i);render();prog();});c.appendChild(l);});}
    else if(item.t==='check'){const max=item.max||99;A[item.id]=A[item.id]||[];
      opts.forEach((o,i)=>{const l=document.createElement('label');l.className='opt';
      l.innerHTML='<input type="checkbox"> <span>'+o+'</span>';const inp=l.querySelector('input');
      inp.checked=A[item.id].includes(i);
      inp.addEventListener('change',()=>{const arr=A[item.id];
        if(inp.checked){if(arr.length>=max){inp.checked=false;return;}arr.push(i);}else{A[item.id]=arr.filter(x=>x!==i);
          if(A[item.id+'_top']===i)delete A[item.id+'_top'];}
        if(item.top)renderTop(item,c);prog();});
      c.appendChild(l);});
      if(item.top)renderTop(item,c);}
    else if(item.t==='scale'){const s=document.createElement('div');s.className='scale';
      for(let v=1;v<=5;v++){const b=document.createElement('button');b.type='button';b.textContent=v;
        if(A[item.id]===v)b.className='on';
        b.addEventListener('click',()=>{A[item.id]=v;render();prog();});s.appendChild(b);}
      c.appendChild(s);const lab=document.createElement('div');lab.className='scalelab';
      lab.innerHTML='<span>'+(T(item,'lo')||item.lo)+'</span><span>'+(T(item,'hi')||item.hi)+'</span>';c.appendChild(lab);}
    else if(item.t==='rank'){
      // tap-to-rank: tap assigns the next position, tap again to remove
      A[item.id]=A[item.id]||[];
      const order=A[item.id];
      opts.forEach((o,i)=>{const l=document.createElement('label');l.className='opt';l.style.cursor='pointer';
        const pos=order.indexOf(i);
        l.innerHTML='<span style="min-width:26px;display:inline-block;font-weight:700;color:var(--acc)">'+(pos>=0?(pos+1)+'.':'')+'</span><span>'+o+'</span>';
        l.addEventListener('click',()=>{const p=order.indexOf(i);
          if(p>=0){order.splice(p,1);}else{order.push(i);}render();prog();});
        c.appendChild(l);});
      const hint=document.createElement('div');hint.className='note';hint.style.marginTop='6px';
      hint.textContent=(L==='fr'?'Touchez dans l\\u2019ordre de priorité ; retouchez pour retirer. Classez les '+opts.length+'.':'Tap in order of priority; tap again to remove. Rank all '+opts.length+'.');
      c.appendChild(hint);}
    else if(item.t==='select'){const sel=document.createElement('select');
      sel.innerHTML='<option value="">'+UI[L].select+'</option>'+opts.map((o,i)=>'<option value="'+i+'"'+(A[item.id]===i?' selected':'')+'>'+o+'</option>').join('');
      sel.addEventListener('change',()=>{A[item.id]=sel.value===''?undefined:+sel.value;prog();});c.appendChild(sel);}
    else if(item.t==='text'){const t=document.createElement(item.email?'input':'textarea');
      if(item.email)t.type='email';t.value=A[item.id]||'';
      t.addEventListener('input',()=>{A[item.id]=t.value;prog();});c.appendChild(t);}
    // optional free-note box under every choice question
    if(item.t!=='text'){const nb=document.createElement('div');nb.className='notebox';
      var _ph=(L==='fr'&&FR[item.id]&&FR[item.id].note_ph)||item.note_ph||UI[L].note_ph;
      nb.innerHTML='<input type="text" placeholder="'+_ph+'">';
      const ni=nb.querySelector('input');ni.value=N[item.id]||'';
      ni.addEventListener('input',()=>{N[item.id]=ni.value;});c.appendChild(nb);}
    const e=document.createElement('div');e.className='err';e.id='e_'+item.id;e.textContent=item.email?UI[L].err_email:UI[L].err;c.appendChild(e);
    f.appendChild(c);
  }
  const btn=document.createElement('button');btn.className='btn';btn.type='button';btn.textContent=UI[L].submit;
  btn.addEventListener('click',submit);f.appendChild(btn);}
function maybeEnd(item,i){
  if(item.end_if&&item.end_if.includes(i)){$('#f').style.display='none';
    const em=$('#endmsg');em.style.display='block';em.textContent=T(item,'end_msg')||item.end_msg;}
  else{$('#endmsg').style.display='none';$('#f').style.display='block';}}
function answered(item){const v=A[item.id];
  if(item.t==='check'){if(!(Array.isArray(v)&&v.length>0))return false;
    if(item.top&&v.length>=2&&A[item.id+'_top']===undefined)return false;return true;}
  if(item.t==='rank')return Array.isArray(v)&&v.length===item.o.length;
  if(item.t==='text'){const tv=(A[item.id]||'').trim();
    if(item.req_if_shown&&!tv)return false;
    if(item.email&&tv&&!/^[^\\s@]+@[^\\s@]+\\.[^\\s@]{2,}$/.test(tv))return false;
    return true;}
  return v!==undefined;}
function visibleReq(){return Q.filter(i=>i.id&&shouldShow(i)&&(i.req||i.req_if_shown));}
function prog(){const vr=visibleReq();if(!vr.length)return;const done=vr.filter(answered).length;
  $('#pf').style.width=Math.round(done/vr.length*100)+'%';savePartial();}
let _ptimer=null;
function savePartial(){
  // debounce: persist a partial response at most every 1.5 s of quiet
  if(localStorage.getItem('svt_done'))return;
  clearTimeout(_ptimer);
  _ptimer=setTimeout(function(){
    const tok=localStorage.getItem('svt_tok')||(Math.random().toString(36).slice(2)+Date.now().toString(36));
    localStorage.setItem('svt_tok',tok);
    fetch('/api/partial',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tok:tok,lang:L,answers:A,notes:N})}).catch(()=>{});
  },1500);}
async function submit(){
  let ok=true;
  for(const item of visibleReq()){const e=$('#e_'+item.id);
    if(!answered(item)){ok=false;if(e)e.style.display='block';}
    else if(e)e.style.display='none';}
  if(!ok){const bad=document.querySelector('.err[style*="block"]');if(bad)bad.scrollIntoView({block:'center',behavior:'smooth'});return;}
  if(localStorage.getItem('svt_done')){alert(UI[L].dup);return;}
  const tok=localStorage.getItem('svt_tok')||(Math.random().toString(36).slice(2)+Date.now().toString(36));
  localStorage.setItem('svt_tok',tok);
  const r=await fetch('/api/submit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tok:tok,lang:L,answers:A,notes:N})});
  if(r.ok){localStorage.setItem('svt_done','1');$('#f').style.display='none';$('#done').style.display='block';window.scrollTo(0,0);}
  else{alert(UI[L].fail);}}
paint();
</script></div></body></html>""".replace("__PAYLOAD__", payload).replace("__BRAND__", BRAND)

# ---------------------------------------------------------------------------
# Admin: login with per-IP lockout, dashboard, CSV export
# ---------------------------------------------------------------------------
def ip_locked(ip):
    with db() as c:
        r = c.execute("SELECT until FROM lockout WHERE ip=?", (ip,)).fetchone()
    if r and r["until"] > time.time(): return int(r["until"] - time.time())
    return 0

def ip_fail(ip):
    now = int(time.time())
    with db() as c:
        r = c.execute("SELECT fails,last FROM lockout WHERE ip=?", (ip,)).fetchone()
        # consecutive-failure window: reset the count if the last failure is old
        fails = (r["fails"] if r and r["last"] > now - LOCK_SECS else 0) + 1
        until = now + LOCK_SECS if fails >= LOCK_FAILS else 0
        c.execute("INSERT OR REPLACE INTO lockout(ip,fails,until,last) VALUES(?,?,?,?)", (ip, fails, until, now))

def ip_clear(ip):
    with db() as c: c.execute("DELETE FROM lockout WHERE ip=?", (ip,))

def sess_ok(req: Request) -> bool:
    tok = req.cookies.get("svt_admin", "")
    if not tok: return False
    with db() as c:
        r = c.execute("SELECT ts FROM sessions WHERE tok=?", (tok,)).fetchone()
    return bool(r and r["ts"] > time.time() - 86400)

LOGIN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Survey Admin</title><style>body{font-family:sans-serif;background:#f7f5f0;display:flex;align-items:center;justify-content:center;height:100vh}
.c{background:#fff;border:1px solid #e4e1d8;border-radius:14px;padding:30px;width:min(340px,90vw);text-align:center}
input{width:100%;padding:11px;border:1px solid #e4e1d8;border-radius:10px;margin:12px 0;font-size:15px}
button{width:100%;padding:11px;border:none;background:#2f6f5e;color:#fff;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
.e{color:#b3532f;font-size:13px;min-height:18px}.b{color:#68716d;font-size:12px;margin-top:14px}</style></head><body><div class="c">
<h2>Survey Admin</h2><form method="post" action="__LOGIN__"><input type="password" name="pw" placeholder="Password" autofocus>
<button>Sign in</button></form><div class="e">__MSG__</div><div class="b">__BRAND__</div></div></body></html>"""

def login_page(msg="", status=200):
    prefix = "" if ADMIN_HOST else "/admin"
    return HTMLResponse(LOGIN.replace("__MSG__", msg).replace("__LOGIN__", prefix + "/login").replace("__BRAND__", BRAND), status_code=status)

@app.middleware("http")
async def router(request: Request, call_next):
    # Host-based routing when both hostnames are configured;
    # otherwise the admin area lives under /admin on the same host.
    h = host_of(request)
    if ADMIN_HOST and h == ADMIN_HOST:
        return await admin_router(request, request.url.path)
    if not ADMIN_HOST and request.url.path.startswith("/admin"):
        return await admin_router(request, request.url.path[len("/admin"):] or "/")
    return await call_next(request)

async def admin_router(request: Request, p: str):
    ip = client_ip(request)
    if request.method == "POST" and p == "/login":
        wait = ip_locked(ip)
        if wait: return login_page(f"Locked. Try again in {wait}s.", 429)
        form = await request.form(); pw = str(form.get("pw", ""))
        if secrets.compare_digest(pw, ADMIN_PASS):
            ip_clear(ip); tok = secrets.token_urlsafe(32)
            with db() as c: c.execute("INSERT INTO sessions(tok,ts) VALUES(?,?)", (tok, int(time.time())))
            r = RedirectResponse("/" if ADMIN_HOST else "/admin", status_code=303)
            r.set_cookie("svt_admin", tok, httponly=True, secure=True, samesite="lax", max_age=86400)
            return r
        ip_fail(ip)
        wait = ip_locked(ip)
        return login_page(f"Wrong password. Locked for {wait}s." if wait else "Wrong password.", 401)
    if not sess_ok(request):
        if ip_locked(ip): return login_page(f"Locked. Try again in {ip_locked(ip)}s.", 429)
        return login_page()
    if p == "/export.csv": return export_csv()
    return dashboard()

def dashboard():
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM responses WHERE status='complete'").fetchone()[0]
        npart = c.execute("SELECT COUNT(*) FROM responses WHERE status!='complete'").fetchone()[0]
        ctr = {r["k"]: r["n"] for r in c.execute("SELECT k,n FROM counters")}
        rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM responses WHERE status='complete'")]
        last = c.execute("SELECT ts FROM responses ORDER BY ts DESC LIMIT 1").fetchone()
        codes = [r["code"] for r in c.execute("SELECT code FROM responses ORDER BY ts DESC LIMIT 8") if r["code"]]
    lasts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last[0])) if last else "—"
    view, start, comp = ctr.get("view", 0), ctr.get("start", 0), ctr.get("complete", 0)
    pr = lambda a, b: f"{a/b*100:.0f}%" if b else "—"
    csv_href = ("/export.csv" if ADMIN_HOST else "/admin/export.csv")
    html = [f"""<div style="font-family:sans-serif;max-width:760px;margin:30px auto;padding:0 14px">
    <h2>Survey — Dashboard</h2>
    <p><b>{n}</b> complete · <b>{npart}</b> in progress (partial) · last {lasts}<br>
    Views {view} → Starts {start} ({pr(start,view)}) → Completes {comp} ({pr(comp,start)} of starts)</p>
    <p><a href="{csv_href}" style="background:#2f6f5e;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">Download full CSV</a></p><hr>"""]
    if codes: html.append("<p><b>Latest respondents:</b> " + " · ".join(codes) + "</p>")
    num = 0
    for item in Q:
        if not item.get("id"): continue
        num += 1
        if item["t"] in ("radio", "select", "check"):
            counts = [0] * len(item["o"])
            for d in rows:
                v = d.get(item["id"])
                if item["t"] == "check" and isinstance(v, list):
                    for i in v:
                        if isinstance(i, int) and i < len(counts): counts[i] += 1
                elif isinstance(v, int) and v < len(counts): counts[v] += 1
            html.append(f"<p><b>Q{num}.</b> {item['q']}<br>" + " · ".join(
                f"{o}: <b>{c}</b>" for o, c in zip(item["o"], counts)) + "</p>")
        elif item["t"] == "rank":
            # average position per option (1 = ranked first)
            sums = [0]*len(item["o"]); cnts = [0]*len(item["o"])
            for d in rows:
                v = d.get(item["id"])
                if isinstance(v, list):
                    for p, i in enumerate(v):
                        if isinstance(i, int) and i < len(sums): sums[i] += p+1; cnts[i] += 1
            parts = [f"{o}: avg {sums[i]/cnts[i]:.1f}" if cnts[i] else f"{o}: —" for i, o in enumerate(item["o"])]
            html.append(f"<p><b>Q{num}.</b> {item['q']}<br>" + " · ".join(parts) + "</p>")
        elif item["t"] == "scale":
            vals = [d.get(item["id"]) for d in rows if isinstance(d.get(item["id"]), int)]
            avg = f"{sum(vals)/len(vals):.2f}" if vals else "—"
            html.append(f"<p><b>Q{num}.</b> {item['q']}<br>avg <b>{avg}</b> (n={len(vals)})</p>")
        else:
            filled = sum(1 for d in rows if (d.get(item["id"]) or "").strip())
            html.append(f"<p><b>Q{num}.</b> {item['q']}<br>{filled} text answers</p>")
    html.append(f'<hr><p style="color:#68716d;font-size:12px">{BRAND}</p></div>')
    return HTMLResponse("".join(html))

def export_csv():
    """One row per respondent. Headers carry the full question text; answers are
    exported as full option text; multi-select joined with ' | '; ranks as
    'position:option'; per-question notes get their own columns."""
    ids = [q for q in Q if q.get("id")]
    num = {}; n = 0
    for q in ids:
        if q.get("req"): n += 1; num[q["id"]] = n
    def _h(q): return (f"Q{num[q['id']]}. " if q["id"] in num else "") + q["q"]
    heads = ["respondent_code", "status", "submitted_at", "language"]
    for q in ids:
        heads.append(_h(q))
        if q.get("top"): heads.append(_h(q) + " — top pick")
        if q["t"] != "text": heads.append(_h(q) + " — note")
    def esc(s): return '"' + str(s).replace('"', '""') + '"'
    lines = [",".join(esc(h) for h in heads)]
    with db() as c:
        rows = list(c.execute("SELECT ts,lang,data,code,status FROM responses ORDER BY ts"))
    for r in rows:
        d = json.loads(r["data"]); notes = d.pop("_notes", {})
        cells = [r["code"] or "", r["status"] or "complete",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])), r["lang"] or "en"]
        for q in ids:
            v = d.get(q["id"])
            if q["t"] == "check" and isinstance(v, list):
                cells.append(" | ".join(q["o"][i] for i in v if isinstance(i, int) and i < len(q["o"])))
            elif q["t"] == "rank" and isinstance(v, list):
                cells.append(" | ".join(f"{p+1}:{q['o'][i]}" for p, i in enumerate(v) if isinstance(i, int) and i < len(q["o"])))
            elif q["t"] in ("radio", "select") and isinstance(v, int) and v < len(q["o"]):
                cells.append(q["o"][v])
            elif q["t"] == "scale" and isinstance(v, int):
                cells.append(str(v))
            elif q["t"] == "text":
                cells.append(v or "")
            else:
                cells.append("")
            if q.get("top"):
                tv = d.get(q["id"] + "_top")
                cells.append(q["o"][tv] if isinstance(tv, int) and tv < len(q["o"]) else "")
            if q["t"] != "text": cells.append(notes.get(q["id"], ""))
        lines.append(",".join(esc(x) for x in cells))
    body = "﻿" + "\n".join(lines)  # BOM so spreadsheet apps detect UTF-8
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=survey_export.csv"})

# ---------------------------------------------------------------------------
# Public survey routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    bump("view")
    return HTMLResponse(survey_page(), headers={"Cache-Control": "no-store, max-age=0"})

@app.post("/api/view")
def apiview(): return {"ok": True}

@app.post("/api/start")
def apistart(): bump("start"); return {"ok": True}

def _respondent_code(region: str, rid: int) -> str:
    # date-time + region + row id: anonymous but easy to reference in support
    return time.strftime("%Y%m%d-%H%M") + f"-{region}-{rid}"

def _region(req: Request) -> str:
    # CDN country header when present; harmless "??" otherwise
    return (req.headers.get("cf-ipcountry") or "??").upper()

@app.post("/api/partial")
async def partial(req: Request):
    d = await req.json()
    tok = str(d.get("tok", ""))[:64]; ans = d.get("answers", {}); notes = d.get("notes", {}) or {}
    lang = "fr" if d.get("lang") == "fr" else "en"
    if not tok or not isinstance(ans, dict): return JSONResponse({"ok": False}, status_code=400)
    ans["_notes"] = {k: str(v)[:1000] for k, v in notes.items() if str(v).strip()}
    with db() as c:
        row = c.execute("SELECT id,status FROM responses WHERE tok=?", (tok,)).fetchone()
        if row:
            if row["status"] == "complete": return {"ok": True}  # never downgrade a final answer
            c.execute("UPDATE responses SET ts=?,lang=?,data=? WHERE tok=?",
                      (int(time.time()), lang, json.dumps(ans, ensure_ascii=False), tok))
        else:
            cur = c.execute("INSERT INTO responses(ts,tok,lang,data,code,status) VALUES(?,?,?,?,?,'partial')",
                            (int(time.time()), tok, lang, json.dumps(ans, ensure_ascii=False), ""))
            rid = cur.lastrowid
            c.execute("UPDATE responses SET code=? WHERE id=?", (_respondent_code(_region(req), rid), rid))
    return {"ok": True}

@app.post("/api/submit")
async def submit(req: Request):
    d = await req.json()
    tok = str(d.get("tok", ""))[:64]; ans = d.get("answers", {}); notes = d.get("notes", {}) or {}
    lang = "fr" if d.get("lang") == "fr" else "en"
    if not tok or not isinstance(ans, dict): return JSONResponse({"ok": False}, status_code=400)
    ans["_notes"] = {k: str(v)[:1000] for k, v in notes.items() if str(v).strip()}
    with db() as c:
        row = c.execute("SELECT id,status FROM responses WHERE tok=?", (tok,)).fetchone()
        if row:
            if row["status"] != "complete":
                c.execute("UPDATE responses SET ts=?,lang=?,data=?,status='complete' WHERE tok=?",
                          (int(time.time()), lang, json.dumps(ans, ensure_ascii=False), tok))
                # counter updated inside the same transaction: one writer, no lock contention
                c.execute("UPDATE counters SET n=n+1 WHERE k='complete'")
        else:
            cur = c.execute("INSERT INTO responses(ts,tok,lang,data,code,status) VALUES(?,?,?,?,?,'complete')",
                            (int(time.time()), tok, lang, json.dumps(ans, ensure_ascii=False), ""))
            rid = cur.lastrowid
            c.execute("UPDATE responses SET code=? WHERE id=?", (_respondent_code(_region(req), rid), rid))
            c.execute("UPDATE counters SET n=n+1 WHERE k='complete'")
    return {"ok": True}

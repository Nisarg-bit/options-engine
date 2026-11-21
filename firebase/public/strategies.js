/* Stock Futures: Strategy Library, Builder (visual + advanced), Research & Suggestions.
   Rendered inside the Stock Futures section by futures.js (views 'lib' and 'res').
   Reads the worker snapshot's `research` block on the read-only site, or the API
   (/api/futures/research/overview, ...) on the authenticated dashboard. Every
   mutation (save, shadow, approve, dismiss, Research now) exists ONLY on the
   authenticated dashboard and is enforced server-side. Paper trading only. */
(function(){
'use strict';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fin=v=>v!=null&&isFinite(v);
const rs=v=>fin(v)?(v<0?'−':'')+'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN'):'—';
const srs=v=>fin(v)?(v>0?'+':'')+rs(v):'—';
const cls=v=>!fin(v)||v===0?'':v>0?'up':'down';
const badge=(t,k)=>`<span class="sf-b sf-${k||'n'}">${esc(t)}</span>`;
const dt=s=>s?String(s).slice(0,10)+' '+String(s).slice(11,16):'—';
const empty=t=>`<div class="sf-empty">${esc(t)}</div>`;
const LIFE={DRAFT:'n',SHADOW:'info',ACTIVE:'ok',DISABLED:'warn',ARCHIVED:'n'};
const EVID={INSUFFICIENT:'bad',EARLY:'warn',EVALUATED:'ok'};
const api=()=>!(typeof SRC!=='undefined'&&SRC.mode==='firebase')&&typeof fetch==='function';
const KTOK=new URLSearchParams(location.search).get('k');   // same token support as the rest of the page
async function call(method,path,body){
  const h={'content-type':'application/json'}; if(KTOK) h['x-dash-token']=KTOK;
  const r=await fetch(path,{method,headers:h,body:body?JSON.stringify(body):undefined});
  let d={}; try{ d=await r.json(); }catch(e){}
  if(!r.ok||d.ok===false) throw new Error(d.error||d.detail||('HTTP '+r.status));
  return d.result??d;
}
let OV=null, OVAT=0, IND=null, MSG='', OPEN=null, B=null, BSID=null, BVAL=null, PREV=null, ADV=false, SEL={};

async function overview(s,force){
  if(!api()) return s?.research||null;
  if(!force&&OV&&Date.now()-OVAT<8000) return OV;
  try{ OV=await call('GET','/api/futures/research/overview'); OVAT=Date.now(); }catch(e){ OV={available:false,reason:e.message}; }
  return OV;
}
function msgBar(){ return MSG?`<div class="sf-banner">${esc(MSG)}</div>`:''; }

/* ------------------------------------------------------------ metrics table */
function mrows(m,keys){
  if(!m) return '<tr><td colspan="8">—</td></tr>';
  return (keys||['intraday:LONG','intraday:SHORT','swing:LONG','swing:SHORT','portfolio']).filter(k=>m[k]&&(m[k].settled||k==='portfolio'))
    .map(k=>{const x=m[k],u=x.undefined||{};const c=(key,f)=>x[key]==null?`<td title="${esc(u[key]||'')}">—</td>`:`<td>${f(x[key])}</td>`;
      return `<tr><th>${esc(k)}</th><td>${x.settled}</td><td class="${cls(x.net_pnl)}">${srs(x.net_pnl)}</td><td>${rs(x.costs)}</td>${c('expectancy',srs)}${c('win_rate',v=>Math.round(v*100)+'%')}${c('profit_factor',v=>v.toFixed(2))}<td>${rs(x.max_drawdown)}</td>${c('ci80',v=>srs(v[0])+' … '+srs(v[1]))}</tr>`;}).join('');
}
const mhead='<tr><th></th><th>Trades</th><th>Net</th><th>Costs</th><th>Expectancy</th><th>Win</th><th>PF</th><th>Max DD</th><th>80% interval/trade</th></tr>';
const mtable=(m,keys)=>`<div class="sf-scroll"><table class="sf-t sf-narrow"><thead>${mhead}</thead><tbody>${mrows(m,keys)}</tbody></table></div>`;

/* ------------------------------------------------------------ research view */
function researchView(o){
  if(!o||o.available===false) return empty(o?.reason||'Research data appears once the research agent has run (needs the library on the server).');
  const P=o.providers||{}, cad=o.cadence||{}, pol=o.policy||{};
  const jobs=(o.jobs||[]).slice(0,6);
  const running=jobs.find(j=>j.status==='RUNNING'||j.status==='QUEUED');
  const prov=k=>{const p=P[k]||{};return `${badge(p.provider||'none',p.fixture?'warn':p.configured?'ok':'bad')} <span class="sf-why">${esc(p.detail||'')}</span>`;};
  const sugg=(o.suggestions||[]);
  const open=sugg.filter(s=>['OPEN','CONTINUE'].includes(s.status));
  return msgBar()+`<div class="sf-grid sf-g3">
   <div class="sf-card"><h3>Research agent ${running?badge(running.status,'warn'):badge('idle','n')}</h3>
    <table class="sf-kv"><tr><th>Search</th><td>${prov('search')}</td></tr><tr><th>Rule extraction</th><td>${prov('llm')}</td></tr>
    <tr><th>Cadence</th><td>every ${esc(pol.cadence?.every_sessions)} completed sessions (XBOM)</td></tr>
    <tr><th>Next cycle</th><td>${cad.next_cycle_in_sessions!=null?`in ${cad.next_cycle_in_sessions} session(s)`:'after the next session'} · last processed ${esc(cad.last_processed_session||'—')}</td></tr>
    <tr><th>Last cycle</th><td>${esc(o.last_cycle?.cycle||'—')} ${dt(o.last_cycle?.finished)}</td></tr></table>
    ${api()?`<button class="sf-btn" data-rnow ${running?'disabled title="a research job is already queued or running"':''}>Research now</button>`:'<p class="sf-note">Research now and approvals are on the authenticated dashboard.</p>'}
    ${running?`<p class="sf-note"><b>${esc(running.id)}</b> ${esc(running.status)} · ${esc(running.progress?.step||'')}</p>`:''}</div>
   <div class="sf-card"><h3>Locks and permissions</h3>
    <table class="sf-kv">${Object.entries(pol.locks||{}).map(([k,v])=>`<tr><th>${esc(k)} gate</th><td>${v?badge('LOCKED','warn'):badge('open','ok')} <span class="sf-why">${esc((pol.lock_notes||{})[k]||'')}</span></td></tr>`).join('')}
    <tr><th>Open to research</th><td>${esc((pol.open_areas||[]).join(', '))}</td></tr>
    <tr><th>Hard constraints</th><td>${esc(pol.hard_constraints?.lots_per_entry)} lot per entry; ${esc(pol.hard_constraints?.shared_limits_from||'')}</td></tr>
    <tr><th>Activation</th><td>${esc(pol.permissions?.activation||'')}</td></tr></table></div>
   <div class="sf-card"><h3>Active by profile</h3>
    ${(o.active||[]).length?`<table class="sf-kv">${(o.active||[]).map(a=>`<tr><th>${esc(a.mode)} #${esc(a.priority)}</th><td>${esc(a.name)} ${a.legacy?badge('deployed v1','n'):badge(a.version_id,'info')}</td></tr>`).join('')}</table>`:empty('Nothing active.')}
    <p class="sf-note">Priority decides which strategy reserves first when two signal at once. Shared limits apply to all.</p></div></div>
  <div class="sf-card"><h3>Suggestions ${badge(open.length+' open','info')}</h3>
   ${sugg.length?sugg.map(suggestionCard).join(''):empty('No suggestions yet. They appear after a research cycle, or when you request activation of a strategy.')}</div>
  <div class="sf-card"><h3>Experiment comparison</h3>${comparison(sugg)}</div>
  <div class="sf-grid sf-g2">
   <div class="sf-card"><h3>Approvals and activations</h3>
    ${(o.activations||[]).length?`<table class="sf-t sf-narrow"><thead><tr><th>When</th><th>Action</th><th>Profile</th><th>Version</th><th>By</th></tr></thead><tbody>${o.activations.map(a=>`<tr><td>${dt(a.at)}</td><td>${esc(a.action)}</td><td>${esc(a.mode)}</td><td>${esc(a.version_id)}</td><td>${esc(a.actor)}</td></tr>`).join('')}</tbody></table>`:empty('No activations yet: everything running was deployed as v1.')}
    ${(o.approvals||[]).filter(a=>!a.used_at).length?`<p class="sf-note">${(o.approvals||[]).filter(a=>!a.used_at).length} approval(s) not used (refused at activation).</p>`:''}</div>
   <div class="sf-card"><h3>Jobs and events</h3>
    <table class="sf-t sf-narrow"><tbody>${jobs.map(j=>`<tr><td>${dt(j.created)}</td><td>${esc(j.kind)}</td><td>${badge(j.status,j.status==='DONE'?'ok':j.status==='FAILED'?'bad':'warn')}</td><td class="sf-why">${esc(j.error?j.error.split('\n')[0]:j.progress?.step||'')}</td></tr>`).join('')}</tbody></table>
    <ul class="sf-req">${(o.events||[]).slice(0,12).map(e=>`<li><span class="sf-tm">${dt(e.at)}</span> ${esc(e.type)} <span class="sf-why">${esc(e.payload?.title||e.payload?.name||e.payload?.reason||e.payload?.status||'')}</span></li>`).join('')}</ul></div></div>`;
}
function suggestionCard(s){
  const e=s.evidence||{}, pv=e.provenance||{}, lc=s.lock_changes||[], fixture=pv.fixture;
  const rec=e.recent||{}, rep=e.replay||{}, fw=e.forward||{};
  const decided=['REPLACED','ALONGSIDE','DISMISSED'].includes(s.status);
  return `<details class="sf-sg" ${OPEN===s.id?'open':''} data-sg="${esc(s.id)}"><summary><b>${esc(s.title)}</b> ${badge(s.kind,'n')} ${badge(e.label||'?',EVID[e.label]||'n')} ${badge(s.status,s.status==='OPEN'?'info':'n')} ${fixture?badge('FIXTURE','warn'):''} ${lc.length?badge('needs lock change','bad'):''}</summary>
   ${e.description?`<pre class="sf-pre">${esc(e.description)}</pre>`:''}
   ${(e.diff||[]).length?`<h4>Differences from the active version</h4><ul class="sf-req">${e.diff.slice(0,20).map(d=>`<li><span class="sf-tm">${esc(d.path)}</span> ${esc(JSON.stringify(d.from))} → <b>${esc(JSON.stringify(d.to))}</b></li>`).join('')}</ul>`:''}
   ${lc.length?`<h4>Required lock changes</h4><ul class="sf-req">${lc.map(l=>`<li>${badge(l.lock,'bad')} ${esc(l.requested||((l.paths||[]).map(p=>p.path+': '+JSON.stringify(p.from)+' → '+JSON.stringify(p.to)).join('; ')))} ${api()&&!decided?`<label><input type="checkbox" data-lock="${esc(l.lock)}" data-for="${esc(s.id)}"> approve this lock change</label>`:''}</li>`).join('')}</ul>`:''}
   ${e.proposal_only?`<p class="sf-note"><b>Research proposal only:</b> it cannot run until the locked gate it needs exists and you approve the lock change. ${esc((e.errors||[]).join('; '))}</p>`:''}
   <div class="sf-grid sf-g2">
    <div><h4>Latest ${esc((rec.sessions||[]).length)} sessions (${esc((rec.sessions||[]).join(', ')||'none')}) · historical replay</h4>${mtable(rec.metrics)}
      <p class="sf-sub">Active strategy on the same sessions:</p>${mtable(rec.baseline,['portfolio'])}</div>
    <div><h4>All available history · ${esc((rep.sessions||[]).length)} sessions</h4>${mtable(rep.metrics)}
      <p class="sf-sub">+1 tick slippage per side: net ${srs(rep.stressed?.portfolio?.net_pnl)} · status ${esc(rep.status||'not run')}${rep.error?' · '+esc(rep.error):''}</p>
      <h4>Forward shadow paper</h4><p class="sf-sub">${fw.sessions||0} sessions, ${fw.settled||0} settled trades since ${dt(fw.since||fw.frozen_at)} (frozen version; replay results never count as forward)</p>
      ${fw.metrics?mtable(fw.metrics,['portfolio']):''}</div></div>
   <h4>Sources and data</h4><ul class="sf-req">
    <li>${esc(pv.kind||'?')} · ${esc(pv.method||pv.via||'')} ${pv.source_url?`· <a href="${esc(pv.source_url)}" target="_blank" rel="noopener noreferrer">${esc(pv.source_url)}</a>`:''} ${pv.publisher?'· '+esc(pv.publisher):''} ${pv.author?'· '+esc(pv.author):''} ${pv.published?'· published '+esc(pv.published):''} ${pv.retrieved?'· retrieved '+dt(pv.retrieved):''}</li>
    ${(pv.unverified_claims||[]).length?`<li>Unverified claims in the source (not results): ${esc(pv.unverified_claims.join('; '))}</li>`:''}
    ${e.data?`<li>Data needed: ${Object.entries(e.data).map(([k,v])=>`${esc(k)} ${v?'✓':'✗'}`).join(' · ')}</li>`:''}
    ${(e.notes||[]).map(n=>`<li>${esc(n)}</li>`).join('')}
    ${(e.limitations||[]).map(n=>`<li>⚠ ${esc(n)}</li>`).join('')}
    ${e.learned_components?`<li>${esc(e.learned_components)}</li>`:''}
    <li class="sf-tm">replay ${esc(rep.registry||'—')} · config ${esc(rep.config_hash||'—')} · data ${esc(rep.data?.data_sha256||'—')} · code ${esc(rep.code?.code_sha256||'—')} · evaluated ${dt(rep.evaluated_at)}</li></ul>
   ${api()&&!decided&&!e.proposal_only?`<div class="sf-act">
     <select data-mode-for="${esc(s.id)}"><option value="">profile: strategy's own</option><option>intraday</option><option>swing</option></select>
     <button class="sf-btn" data-dec="REPLACE" data-id="${esc(s.id)}" data-h="${esc(s.evidence_hash)}">Replace current</button>
     <button class="sf-btn" data-dec="ALONGSIDE" data-id="${esc(s.id)}" data-h="${esc(s.evidence_hash)}">Run alongside</button>
     <button class="sf-btn" data-dec="CONTINUE" data-id="${esc(s.id)}">Continue testing</button>
     <button class="sf-btn" data-dec="DISMISS" data-id="${esc(s.id)}">Dismiss</button>
     <button class="sf-btn" data-dec="EDIT_COPY" data-id="${esc(s.id)}">Edit a copy</button>
     <p class="sf-note">Replace / Run alongside activate exactly version ${esc(s.version_id)} with the evidence shown, for future entries only. Open positions keep their own plan. Evidence: ${esc(e.label||'?')}.</p></div>`:''}
   ${api()&&e.proposal_only?`<div class="sf-act"><button class="sf-btn" data-dec="DISMISS" data-id="${esc(s.id)}">Dismiss</button><button class="sf-btn" data-dec="EDIT_COPY" data-id="${esc(s.id)}">Edit a copy</button></div>`:''}
  </details>`;
}
function comparison(sugg){
  const rows=sugg.filter(s=>s.evidence?.replay?.metrics);
  if(!rows.length) return empty('Nothing evaluated yet.');
  const p=x=>x?.portfolio||{};
  return `<div class="sf-scroll"><table class="sf-t"><thead><tr><th>Candidate</th><th>Evidence</th><th>Sessions</th><th>Trades</th><th>Net</th><th>Expectancy</th><th>PF</th><th>Max DD</th><th>Stressed net</th><th>Active (same sessions)</th><th>Forward</th></tr></thead><tbody>
   ${rows.map(s=>{const e=s.evidence,r=p(e.replay.metrics),b=p(e.baseline?.metrics);return `<tr><td>${esc(s.title)}</td><td>${badge(e.label,EVID[e.label])}</td><td>${(e.replay.sessions||[]).length}</td><td>${r.settled??'—'}</td><td class="${cls(r.net_pnl)}">${srs(r.net_pnl)}</td><td>${srs(r.expectancy)}</td><td>${fin(r.profit_factor)?r.profit_factor.toFixed(2):'—'}</td><td>${rs(r.max_drawdown)}</td><td>${srs(p(e.replay.stressed).net_pnl)}</td><td class="${cls(b.net_pnl)}">${srs(b.net_pnl)} (${b.settled??0})</td><td>${e.forward?.sessions||0} s · ${e.forward?.settled||0} t</td></tr>`;}).join('')}
  </tbody></table></div><p class="sf-note">Same costs, fills and data latency for every row. Historical replay is in-sample while fewer than 15 complete sessions exist; forward shadow results are separate and only count after a version is frozen.</p>`;
}

/* ------------------------------------------------------------ library view */
function libraryView(o){
  if(B) return builderView();
  if(!o||o.available===false) return empty(o?.reason||'The strategy library appears once it is set up on the server.');
  const st=o.strategies||[];
  const groups=['ACTIVE','SHADOW','DRAFT','DISABLED','ARCHIVED'];
  return msgBar()+`<div class="sf-card"><h3>Strategy library ${badge(st.length,'n')}<span style="flex:1"></span>${api()?'<button class="sf-btn" data-new="intraday">New intraday</button> <button class="sf-btn" data-new="swing">New swing</button>':''}</h3>
   <p class="sf-sub">Lifecycle (what it may do) and evidence (how much we know) are separate. Edits create new versions; an edit of an active strategy becomes a draft copy until you approve it.</p>
   ${groups.map(g=>{const xs=st.filter(s=>s.lifecycle===g);return xs.length?`<h4>${g} ${badge(xs.length,LIFE[g])}</h4><div class="sf-scroll"><table class="sf-t sf-narrow"><tbody>${xs.map(s=>`<tr class="sf-click" data-open="${esc(s.id)}"><td><b>${esc(s.name)}</b><div class="sf-tm">${esc(s.id)} · ${esc(s.head_version)}</div></td><td>${esc(s.mode)}</td><td>${badge(s.evidence,EVID[s.evidence])}</td><td>${esc((s.provenance||{}).kind||'')}${(s.provenance||{}).fixture?' '+badge('FIXTURE','warn'):''}</td><td>${dt(s.updated)}</td></tr>${SEL.detail&&SEL.detail.strategy?.id===s.id?`<tr><td colspan="5">${detailView(SEL.detail)}</td></tr>`:''}`).join('')}</tbody></table></div>`:'';}).join('')}</div>`;
}
function detailView(d){
  const s=d.strategy, v=d.validation||{};
  return `<div class="sf-detail"><pre class="sf-pre">${esc(d.description)}</pre>
   ${v.errors?.length?`<p class="sf-note"><b>Not valid:</b> ${esc(v.errors.join('; '))}</p>`:''}${v.warnings?.length?`<p class="sf-note">${esc(v.warnings.join('; '))}</p>`:''}
   ${(v.lock_changes||[]).length?`<p class="sf-note">${badge('lock change','bad')} differs from the active strategy in locked area(s): ${esc(v.lock_changes.map(l=>l.lock).join(', '))}</p>`:''}
   <h4>Versions</h4><ul class="sf-req">${(d.versions||[]).map((x,i,a)=>`<li>v${x.n} <span class="sf-tm">${esc(x.version_id)}</span> ${dt(x.created)} ${esc(x.actor)} · ${esc(x.note||'')} ${i?`<button class="sf-btn sf-x" data-vdiff="${esc(a[i-1].version_id)}|${esc(x.version_id)}">diff</button>`:''} ${api()&&x.version_id!==s.head_version?`<button class="sf-btn sf-x" data-restore="${esc(x.version_id)}" data-sid="${esc(s.id)}">restore as new</button>`:''}</li>`).join('')}</ul>
   ${SEL.diff?`<ul class="sf-req">${SEL.diff.map(x=>`<li><span class="sf-tm">${esc(x.path)}</span> ${esc(JSON.stringify(x.from))} → <b>${esc(JSON.stringify(x.to))}</b></li>`).join('')||'<li>identical</li>'}</ul>`:''}
   ${api()?`<div class="sf-act"><button class="sf-btn" data-edit="${esc(s.id)}">${s.lifecycle==='ACTIVE'?'Edit a draft copy':'Edit'}</button>
     <button class="sf-btn" data-dup="${esc(s.id)}">Duplicate</button>
     <button class="sf-btn" data-replay="${esc(s.id)}">Replay</button>
     ${['DRAFT','DISABLED'].includes(s.lifecycle)?`<button class="sf-btn" data-shadow="${esc(s.id)}">Shadow test</button>`:''}
     ${s.lifecycle!=='ACTIVE'&&s.lifecycle!=='ARCHIVED'?`<button class="sf-btn" data-reqact="${esc(s.id)}">Request activation</button>`:''}
     ${['ACTIVE','SHADOW'].includes(s.lifecycle)?`<button class="sf-btn" data-life="DISABLED" data-sid="${esc(s.id)}">Disable</button>`:''}
     ${s.lifecycle!=='ARCHIVED'?`<button class="sf-btn" data-life="ARCHIVED" data-sid="${esc(s.id)}">Archive</button>`:`<button class="sf-btn" data-life="DRAFT" data-sid="${esc(s.id)}">Restore</button>`}</div>
     <p class="sf-note">Disable/archive stop new entries only; open positions keep their plan and every record stays.</p>`:''}</div>`;
}

/* ------------------------------------------------------------ builder */
const OPS=['>','>=','<','<=','cross_above','cross_below'];
function indSelect(path,val){
  const groups={};(IND?.indicators||[]).forEach(i=>(groups[i.group]??=[]).push(i));
  return `<select data-bp="${path}">${Object.entries(groups).map(([g,xs])=>`<optgroup label="${esc(g)}">${xs.map(i=>`<option value="${i.id}"${i.id===val?' selected':''}>${esc(i.label)}${i.intraday_only?' (intraday)':''}</option>`).join('')}</optgroup>`).join('')}</select>`;
}
function paramInputs(path,op){
  const spec=(IND?.indicators||[]).find(i=>i.id===op.ind)?.params||{};
  return Object.entries(spec).map(([k,s])=>{const v=(op.params||{})[k]??s.default;
    return s.type==='choice'?`<label class="sf-p">${esc(k)} <select data-bp="${path}.params.${k}">${s.choices.map(c=>`<option${c===v?' selected':''}>${esc(c)}</option>`).join('')}</select></label>`
      :`<label class="sf-p">${esc(k)} <input data-bp="${path}.params.${k}" data-num="${s.type!=='time'}" value="${esc(v)}" size="5"></label>`;}).join('');
}
function operand(path,o){
  if(o&&'value' in o) return `<span class="sf-opd"><button class="sf-btn sf-x" data-tog="${path}" title="compare with an indicator">#</button><input data-bp="${path}.value" data-num="true" value="${esc(o.value)}" size="9"></span>`;
  return `<span class="sf-opd"><button class="sf-btn sf-x" data-tog="${path}" title="compare with a number">ƒ</button>${indSelect(path+'.ind',o?.ind)} ${paramInputs(path,o||{})}<label class="sf-p">bars ago <input data-bp="${path}.offset" data-num="true" value="${esc(o?.offset||0)}" size="2"></label></span>`;
}
function nodeEd(path,n,depth){
  if(n.all||n.any){const k=n.all?'all':'any';
    return `<div class="sf-grp" style="margin-left:${depth?12:0}px"><div class="sf-grph"><select data-gk="${path}"><option value="all"${k==='all'?' selected':''}>ALL of</option><option value="any"${k==='any'?' selected':''}>ANY of</option></select>
      <button class="sf-btn sf-x" data-addc="${path}.${k}">+ condition</button><button class="sf-btn sf-x" data-addg="${path}.${k}">+ group</button>${depth?`<button class="sf-btn sf-x" data-del="${path}">remove group</button>`:''}</div>
      ${n[k].map((ch,i)=>nodeEd(`${path}.${k}.${i}`,ch,depth+1)).join('')}</div>`;}
  return `<div class="sf-cond">${operand(path+'.left',n.left)} <select data-bp="${path}.op">${OPS.map(o=>`<option${o===n.op?' selected':''}>${o}</option>`).join('')}</select> ${operand(path+'.right',n.right)} <button class="sf-btn sf-x" data-del="${path}">✕</button></div>`;
}
function getp(o,path){return path.split('.').reduce((a,k)=>a==null?a:a[/^\d+$/.test(k)?+k:k],o);}
function setp(o,path,v){const ks=path.split('.');let a=o;ks.slice(0,-1).forEach(k=>{k=/^\d+$/.test(k)?+k:k;if(a[k]==null)a[k]={};a=a[k];});a[ks.at(-1)]=v;}
function delp(o,path){const ks=path.split('.');const last=ks.pop();const a=getp(o,ks.join('.'));if(Array.isArray(a))a.splice(+last,1);else delete a[last];}
function builderView(){
  if(!api()) return empty('The builder needs the authenticated dashboard.');
  if(!IND) return empty('Loading the indicator registry…');
  const v=BVAL?.validation, ex=B.exits||{};
  const field=(label,path,val,num,size)=>`<label class="sf-p">${label} <input data-bp="${path}" ${num?'data-num="true"':''} value="${esc(val??'')}" size="${size||6}"></label>`;
  return msgBar()+`<div class="sf-card"><h3>Strategy builder ${BSID?badge('editing '+BSID,'info'):badge('new','n')}<span style="flex:1"></span>
     <button class="sf-btn" data-badv>${ADV?'Visual editor':'Advanced (JSON)'}</button> <button class="sf-btn" data-bclose>Close</button></h3>
   ${ADV?`<textarea class="sf-json" id="sf-adv" spellcheck="false">${esc(JSON.stringify(B,null,2))}</textarea>
     <div class="sf-act"><button class="sf-btn" data-advapply>Apply JSON</button> <button class="sf-btn" data-export>Export</button> <label class="sf-btn">Import <input type="file" accept=".json" data-import hidden></label></div>
     <p class="sf-note">Same schema as the visual editor (sf-strategy/1). Fields the visual editor does not show are kept unchanged. No code is executed: definitions are data validated on the server.</p>`
   :`<div class="sf-bld">
     ${field('Name','name',B.name,false,30)} ${field('Tags (comma)','tags',(B.tags||[]).join(','),false,20)}
     <label class="sf-p">Mode <select data-bp="mode"><option${B.mode==='intraday'?' selected':''}>intraday</option><option${B.mode==='swing'?' selected':''}>swing</option></select></label>
     <label class="sf-p">Timeframe <select data-bp="timeframe">${(IND.timeframes||[]).map(t=>`<option${t===B.timeframe?' selected':''}>${t}</option>`).join('')}</select></label>
     <label class="sf-p"><input type="checkbox" data-dir="LONG" ${B.directions.includes('LONG')?'checked':''}> LONG</label><label class="sf-p"><input type="checkbox" data-dir="SHORT" ${B.directions.includes('SHORT')?'checked':''}> SHORT</label>
     <label class="sf-p">Description <input data-bp="description" value="${esc(B.description||'')}" size="60"></label>
     <p class="sf-sub">Universe: F&amp;O stocks in qualifying sectors. Market and sector gates are LOCKED (edit them only in Advanced, which makes this a lock-change proposal).</p>
     ${['LONG','SHORT'].filter(d=>B.directions.includes(d)).map(d=>`<h4>${d} entry</h4>${nodeEd('entry.'+d,B.entry[d]||{all:[]},0)}
       <div class="sf-cond">Rank by ${operand('ranking.'+d,B.ranking?.[d]||{ind:'rel_strength'})} <select data-bp="ranking.${d}.order"><option value="desc"${B.ranking?.[d]?.order==='desc'?' selected':''}>highest first</option><option value="asc"${B.ranking?.[d]?.order==='asc'?' selected':''}>lowest first</option></select></div>`).join('')}
     <h4>Exits and timing</h4>
     <label class="sf-p">Stop <select data-bp="exits.stop.basis">${(IND.stop_bases||[]).map(x=>`<option${x===ex.stop?.basis?' selected':''}>${x}</option>`).join('')}</select></label>${ex.stop?.basis!=='opening_range_edge'?field('value','exits.stop.value',ex.stop?.value,true,4):''}
     <label class="sf-p">Target <select data-bp="exits.target.basis">${(IND.target_bases||[]).map(x=>`<option${x===ex.target?.basis?' selected':''}>${x}</option>`).join('')}</select></label>${field('value','exits.target.value',ex.target?.value,true,4)}
     ${field('Trailing %','exits.trailing.value',ex.trailing?.value??'',true,4)} ${field('Hard stop cap %','exits.hard_max_stop_pct',ex.hard_max_stop_pct,true,4)}
     ${B.mode==='intraday'?field('Flat by','exits.time_exit',ex.time_exit,false,5):field('Max holding sessions','exits.max_holding_sessions',ex.max_holding_sessions,true,3)+field('Pre-expiry buffer sessions','exits.pre_expiry_buffer_sessions',ex.pre_expiry_buffer_sessions??1,true,3)}
     ${field('Entries from','timing.entry_window.0',B.timing?.entry_window?.[0],false,5)}${field('to','timing.entry_window.1',B.timing?.entry_window?.[1],false,5)} ${field('Scan every (s)','timing.scan_cadence_s',B.timing?.scan_cadence_s,true,5)} ${field('Top N','top_n',B.top_n,true,3)}
     <p class="sf-note">One lot per entry and the shared portfolio limits are fixed for every strategy and are not editable here.</p></div>`}
   <div class="sf-grid sf-g2"><div><h4>Plain-language preview</h4><pre class="sf-pre">${esc(BVAL?.description||'…')}</pre></div>
    <div><h4>Validation ${v?(v.ok?badge('valid','ok'):badge('errors','bad')):''}</h4>
     ${v?`<ul class="sf-req">${v.errors.map(e=>`<li>✗ ${esc(e)}</li>`).join('')}${v.warnings.map(e=>`<li>⚠ ${esc(e)}</li>`).join('')}
       <li>Warm-up: ${v.warmup_bars} ${esc(B.timeframe)} bars</li><li>Data: ${Object.entries(v.data||{}).map(([k,x])=>`${esc(k)} ${x?'✓':'✗ unavailable'}`).join(' · ')||'—'}</li>
       ${(v.lock_changes||[]).map(l=>`<li>${badge('lock change: '+l.lock,'bad')} activation will need your explicit lock approval</li>`).join('')}</ul>`:'…'}</div></div>
   <div class="sf-act"><button class="sf-btn" data-bsave>Save draft</button> <button class="sf-btn" data-bdup>Save as copy</button>
     ${BSID?`<button class="sf-btn" data-replay="${esc(BSID)}">Replay</button> <button class="sf-btn" data-shadow="${esc(BSID)}">Shadow test</button> <button class="sf-btn" data-reqact="${esc(BSID)}">Request activation</button>`:''}</div>
   <h4>Chart check</h4><div class="sf-act"><label class="sf-p">Symbol <input id="sf-psym" value="${esc(SEL.psym||'RELIANCE')}" size="12"></label> <label class="sf-p">Session <input id="sf-pday" value="${esc(SEL.pday||new Date().toISOString().slice(0,10))}" size="10"></label> <button class="sf-btn" data-prev>Show where it would trade</button></div>
   ${PREV?previewChart(PREV):''}</div>`;
}
function previewChart(p){
  if(p.error) return `<p class="sf-note">${esc(p.error)} ${p.validation?esc(p.validation.errors.join('; ')):''}</p>`;
  const b=p.bars||[]; if(!b.length) return empty('No bars.');
  const W=900,H=260,top=8,bot=22, xs=b.map(x=>[x.h,x.l]).flat();
  Object.values(p.overlays||{}).forEach(s=>s.forEach(v=>fin(v)&&xs.push(v)));
  const hi=Math.max(...xs),lo=Math.min(...xs),sp=(hi-lo)||1,y=v=>top+(hi-v)/sp*(H-top-bot),cw=(W-50)/b.length;
  const idx=Object.fromEntries(b.map((x,i)=>[x.ts,i]));const colors=['var(--sf-info)','var(--sf-warn)','var(--sf-accent)','#8a5cf6'];
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(p.symbol)} ${esc(p.day)} with entry markers">
    ${b.map((x,i)=>{const u=x.c>=x.o,c=u?'var(--sf-up)':'var(--sf-down)',X=i*cw+cw/2;return `<line x1="${X}" x2="${X}" y1="${y(x.h)}" y2="${y(x.l)}" stroke="${c}" stroke-width="${Math.max(.5,cw*.15)}"/><rect x="${X-cw*.3}" y="${y(Math.max(x.o,x.c))}" width="${Math.max(.6,cw*.6)}" height="${Math.max(.8,Math.abs(y(x.o)-y(x.c)))}" fill="${c}"/>`;}).join('')}
    ${Object.entries(p.overlays||{}).map(([k,s],j)=>`<polyline fill="none" stroke="${colors[j%4]}" stroke-width="1.2" points="${s.map((v,i)=>fin(v)?`${i*cw+cw/2},${y(v)}`:'').filter(Boolean).join(' ')}"><title>${esc(k)}</title></polyline>`).join('')}
    ${(p.markers||[]).map(m=>{const i=idx[m.ts];if(i==null)return'';const X=i*cw+cw/2,L=m.direction==='LONG',Y=L?y(b[i].l)+12:y(b[i].h)-12;return `<path d="M${X-5},${Y+(L?6:-6)} L${X+5},${Y+(L?6:-6)} L${X},${Y-(L?2:-2)} Z" fill="${L?'var(--sf-up)':'var(--sf-down)'}"><title>${esc(m.direction)} rule passed ${esc(String(m.ts).slice(11,16))} @ ${esc(m.price)}</title></path>`;}).join('')}
    <text x="${W-4}" y="12" font-size="10" text-anchor="end" fill="var(--sf-muted)">${hi.toFixed(2)}</text><text x="${W-4}" y="${H-bot}" font-size="10" text-anchor="end" fill="var(--sf-muted)">${lo.toFixed(2)}</text></svg>
   <p class="sf-note">${(p.markers||[]).length} bar(s) where an entry rule first passed (▲ long, ▼ short), on completed bars. Overlays: ${Object.keys(p.overlays||{}).map(esc).join(', ')||'none'}. Market/sector gates, verification and limits still decide whether it would actually trade.</p>`;
}
let VT=null;
async function revalidate(rerender){
  clearTimeout(VT); VT=setTimeout(async()=>{ try{ BVAL=await call('POST','/api/futures/strategy/validate',B); }catch(e){ BVAL={validation:{ok:false,errors:[e.message],warnings:[],data:{}}}; } rerender(); },350);
}

/* ------------------------------------------------------------ binding */
function bind(root,rerender,view){
  const on=(sel,fn)=>root.querySelectorAll(sel).forEach(el=>fn(el));
  const act=async(fn,ok)=>{try{const r=await fn();MSG=ok?ok(r):'Done.';}catch(e){MSG='Refused: '+e.message;}OVAT=0;rerender(true);};
  on('[data-rnow]',b=>b.onclick=()=>act(()=>call('POST','/api/futures/research/now',{}),r=>r.created?`Research job ${r.job} queued.`:`A research job (${r.job}) is already ${r.status.toLowerCase()}: no duplicate started.`));
  on('details[data-sg]',d=>d.ontoggle=()=>{if(d.open)OPEN=d.dataset.sg;});
  on('[data-dec]',b=>b.onclick=()=>{const id=b.dataset.id,a=b.dataset.dec;
    const locks=[...root.querySelectorAll(`[data-lock][data-for="${id}"]`)].filter(x=>x.checked).map(x=>x.dataset.lock);
    const mode=root.querySelector(`[data-mode-for="${id}"]`)?.value||undefined;
    const body={action:a,evidence_hash:b.dataset.h,lock_changes_approved:locks,mode};
    if(a==='DISMISS'){const note=prompt('Why dismiss? (optional)');if(note===null)return;body.note=note;body.reconsider=confirm('Show it again if meaningful new evidence arrives?')?'on_new_evidence':'never';}
    if((a==='REPLACE'||a==='ALONGSIDE')&&!confirm(`${a==='REPLACE'?'Replace the current strategy with':'Run alongside the current strategy:'} this exact version, for future paper entries?`))return;
    act(async()=>{const r=await call('POST',`/api/futures/suggestions/${id}/decide`,body);if(a==='EDIT_COPY'){await openBuilder(r.strategy_id);}return r;},
        r=>a==='EDIT_COPY'?'Copy opened in the builder (Strategies tab).':a==='REPLACE'||a==='ALONGSIDE'?`Activated (config v${r.config_version}) for future entries.`:`Suggestion ${String(r.status||'').toLowerCase()}.`);});
  on('[data-open]',r=>r.onclick=async e=>{if(e.target.closest('button,input,select,a'))return;const id=r.dataset.open;
    if(SEL.detail?.strategy?.id===id){SEL.detail=null;SEL.diff=null;return rerender(true);}
    try{SEL.detail=await call('GET','/api/futures/strategies/'+id);SEL.diff=null;}catch(err){MSG=err.message;}rerender(true);});
  on('[data-vdiff]',b=>b.onclick=async()=>{const [a,c]=b.dataset.vdiff.split('|');SEL.diff=(await call('GET',`/api/futures/diff?a=${a}&b=${c}`)).diff;rerender(true);});
  on('[data-restore]',b=>b.onclick=()=>act(()=>call('POST',`/api/futures/strategies/${b.dataset.sid}/restore`,{version_id:b.dataset.restore}),r=>`Restored as new version ${r.version_id} (draft).`));
  on('[data-dup]',b=>b.onclick=()=>act(()=>call('POST',`/api/futures/strategies/${b.dataset.dup}/duplicate`,{}),r=>`Duplicated as ${r.strategy_id}.`));
  on('[data-replay]',b=>b.onclick=()=>act(()=>call('POST',`/api/futures/strategies/${b.dataset.replay}/replay`,{}),r=>`Replay job ${r.job} ${r.created?'queued':'already exists'}; results appear in the Research tab when the research worker finishes it.`));
  on('[data-shadow]',b=>b.onclick=()=>act(()=>call('POST',`/api/futures/strategies/${b.dataset.shadow}/shadow`,{}),()=>'Frozen and started in forward shadow (separate records; not the active portfolio).'));
  on('[data-reqact]',b=>b.onclick=()=>act(()=>call('POST',`/api/futures/strategies/${b.dataset.reqact}/request-activation`,{}),r=>`Activation requested: review and approve suggestion ${r.suggestion} in the Research tab.`));
  on('[data-life]',b=>b.onclick=()=>{if(!confirm(`Set this strategy to ${b.dataset.life}? New entries stop; open positions keep their plan.`))return;act(()=>call('POST',`/api/futures/strategies/${b.dataset.sid}/lifecycle`,{to:b.dataset.life}));});
  on('[data-edit]',b=>b.onclick=()=>openBuilder(b.dataset.edit).then(()=>rerender(true)));
  on('[data-new]',b=>b.onclick=()=>newBuilder(b.dataset.new).then(()=>rerender(true)));
  if(view==='lib'&&B){
    on('[data-bclose]',b=>b.onclick=()=>{B=null;BSID=null;PREV=null;rerender(true);});
    on('[data-badv]',b=>b.onclick=()=>{ADV=!ADV;rerender(true);});
    on('[data-bp]',el=>el.onchange=()=>{let v=el.value;if(el.dataset.num==='true'&&v!=='')v=Number(v);
      if(el.dataset.bp==='tags')v=String(v).split(',').map(x=>x.trim()).filter(Boolean);
      if(el.dataset.bp==='exits.trailing.value'){B.exits.trailing=v===''||v===0?null:{basis:'pct',value:v};}
      else if(el.dataset.bp==='exits.target.value'&&!B.exits.target){B.exits.target={basis:'r_multiple',value:v};}
      else setp(B,el.dataset.bp,v);
      if(el.dataset.bp.endsWith('.ind')){const op=getp(B,el.dataset.bp.slice(0,-4));op.params={};}
      if(el.dataset.bp==='mode'&&v==='swing')B.timeframe='1d';
      revalidate(()=>rerender(true));rerender(true);});
    on('[data-dir]',el=>el.onchange=()=>{const d=el.dataset.dir;B.directions=el.checked?[...new Set([...B.directions,d])]:B.directions.filter(x=>x!==d);
      if(el.checked&&!B.entry[d])B.entry[d]={all:[{left:{ind:'price',params:{field:'close'}},op:d==='LONG'?'>':'<',right:{ind:'vwap'}}]};revalidate(()=>rerender(true));rerender(true);});
    on('[data-gk]',el=>el.onchange=()=>{const n=getp(B,el.dataset.gk),k=n.all?'all':'any';n[el.value]=n[k];if(el.value!==k)delete n[k];revalidate(()=>rerender(true));rerender(true);});
    on('[data-addc]',b=>b.onclick=()=>{getp(B,b.dataset.addc).push({left:{ind:'rsi',params:{period:14}},op:'>',right:{value:50}});revalidate(()=>rerender(true));rerender(true);});
    on('[data-addg]',b=>b.onclick=()=>{getp(B,b.dataset.addg).push({any:[{left:{ind:'price',params:{field:'close'}},op:'>',right:{ind:'ema',params:{period:20}}}]});revalidate(()=>rerender(true));rerender(true);});
    on('[data-del]',b=>b.onclick=()=>{delp(B,b.dataset.del);revalidate(()=>rerender(true));rerender(true);});
    on('[data-tog]',b=>b.onclick=()=>{const o=getp(B,b.dataset.tog);setp(B,b.dataset.tog,'value' in (o||{})?{ind:'vwap'}:{value:0});revalidate(()=>rerender(true));rerender(true);});
    on('[data-advapply]',b=>b.onclick=()=>{try{B=JSON.parse(root.querySelector('#sf-adv').value);MSG='JSON applied.';revalidate(()=>rerender(true));}catch(e){MSG='Invalid JSON: '+e.message;}rerender(true);});
    on('[data-export]',b=>b.onclick=()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(B,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download=(B.name||'strategy').replace(/[^\w-]+/g,'_')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);});
    on('[data-import]',el=>el.onchange=async()=>{try{B=JSON.parse(await el.files[0].text());BSID=null;MSG='Imported: save it as a new draft.';revalidate(()=>rerender(true));}catch(e){MSG='Import failed: '+e.message;}rerender(true);});
    on('[data-bsave]',b=>b.onclick=()=>act(async()=>{const r=BSID?await call('POST',`/api/futures/strategies/${BSID}/edit`,{body:B}):await call('POST','/api/futures/strategies',{body:B});BSID=r.strategy_id;return r;},
       r=>r.copied?`The strategy is active, so the edit was saved as a new draft copy ${r.strategy_id} (activation needs approval).`:r.version_id===(window.SFX._head||'')?'No changes to save.':`Saved version ${r.version_id}.`));
    on('[data-bdup]',b=>b.onclick=()=>act(async()=>{const r=await call('POST','/api/futures/strategies',{body:Object.assign({},B,{name:(B.name||'')+' (copy)'})});BSID=r.strategy_id;return r;},r=>`Saved as new strategy ${r.strategy_id}.`));
    on('[data-prev]',b=>b.onclick=async()=>{SEL.psym=root.querySelector('#sf-psym').value.trim().toUpperCase();SEL.pday=root.querySelector('#sf-pday').value.trim();
      PREV={error:'Loading…'};rerender(true);try{PREV=await call('POST','/api/futures/strategy/preview',{body:B,symbol:SEL.psym,day:SEL.pday});}catch(e){PREV={error:e.message};}rerender(true);});
  }
}
async function loadInd(){ if(!IND&&api()){ try{ IND=await call('GET','/api/futures/indicators'); }catch(e){ MSG=e.message; } } }
async function openBuilder(sid){ await loadInd(); const d=await call('GET','/api/futures/strategies/'+sid); B=d.body; BSID=sid; PREV=null; ADV=false;
  window.SFX._head=d.strategy.head_version; window.SFX._go&&window.SFX._go('lib'); BVAL=await call('POST','/api/futures/strategy/validate',B).catch(()=>null); }
async function newBuilder(mode){ await loadInd(); const o=OV||{}; const act=(o.strategies||[]).find(s=>s.lifecycle==='ACTIVE'&&s.mode===mode);
  const base=act?(await call('GET','/api/futures/strategies/'+act.id)).body:null;
  if(!base){ MSG='No active template to start from.'; return; }
  B=JSON.parse(JSON.stringify(base)); B.name=`New ${mode} strategy`; B.tags=['user']; BSID=null; PREV=null; ADV=false;
  BVAL=await call('POST','/api/futures/strategy/validate',B).catch(()=>null); }

window.SFX={
  async render(view,s,el,rerender){
    if(view==='lib') await loadInd();
    const o=await overview(s,false);
    el.innerHTML=view==='res'?researchView(o):libraryView(o);
    bind(el,f=>{ if(f) OVAT=0; rerender(); },view);
    if(view==='res'&&(o?.jobs||[]).some(j=>j.status==='RUNNING'||j.status==='QUEUED')) setTimeout(()=>{OVAT=0;},8000);
  },
  sig(view){ return JSON.stringify([view,OVAT,MSG,OPEN,!!B,ADV,SEL.detail?.strategy?.id,SEL.diff?.length,PREV?.markers?.length,PREV?.error,BVAL?.validation?.errors?.length]); }
};
})();

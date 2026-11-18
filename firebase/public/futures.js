/* Stock Futures workspace (paper trading and replay, FUTSTK only).
   Reads /live/futures on Firebase (read-only), or /api/futures/state when the
   page is served by the API. Operator controls (pause, entry approval, exit
   override) appear only in API mode and are enforced server-side by the
   service's own credentials; the Firebase page can never write.
   If neither source has data yet, it shows /futures-demo.json, a fixture run
   with synthetic data, under a FIXTURE banner. Styles: futures.css. */
(function(){
'use strict';
const q=(s,r=document)=>r.querySelector(s), qa=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fin=v=>v!=null&&isFinite(v);
const n=(v,d=2)=>fin(v)?Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const rs=v=>fin(v)?(v<0?'−':'')+'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN'):'—';
const srs=v=>fin(v)?(v>0?'+':'')+rs(v):'—';
const pc=v=>fin(v)?(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(2)+'%':'—';
const cls=v=>!fin(v)||v===0?'':v>0?'up':'down';
const t5=s=>s?String(s).slice(11,16):'—';
const dt=s=>s?String(s).slice(0,10)+' '+String(s).slice(11,16):'—';
const empty=t=>`<div class="sf-empty">${esc(t)}</div>`;
const badge=(t,k)=>`<span class="sf-b sf-${k||'n'}">${esc(t)}</span>`;
const res=r=>badge(r||'—',r==='PASS'?'ok':r==='FAIL'?'bad':r==='UNAVAILABLE'?'warn':'n');
const dir=d=>badge(d||'—',d==='LONG'?'ok':d==='SHORT'?'bad':'n');

const main=q('main'); if(!main) return;
const sec=document.createElement('section'); sec.id='sfut';
sec.innerHTML=`<div class="sf-head"><div><h1>Stock Futures <span id="sf-label"></span></h1><div class="sf-sub" id="sf-sub">Loading…</div></div>
 <div class="sf-ctl"><div class="sf-tabs" role="tablist" aria-label="Mode">
  <button role="tab" data-m="intraday" aria-selected="true">Intraday</button><button role="tab" data-m="swing" aria-selected="false">Swing</button></div>
 <div class="sf-tabs" role="tablist" aria-label="View">
  <button role="tab" data-v="scan" aria-selected="true">Scan</button><button role="tab" data-v="pos" aria-selected="false">Positions</button>
  <button role="tab" data-v="jr" aria-selected="false">Journal</button></div></div></div>
 <div id="sf-banner"></div><div id="sf-body"></div><div class="sf-foot" id="sf-foot"></div>`;
main.append(sec);
const btn=document.createElement('button'); btn.dataset.t='sfut'; btn.textContent='Stock Futures'; btn.setAttribute('aria-label','Stock Futures');
const after=q('nav button[data-t="opts"]')||q('nav button[data-t="pulse"]');
after?after.after(btn):(q('nav .chapters')||q('nav')).append(btn);
function show(){
  qa('nav button[data-t]').forEach(x=>{x.classList.toggle('on',x===btn);x.setAttribute('aria-current',x===btn?'page':'false');});
  qa('main section').forEach(s=>s.classList.toggle('on',s===sec));
  history.replaceState(null,'','#sfut'); paint(true);
}
btn.addEventListener('click',show);
// Market Pulse claims the landing page on load; a #sfut deep link takes it back once everything has mounted.
if(location.hash==='#sfut'){ show(); addEventListener('load',()=>setTimeout(()=>{if(location.hash==='#sfut'||!sec.classList.contains('on'))show();},50),{once:true}); }

let MODE='intraday', VIEW='scan', SIG='', S=null, SRCKIND='', JF={mode:'',dir:'',cls:''};
qa('[data-m]',sec).forEach(b=>b.onclick=()=>{MODE=b.dataset.m;qa('[data-m]',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});
qa('[data-v]',sec).forEach(b=>b.onclick=()=>{VIEW=b.dataset.v;qa('[data-v]',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});

const apiMode=()=>!(typeof SRC!=='undefined'&&SRC.mode==='firebase')&&typeof get==='function';
async function load(){
  try{
    if(typeof SRC!=='undefined'&&SRC.mode==='firebase'){ const v=SRC.live?.futures; if(v&&v.available!==false){SRCKIND='live';return v;} }
    else if(typeof get==='function'){ const v=await get('/api/futures/state'); if(v&&v.available!==false){SRCKIND='api';return v;} }
  }catch(e){}
  try{ const r=await fetch('/futures-demo.json',{cache:'no-cache'}); if(r.ok){SRCKIND='demo';return await r.json();} }catch(e){}
  return null;
}
async function paint(force){
  if(!sec.classList.contains('on')) return;
  const s=await load(); const sig=JSON.stringify(s)+MODE+VIEW+JSON.stringify(JF);
  if(!force&&sig===SIG) return; SIG=sig; S=s;
  const body=q('#sf-body');
  if(!s){ q('#sf-sub').textContent='No stock-futures data yet'; body.innerHTML=empty('The stock-futures worker has not published anything yet. See README: Stock futures paper system.'); return; }
  const kind=SRCKIND==='demo'?'FIXTURE DEMO · synthetic data':s.label||'PAPER';
  q('#sf-label').innerHTML=badge(kind,SRCKIND==='demo'||/FIXTURE|REPLAY/.test(kind)?'warn':'info');
  q('#sf-sub').textContent=`Session ${s.session||'—'} · updated ${t5(s.generated)} IST · config v${s.config_version??'—'}`;
  const b=[];
  if(s.config_label) b.push(`<b>Config:</b> ${esc(s.config_label)}`);
  if(s.live_entry_gate) b.push(`<b>Entries blocked:</b> ${esc(s.live_entry_gate)}`);
  if(s.paused_global) b.push('<b>Global pause is ON</b> — no new entries; monitoring and exits continue.');
  q('#sf-banner').innerHTML=b.length?`<div class="sf-banner">${b.join('<br>')}</div>`:'';
  q('#sf-foot').innerHTML=(s.notes||[]).map(esc).join('<br>');
  body.innerHTML=VIEW==='scan'?scanView(s):VIEW==='pos'?posView(s):journalView(s);
  bind();
}
setInterval(()=>paint(false),5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)paint(false);});

/* ------------------------------------------------------------ scan */
function kv(rows){return `<table class="sf-kv">${rows.map(([k,v])=>`<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join('')}</table>`;}
function scanView(s){
  const p=s.profiles?.[MODE]; if(!p) return empty('No profile for this mode.');
  const sc=p.last_scan;
  const prof=`<div class="sf-card"><h3>${MODE==='intraday'?'Intraday':'Swing'} profile</h3>${kv([
    ['Status',p.paused?badge('PAUSED','warn'):badge('active','ok')],['Benchmark',esc(p.benchmark)],
    ['Regime now',esc(p.regime||'—')],['Scan cadence',p.cadence_s?p.cadence_s/60+' min':'—'],
    ['Entry window',(p.entry_window||[]).join('–')+' IST'],[MODE==='intraday'?'Exit deadline':'Holding',MODE==='intraday'?esc(p.exit_deadline)+' IST':'overnight allowed; pre-expiry exit enforced'],
    ['Direction map',Object.entries(p.direction_map||{}).map(([k,v])=>`${k}→${v.join(',')||'none'}`).join(' · ')]])}</div>`;
  const feeds=s.feeds?`<div class="sf-card"><h3>Feed health</h3>${kv(Object.entries(s.feeds.feeds||{}).map(([k,v])=>[k,badge(v,v==='ok'?'ok':/fixture/.test(v)?'info':'bad')]))}
    <p class="sf-note">Source: ${esc(s.feeds.source)}${s.feeds.stock_futures!=null?` · ${s.feeds.stock_futures} FUTSTK contracts in master`:''}</p></div>`:'';
  if(!sc) return `<div class="sf-grid sf-g2">${prof}${feeds}</div>`+empty('No scan has run for this mode yet.');
  const reg=sc.regime||{}, ev=reg.evidence||{};
  const regime=`<div class="sf-card"><h3>Market regime ${badge(reg.regime||'—',reg.regime==='BULLISH'?'ok':reg.regime==='BEARISH'?'bad':'warn')}</h3>
    <p>Permitted: ${(reg.permitted||[]).map(dir).join(' ')||badge('no new entries','warn')}</p>
    ${kv(Object.entries(ev.metrics||{}).map(([k,v])=>[k,n(v,3)]).concat([
      ['Window',esc(ev.window?`${dt(ev.window.start_ts)} → ${dt(ev.window.end_ts)} (${ev.window.effective})`:'—')],
      ['Breadth',ev.breadth?`${ev.breadth.advancers} up / ${ev.breadth.decliners} down of ${ev.breadth.counted}`:'—'],['Source',esc(ev.source)]]))}
    <p class="sf-note">${(reg.reasons||[]).map(esc).join('<br>')}</p></div>`;
  const by=sc.sectors?.by_direction||{};
  const sectors=Object.entries(by).map(([d,v])=>`<div class="sf-card sf-wide"><h3>Sectors for ${dir(d)}</h3><div class="sf-scroll"><table class="sf-t"><thead><tr><th>Rank</th><th>Sector</th><th>Return</th><th>vs benchmark</th><th>Coverage</th><th>Absolute</th><th>Relative</th><th>Result</th><th>Reason</th></tr></thead><tbody>
    ${v.assessed.slice().sort((a,b)=>(a.rank||99)-(b.rank||99)).map(a=>`<tr><td>${a.rank||''}</td><td>${esc(a.sector)}</td><td class="${cls(a.return_pct)}">${pc(a.return_pct)}</td><td class="${cls(a.relative_pct)}">${pc(a.relative_pct)}</td><td>${a.fresh}/${a.constituents}</td><td>${res(a.absolute_test)}</td><td>${res(a.relative_test)}</td><td>${res(a.result)}</td><td class="sf-why">${esc(a.reason)}</td></tr>`).join('')}
    </tbody></table></div><p class="sf-note">Sector metrics: ${esc((v.assessed[0]||{}).source||'—')}. Unmapped: ${(sc.sectors.unmapped||[]).map(u=>esc(u.symbol)).join(', ')||'none'}.</p></div>`).join('');
  const cands=sc.candidates?.candidates||[];
  const ver={}; (sc.verification||[]).forEach(v=>ver[v.symbol]=v);
  const rej={}; (sc.rejections||[]).forEach(r=>rej[r.symbol]=r.reason);
  const stocks=`<div class="sf-card sf-wide"><h3>Ranked stocks</h3>${cands.length?`<div class="sf-scroll"><table class="sf-t"><thead><tr><th>#</th><th>Symbol</th><th>Dir</th><th>Sector</th><th>Score</th><th>Return</th><th>vs bench</th><th>Turnover</th><th>Verification</th><th>Entry</th></tr></thead><tbody>
    ${cands.map(c=>{const v=ver[c.symbol];return `<tr><td>${c.rank+1}</td><td><b>${esc(c.symbol)}</b></td><td>${dir(c.direction)}</td><td>${esc(c.sector)} (#${c.sector_rank})</td><td>${n(c.score,2)}</td><td class="${cls(c.metrics.return_pct)}">${pc(c.metrics.return_pct)}</td><td class="${cls(c.metrics.rel_return_pct)}">${pc(c.metrics.rel_return_pct)}</td><td>${rs(c.metrics.turnover_rs)}</td><td>${v?res(v.status)+` <span class="sf-why">${esc((v.reasons||[]).join('; '))}</span>`:'—'}</td><td class="sf-why">${rej[c.symbol]?esc(rej[c.symbol]):sc.trade_id&&sc.trade_id.endsWith(c.symbol)?badge('entered','ok'):''}</td></tr>`;}).join('')}
    </tbody></table></div><p class="sf-note">Tie-break: ${esc(sc.candidates.tie_break)} · candidates expire ${t5(sc.candidates.expires_at)} IST</p>`:empty('No candidates in this scan.')}</div>`;
  const v0=(sc.verification||[]).find(v=>v.status==='PASS');
  const verif=v0?`<div class="sf-card"><h3>Nifty Desk verification · ${esc(v0.symbol)}</h3>${kv(Object.entries(v0.evidence?.metrics||{}).map(([k,v])=>[k+' ('+(v0.evidence.kinds?.[k]||'?')+')',n(v,2)]).concat([
      ['Snapshot',`${dt(v0.evidence.snapshot_ts)} (${v0.evidence.age_s}s old)`],['Source',esc(v0.evidence.source)],
      ['Shares a source with screening',v0.evidence.shared_source_with_screening?badge('yes','warn'):'no']]))}</div>`:'';
  const k=sc.contract;
  const contract=k?`<div class="sf-card"><h3>Chosen contract</h3>${kv([['Contract',esc(k.tradingsymbol)],['Instrument id',esc(k.instrument_id)],['Expiry',esc(k.expiry)],['Lot / tick',`${k.lot_size} / ${k.tick_size}`],
      ...(k.pre_expiry_deadline?[['Pre-expiry exit by',dt(k.pre_expiry_deadline)]]:[])])}</div>`:'';
  const stop=sc.no_trade_reason?`<div class="sf-banner sf-warnb"><b>No trade:</b> ${esc(sc.no_trade_reason)}</div>`:'';
  return `<p class="sf-sub">Last scan ${esc(sc.scan_id)} · ${esc(sc.status)} · config v${sc.config_version}</p>${stop}
    <div class="sf-grid sf-g3">${prof}${regime}${feeds}</div><div class="sf-grid">${sectors}${stocks}</div>
    <div class="sf-grid sf-g2">${verif}${contract}</div>${timelineCard('Scan timeline',sc.timeline)}`;
}
function timelineCard(title,tl){
  if(!tl||!tl.length) return '';
  return `<details class="sf-card sf-tl"><summary><h3>${esc(title)}</h3></summary><ol>${tl.map(e=>`<li><span class="sf-tm">${dt(e.at)}</span> <span class="sf-ag">${esc(e.agent)}</span> <b>${esc(e.type)}</b> ${esc(e.summary)}</li>`).join('')}</ol></details>`;
}

/* ------------------------------------------------------------ positions */
function posView(s){
  const ps=(s.positions||[]).filter(p=>p.mode===MODE);
  const ap=(s.approvals||[]).filter(a=>ps.some(p=>p.trade_id===a.trade_id));
  const ops=apiMode()&&SRCKIND==='api';
  const ctl=ops?`<div class="sf-card"><h3>Operator controls</h3><p class="sf-note">Requires the service's login (DASH_USER/DASH_PASS or DASH_TOKEN). Pauses stop new entries only.</p>
    <button class="sf-btn" data-pause="global" data-on="${s.paused_global?0:1}">${s.paused_global?'Resume':'Pause'} all entries</button>
    <button class="sf-btn" data-pause="${MODE}" data-on="${s.profiles?.[MODE]?.paused?0:1}">${s.profiles?.[MODE]?.paused?'Resume':'Pause'} ${MODE}</button><span id="sf-msg" class="sf-note"></span></div>`
    :`<p class="sf-note">Read-only view. Operator controls are available on the authenticated API page.</p>`;
  const apps=ap.length?`<div class="sf-card"><h3>Pending requests</h3><table class="sf-t"><thead><tr><th>Kind</th><th>Trade</th><th>Deadline</th><th></th></tr></thead><tbody>${ap.map(a=>`<tr><td>${esc(a.kind)}</td><td>${esc(a.trade_id)}</td><td>${dt(a.deadline)}</td><td>${ops&&a.kind==='entry'?`<button class="sf-btn" data-appr="${esc(a.approval_id)}" data-d="APPROVE">Approve</button> <button class="sf-btn" data-appr="${esc(a.approval_id)}" data-d="REJECT">Reject</button>`:a.kind==='override'?'<span class="sf-note">default plan active while waiting</span>':''}</td></tr>`).join('')}</tbody></table></div>`:'';
  const rows=ps.map(p=>`<div class="sf-card sf-wide"><h3>${dir(p.direction)} ${esc(p.tradingsymbol||p.underlying)} ${badge(p.status,p.status==='OPEN'?'ok':p.status==='UNRESOLVED'||p.unresolved?'bad':'warn')} ${p.overnight?badge('overnight','info'):''} ${p.data_flag?badge('data: '+p.data_flag,'bad'):''}</h3>
    <div class="sf-grid sf-g4">${kv([['Opened with',`${p.direction==='LONG'?'BUY':'SELL'} · OPEN`],['Closes with',`${p.direction==='LONG'?'SELL':'BUY'} · CLOSE`],['Lots / units',`${p.lots} / ${p.units} (filled ${p.entry_filled_units??0})`],['Instrument',esc(p.instrument_id)]])}
    ${kv([['Entry avg',n(p.entry_avg)],['Mark ('+esc(p.mark_side||'—')+')',n(p.mark)],['MTM (gross)',`<span class="${cls(p.mtm)}">${srs(p.mtm)}</span>`],['Expiry',esc(p.expiry)]])}
    ${kv([['Stop',n(p.stop_price)],['Target',n(p.target_price)],['Plan version',p.plan_version],['Evaluated on',esc(p.plan?.evaluated_on||'—')]])}
    ${kv(MODE==='intraday'?[['Exit deadline',dt(p.intraday_deadline)],['Exit',esc(p.exit?.reason||'—')],['Unresolved',esc(p.unresolved?.reason||'—')]]
       :[['Sessions held',`${p.sessions_held??'—'} / ${p.max_holding_sessions??'—'}`],['Pre-expiry exit by',dt(p.pre_expiry_deadline)],['Exit',esc(p.exit?.reason||'—')],['Unresolved',esc(p.unresolved?.reason||'—')]])}</div>
    ${ops&&p.status==='OPEN'?`<form class="sf-ov" data-tid="${esc(p.trade_id)}"><b>Override exit</b> stop <input name="stop_price" inputmode="decimal" size="8"> target <input name="target_price" inputmode="decimal" size="8">${MODE==='swing'?' max sessions <input name="max_holding_sessions" inputmode="numeric" size="3">':''} <button class="sf-btn">Apply</button></form>`:''}
    ${timelineCard('Trade timeline',p.timeline)}</div>`).join('');
  const ord=(s.orders||[]).filter(o=>o.mode===MODE).slice(-30).reverse();
  const orders=`<div class="sf-card sf-wide"><h3>Paper orders</h3>${ord.length?`<div class="sf-scroll"><table class="sf-t"><thead><tr><th>Order</th><th>Side</th><th>Purpose</th><th>Direction</th><th>Status</th><th>Requested</th><th>Filled</th><th>Created</th></tr></thead><tbody>${ord.map(o=>`<tr><td class="sf-why">${esc(o.client_order_id)}</td><td>${badge(o.side,o.side==='BUY'?'ok':'bad')}</td><td>${esc(o.purpose)}</td><td>${esc(o.direction)}</td><td>${esc(o.status)}</td><td>${o.requested_qty}</td><td>${o.filled_qty}</td><td>${dt(o.created)}</td></tr>`).join('')}</tbody></table></div>`:empty('No orders in this mode.')}</div>`;
  const fl=(s.fills||[]).filter(f=>f.mode===MODE).slice(-30).reverse();
  const fills=fl.length?`<div class="sf-card sf-wide"><h3>Fills</h3><div class="sf-scroll"><table class="sf-t"><thead><tr><th>Time</th><th>Side</th><th>Purpose</th><th>Qty</th><th>Fill price</th><th>Quote bid / ask</th><th>Model</th><th>Costs</th></tr></thead><tbody>${fl.map(f=>`<tr><td>${dt(f.ts)}</td><td>${esc(f.side)}</td><td>${esc(f.purpose)}</td><td>${f.qty}</td><td>${n(f.price)}</td><td>${n(f.evidence?.bid)} / ${n(f.evidence?.ask)}</td><td>${esc(f.evidence?.model)}</td><td>${rs(f.costs?.total)}</td></tr>`).join('')}</tbody></table></div></div>`:'';
  return `${ctl}${apps}${rows||empty('No open, pending or unresolved positions in this mode.')}${orders}${fills}`;
}

/* ------------------------------------------------------------ journal */
function journalView(s){
  const all=s.journal||[];
  const rows=all.filter(r=>(!JF.mode||r.mode===JF.mode)&&(!JF.dir||r.direction===JF.dir)&&(!JF.cls||r.class===JF.cls));
  const st=rows.filter(r=>r.class==='settled');
  let eq=0,peak=0,dd=0; st.slice().sort((a,b)=>(a.closed_at||'').localeCompare(b.closed_at||'')).forEach(r=>{eq+=r.net_pnl;peak=Math.max(peak,eq);dd=Math.min(dd,eq-peak);});
  const sum=k=>st.reduce((a,r)=>a+(r[k]||0),0);
  const tiles=[['Settled',st.length],['Excluded',rows.filter(r=>r.class==='excluded').length],['Unresolved',rows.filter(r=>r.class==='unresolved').length],
    ['Gross P&L',srs(sum('gross_pnl'))],['Est. costs',rs(sum('costs'))],['Net P&L',srs(sum('net_pnl'))],['Max drawdown',rs(dd)]];
  const sel=(id,opts,v)=>`<select id="${id}">${opts.map(([k,l])=>`<option value="${k}"${k===v?' selected':''}>${l}</option>`).join('')}</select>`;
  return `<div class="sf-card sf-wide"><h3>Futures paper journal ${badge(SRCKIND==='demo'?'FIXTURE':(s.label||'PAPER'),'warn')}</h3>
   <div class="sf-toolbar"><label>Mode ${sel('sf-jm',[['','All'],['intraday','Intraday'],['swing','Swing']],JF.mode)}</label>
   <label>Direction ${sel('sf-jd',[['','All'],['LONG','Long'],['SHORT','Short']],JF.dir)}</label>
   <label>Class ${sel('sf-jc',[['','All'],['settled','Settled'],['excluded','Excluded'],['unresolved','Unresolved'],['open','Open']],JF.cls)}</label>
   <button class="sf-btn" id="sf-csv">Export filtered CSV</button></div>
   <div class="sf-tiles">${tiles.map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('')}</div>
   <p class="sf-note">Net P&amp;L counts settled trades only. Excluded trades (no fill, or closed after a data exception) and unresolved exits are shown but kept out of settled P&amp;L. Costs are estimates.</p>
   <div class="sf-scroll"><table class="sf-t"><thead><tr><th>Closed</th><th>Mode</th><th>Dir</th><th>Contract</th><th>Sides</th><th>Units</th><th>Entry</th><th>Exit</th><th>Gross</th><th>Costs</th><th>Net</th><th>Reason</th><th>Class</th></tr></thead><tbody>
   ${rows.slice().reverse().map(r=>`<tr><td>${dt(r.closed_at||r.opened_at)}</td><td>${esc(r.mode)}</td><td>${dir(r.direction)}</td><td>${esc(r.tradingsymbol)}</td><td>${esc(r.entry_side||'—')}→${esc(r.exit_side||'—')}</td><td>${r.closed_units??r.units??''}</td><td>${n(r.entry_avg)}</td><td>${n(r.exit_avg)}</td><td class="${cls(r.gross_pnl)}">${srs(r.gross_pnl)}</td><td>${rs(r.costs)}</td><td class="${cls(r.net_pnl)}"><b>${srs(r.net_pnl)}</b></td><td class="sf-why">${esc(r.exit_reason||'')}${r.exception?' · '+esc(r.exception):''}</td><td>${badge(r.class,r.class==='settled'?'ok':r.class==='unresolved'?'bad':'n')}</td></tr>`).join('')||`<tr><td colspan="13">${empty('No trades match.')}</td></tr>`}
   </tbody></table></div></div>`;
}

/* ------------------------------------------------------------ actions */
async function op(path,body){
  const m=q('#sf-msg');
  try{ const r=await post(path,body); if(m) m.textContent=r.ok?'Done.':(r.error||'Refused'); }
  catch(e){ if(m) m.textContent='Refused: '+e.message; else alert('Refused: '+e.message); }
  paint(true);
}
function bind(){
  qa('[data-pause]',sec).forEach(b=>b.onclick=()=>op('/api/futures/ops/pause',{scope:b.dataset.pause,paused:b.dataset.on==='1'}));
  qa('[data-appr]',sec).forEach(b=>b.onclick=()=>op('/api/futures/ops/approval',{approval_id:b.dataset.appr,decision:b.dataset.d}));
  qa('form.sf-ov',sec).forEach(f=>f.onsubmit=e=>{e.preventDefault();const edits={};
    new FormData(f).forEach((v,k)=>{if(String(v).trim()!=='')edits[k]=Number(v);});
    op('/api/futures/ops/override',{trade_id:f.dataset.tid,edits});});
  const jm=q('#sf-jm'),jd=q('#sf-jd'),jc=q('#sf-jc');
  if(jm){jm.onchange=()=>{JF.mode=jm.value;paint(true);};jd.onchange=()=>{JF.dir=jd.value;paint(true);};jc.onchange=()=>{JF.cls=jc.value;paint(true);};}
  const csv=q('#sf-csv');
  if(csv) csv.onclick=()=>{
    const cols=['trade_id','mode','direction','underlying','tradingsymbol','instrument_id','expiry','entry_side','exit_side','lots','closed_units','opened_at','closed_at','entry_avg','exit_avg','trigger_price','gross_pnl','costs','net_pnl','exit_reason','exception','class','config_version','label'];
    const rows=(S.journal||[]).filter(r=>(!JF.mode||r.mode===JF.mode)&&(!JF.dir||r.direction===JF.dir)&&(!JF.cls||r.class===JF.cls));
    // same spreadsheet-formula guard as the Results export
    const cell=x=>'"'+String(x??'').replace(/^[=+@-]/,"'$&").replace(/"/g,'""')+'"';
    const out=[cols.join(','),...rows.map(r=>cols.map(k=>cell(r[k])).join(','))].join('\r\n');
    const u=URL.createObjectURL(new Blob([out],{type:'text/csv'}));const a=document.createElement('a');a.href=u;a.download='futures-paper-journal.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
  };
}
})();

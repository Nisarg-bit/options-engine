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
  <button role="tab" data-v="jr" aria-selected="false">Journal</button><button role="tab" data-v="mkt" aria-selected="false">Market</button><button role="tab" data-v="hl" aria-selected="false">Health</button><button role="tab" data-v="lib" aria-selected="false">Strategies</button><button role="tab" data-v="res" aria-selected="false">Research</button></div></div></div>
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
let MODE='intraday', VIEW='scan', SIG='', S=null, SRCKIND='', JF={mode:'',dir:'',cls:''};
let MK={sort:{},n:{},sector:null,stock:null}, RSIG='', MSIG='', PK='', PCACHE={at:0,v:null};
qa('[data-m]',sec).forEach(b=>b.onclick=()=>{MODE=b.dataset.m;qa('[data-m]',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});
qa('[data-v]',sec).forEach(b=>b.onclick=()=>{VIEW=b.dataset.v;qa('[data-v]',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});

// Deep links: #sfut, #sfut/pos, #sfut/jr, #sfut/mkt, optionally /swing (e.g. #sfut/pos/swing).
// Market Pulse claims the landing page on load; a deep link takes it back once everything has mounted.
const DL=location.hash.match(/^#sfut(?:\/(scan|pos|jr|mkt|hl|lib|res))?(?:\/(intraday|swing))?$/);
if(DL){
  VIEW=DL[1]||'scan'; MODE=DL[2]||'intraday';
  qa('[data-v]',sec).forEach(x=>x.setAttribute('aria-selected',x.dataset.v===VIEW));
  qa('[data-m]',sec).forEach(x=>x.setAttribute('aria-selected',x.dataset.m===MODE));
  show();
  addEventListener('load',()=>[50,400,1200].forEach(ms=>setTimeout(()=>{if(location.hash.startsWith('#sfut')&&!sec.classList.contains('on'))show();},ms)),{once:true});
}

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
  if(!force&&sig===SIG&&VIEW!=='mkt') return; SIG=sig; S=s;
  const body=q('#sf-body');
  if(VIEW==='lib'||VIEW==='res'){
    if(!window.SFX){ body.innerHTML=empty('Loading…'); return; }
    window.SFX._go=v=>{VIEW=v;qa('[data-v]',sec).forEach(x=>x.setAttribute('aria-selected',x.dataset.v===v));};
    const rsig=window.SFX.sig(VIEW)+JSON.stringify(s?.research?.jobs||'');
    if(!force&&rsig===RSIG) return; RSIG=rsig;
    await window.SFX.render(VIEW,s,body,()=>paint(true)); return;
  }
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
  if(VIEW==='mkt'){ const p=await loadPulse(); const ms=JSON.stringify([p?.stock_oi,p?.session,MK]);
    if(!force&&ms===MSIG) return; MSIG=ms; body.innerHTML=marketView(p); bindMarket(); return; }
  body.innerHTML=VIEW==='scan'?scanView(s):VIEW==='pos'?posView(s):VIEW==='hl'?healthView(s):journalView(s);
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
  const regime=ev.method==='opening_range_fib_vwap'||ev.method==='return_structure'?regimeCard(reg,ev):`<div class="sf-card"><h3>Market regime ${badge(reg.regime||'—',reg.regime==='BULLISH'?'ok':reg.regime==='BEARISH'?'bad':'warn')}</h3>
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
    </tbody></table></div><p class="sf-note">Tie-break: ${esc(sc.candidates.tie_break)} · candidates expire ${t5(sc.candidates.expires_at)} IST${cands[0]?.rank_reason?`<br><b>Why ${esc(cands[0].symbol)} ranked first:</b> ${esc(cands[0].rank_reason)}`:''}</p>`:empty('No candidates in this scan.')}</div>`
    +(cands.some(c=>c.metrics.vwap!=null)||(sc.candidates?.rejected||[]).some(r=>r.checks)?breakoutCard(cands,sc.candidates?.rejected||[]):'');
  const v0=(sc.verification||[]).find(v=>v.status==='PASS');
  const val=v=>typeof v==='string'?esc(v):n(v,2);
  const verif=v0?`<div class="sf-card"><h3>Nifty Desk verification · ${esc(v0.symbol)}</h3>${kv(Object.entries(v0.evidence?.metrics||{}).map(([k,v])=>[k+' ('+(v0.evidence.kinds?.[k]||'?')+')',val(v)]).concat([
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
function summaryCard(d){
  if(!d) return '';
  const rows=Object.entries(d.by_mode_direction||{}).map(([k,b])=>`<tr><td>${esc(k)}</td><td>${b.trades}</td><td>${b.wins}</td><td class="${cls(b.gross)}">${srs(b.gross)}</td><td>${rs(b.costs)}</td><td class="${cls(b.net)}"><b>${srs(b.net)}</b></td></tr>`).join('');
  const reg=Object.entries(d.regimes||{}).filter(([,c])=>Object.keys(c).length).map(([m,c])=>`${esc(m)}: `+Object.entries(c).map(([k,v])=>`${esc(k)} ${v}`).join(', ')).join(' · ');
  return `<div class="sf-card sf-wide"><h3>Daily summary · ${esc(d.session)}</h3>
   <div class="sf-tiles"><div><span>Scans (intraday / swing)</span><b>${d.scans?.intraday??0} / ${d.scans?.swing??0}</b></div><div><span>Opened / closed</span><b>${d.opened} / ${d.closed}</b></div><div><span>Open now</span><b>${d.open_overnight}</b></div><div><span>Net P&amp;L</span><b class="${cls(d.net)}">${srs(d.net)}</b></div><div><span>Unresolved</span><b>${(d.unresolved||[]).length}</b></div></div>
   ${rows?`<div class="sf-scroll"><table class="sf-t sf-narrow"><thead><tr><th>Mode:direction</th><th>Trades</th><th>Wins</th><th>Gross</th><th>Costs</th><th>Net</th></tr></thead><tbody>${rows}</tbody></table></div>`:''}
   <p class="sf-note">${reg?'Market labels through the day: '+reg+'<br>':''}${(d.top_no_trade_reasons||[]).length?'Most common no-trade reasons: '+d.top_no_trade_reasons.map(([k,n])=>`${n}× ${esc(k)}`).join('; '):''}</p></div>`;
}
function breakoutCard(cands,rejected){
  const row=(x,status,why)=>{const m=x.metrics||{}, i=x.inputs||{};
    return `<tr><td><b>${esc(x.symbol)}</b></td><td>${dir(x.direction)}</td><td>${res(status)}</td><td>${n(m.close)}</td><td>${n(m.vwap)}</td><td>${n(m.or_high)} / ${n(m.or_low)}</td><td>${pc(m.breakout_distance_pct)}</td><td class="${cls(m.return_pct)}">${pc(m.return_pct)}</td><td>${pc(m.nifty_return_pct)}</td><td class="${cls(m.excess_return_pct)}">${pc(m.excess_return_pct)}</td><td>${rs(m.turnover_rs)}</td><td class="sf-why">${i.bar_ts?'bar '+t5(i.bar_ts)+', window from '+t5(i.window_start):''}</td><td class="sf-why">${esc(why)}</td></tr>`;};
  const rows=cands.map(c=>row(c,'PASS',c.rank_reason||'')).concat(rejected.map(r=>row(r,r.status||'FAIL',r.reason)));
  return `<div class="sf-card sf-wide"><h3>Opening-range breakout screen</h3><div class="sf-scroll"><table class="sf-t"><thead><tr><th>Symbol</th><th>Dir</th><th>Result</th><th>Close</th><th>VWAP</th><th>Open high / low</th><th>Breakout</th><th>Return</th><th>NIFTY</th><th>Excess</th><th>Turnover</th><th>Inputs</th><th>Why</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
   <p class="sf-note">Each stock's own 09:15–09:30 high/low (frozen for the session), its session VWAP from 1-minute traded value, and returns over the same window as NIFTY 50. Closes exactly at VWAP or on the range edge fail; wicks do not count.</p></div>`;
}
function regimeCard(reg,ev){
  const m=ev.metrics||{}, lv=ev.levels||{};
  const shown=reg.regime==='SIDEWAYS'&&ev.readiness==='READY'?'NEUTRAL':reg.regime;
  const head=`<h3>Market regime ${badge(shown||'—',shown==='BULLISH'?'ok':shown==='BEARISH'?'bad':'warn')} ${ev.readiness&&ev.readiness!=='READY'?badge(ev.readiness,'warn'):''}</h3>
    <p>Permitted: ${(reg.permitted||[]).map(dir).join(' ')||badge('no new entries','warn')}</p>`;
  let rows;
  if(ev.method==='opening_range_fib_vwap'){
    const src=ev.vwap_source||{};
    rows=[['Opening range',lv.start?`${t5(lv.start)}–${t5(lv.end)} IST${lv.frozen?' · frozen':''}`:'—'],
      ['OR high / low',`${n(m.or_high??lv.or_high)} / ${n(m.or_low??lv.or_low)}`],
      ['Fib 0.380 / 0.500 / 0.618',`${n(m.fib_038??lv.fib_038)} / ${n(m.fib_050??lv.fib_050)} / ${n(m.fib_0618??lv.fib_0618)}`],
      ['NIFTY 50 close',n(m.nifty_close)+(ev.confirm_bar?` <span class="sf-why">bar ${t5(ev.confirm_bar.start)}–${t5(ev.confirm_bar.end)}</span>`:'')],
      ['VWAP source',`${esc(src.label||src.symbol||'—')} ${src.kind==='proxy'?badge('proxy','warn'):''}`],
      ['Source close / VWAP',`${n(m.vwap_source_close)} / ${n(m.vwap)}`]];
  }else{
    rows=[[`${ev.lookback_sessions}-session return`,pc(m.return_pct)+` <span class="sf-why">threshold ±${n(Number(ev.threshold)*100,2)}%</span>`],
      ['Baseline close',`${n(m.baseline_close)} (${esc(m.baseline_session||'—')})`],['Latest close',`${n(m.latest_close)} (${esc(m.latest_session||'—')})`],
      ['High: latest / prior',`${n(m.latest_high)} / ${n(m.prior_high)}`],['Low: latest / prior',`${n(m.latest_low)} / ${n(m.prior_low)}`]];
  }
  const checks=(ev.checks||[]).map(c=>[c.name,`${badge(c.result,/ABOVE|UP|YES/.test(c.result)?'ok':/BELOW|DOWN/.test(c.result)?'bad':'n')} <span class="sf-why">${esc(c.detail)}</span>`]);
  return `<div class="sf-card">${head}${kv(rows.concat(checks))}<p class="sf-note">${esc(ev.reason||(reg.reasons||[]).join('; '))}${ev.vwap_source?.note?'<br>'+esc(ev.vwap_source.note):''}</p></div>`;
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
  return summaryCard(s.daily_summary)+`<div class="sf-card sf-wide"><h3>Futures paper journal ${badge(SRCKIND==='demo'?'FIXTURE':(s.label||'PAPER'),'warn')}</h3>
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

/* ------------------------------------------------------------ strategy health */
// From the worker snapshot's strategy_health block (sf_api.strategy_health), written
// by the daily evaluation (sf_daily.py). Rule-based system: no learned model.
const hst=s=>badge(s||'—',s==='OK'||s==='DONE'?'ok':s==='FAIL'||s==='DRIFT'||s==='DETERIORATED'?'bad':s==='WARN'||s==='DEFERRED'||s==='INSUFFICIENT'?'warn':'n');
function mrow(k,m){
  if(!m) return '';
  const u=m.undefined||{}, cell=(key,f)=>m[key]==null?`<td title="${esc(u[key]||'')}">—</td>`:`<td>${f(m[key])}</td>`;
  return `<tr><th>${esc(k)}</th><td>${m.settled}</td>${cell('net_pnl',v=>`<span class="${cls(v)}">${srs(v)}</span>`)}${cell('expectancy',v=>srs(v))}
    ${cell('win_rate',v=>Math.round(v*100)+'%')}${cell('avg_win',v=>srs(v))}${cell('avg_loss',v=>srs(v))}${cell('profit_factor',v=>v.toFixed(2))}
    <td>${rs(m.max_drawdown)}</td><td>${rs(m.costs)}</td>${cell('ci80',v=>`${srs(v[0])} … ${srs(v[1])}`)}<td>${m.excluded}/${m.unresolved}</td></tr>`;
}
function healthView(s){
  const h=s.strategy_health;
  if(!h||h.available===false) return empty(h?.reason||'Strategy health appears after the first daily evaluation (16:10 IST on trading days).');
  const L=h.latest||{}, steps=L, m=(L.metrics||{}).cumulative||{}, sess=(L.metrics||{}).session||{};
  const promo=L.promotion||{}, exp=L.experiments||{}, drift=L.drift||{}, data=L.data||{}, rec=L.reconcile||{};
  const findings=[...(rec.champion||[]),...(data.findings||[]),...(L.compliance||[])];
  const fails=findings.filter(f=>f.status==='FAIL');
  const cov=data.coverage||{};
  const prof=Object.entries(h.profiles||{}).map(([k,p])=>`<tr><th>${esc(k)}</th><td>${esc(p.kind)}</td><td>${esc(p.plan_version||'—')}</td><td>${esc(p.regime||'—')}</td><td>${esc(p.first_bar||'ohlc (v1)')}</td></tr>`).join('');
  const chal=(h.challengers||[]).filter(c=>c.status==='shadow');
  const assessed=Object.fromEntries((promo.challengers||[]).map(c=>[c.id,c]));
  const minS=h.policy?.promotion?.min_shadow_sessions, minT=h.policy?.promotion?.min_shadow_settled_trades;
  return `<div class="sf-grid sf-g3">
   <div class="sf-card"><h3>Active strategy ${badge('v'+h.active_version,'info')} ${badge('rule-based','n')}</h3>
     ${kv([['Config hash',esc(h.config_hash||'—')],['Label',esc(h.label||'—')],['Last known good',h.last_known_good?'v'+h.last_known_good:'—'],
          ['Automatic promotion',h.auto_promotion?badge('ON','ok'):badge('PAUSED','warn')],
          ['New entries',Object.entries(h.entries_paused||{}).map(([k,v])=>`${esc(k)} ${v?badge('paused','warn'):badge('on','ok')}`).join(' ')]])}</div>
   <div class="sf-card"><h3>Daily evaluation</h3>
     ${kv([['Latest session',esc(L.session||'—')],['Status',hst(L.status)],['Decision',`<b>${esc(L.decision||'—')}</b>`],
          ['Finished',dt(L.finished_at)],['Next run',dt(h.next_run)+' IST'],['Governance',esc(L.governance||'—')]])}</div>
   <div class="sf-card"><h3>Data and reconciliation ${fails.length?badge(fails.length+' failing','bad'):badge('clean','ok')}</h3>
     ${kv(Object.entries(cov).map(([t,c])=>[t,`${c.symbols} symbols · ${c.median_minutes} min · zero-vol ${Math.round((c.zero_volume_share||0)*1000)/10}% · first-bar defect ${Math.round((c.first_bar_open_at_extreme_share||0)*100)}%`])
        .concat([['Unresolved exits',(rec.unresolved||[]).length?(rec.unresolved||[]).map(esc).join(', '):'none'],['Quarantined',(data.quarantine||[]).length?(data.quarantine).map(esc).join(', '):'none']]))}</div></div>
  <div class="sf-card"><h3>Results by mode and direction ${badge('settled trades only','n')}</h3>
   <div class="sf-scroll"><table class="sf-t"><thead><tr><th></th><th>Trades</th><th>Net</th><th>Expectancy</th><th>Win rate</th><th>Avg win</th><th>Avg loss</th><th>Profit factor</th><th>Max DD</th><th>Costs</th><th>80% interval / trade</th><th>Excl./unres.</th></tr></thead>
   <tbody>${['intraday:LONG','intraday:SHORT','swing:LONG','swing:SHORT','intraday','swing','portfolio'].map(k=>mrow(k,m[k])).join('')}</tbody></table></div>
   <p class="sf-note">Cumulative since paper trading began, net of estimated costs. “—” means the metric is undefined for this sample (hover for why). Intervals are seeded bootstrap estimates and need 5+ trades. Session ${esc(L.session||'')}: ${sess.portfolio?`${sess.portfolio.settled} trades, ${srs(sess.portfolio.net_pnl)}`:'—'}.</p></div>
  <div class="sf-grid sf-g2">
   <div class="sf-card"><h3>Drift and deterioration</h3>
     ${kv([['Data drift ('+esc(drift.data?.method||'')+')',hst(drift.data?.status)+' '+esc(drift.data?.detail||(drift.data?.flagged||[]).join(', '))],
          ...Object.entries(drift.performance||{}).map(([k,v])=>[`${k} (${esc(v.method)})`,hst(v.status)+' '+esc(v.detail||(v.ci80?`80% interval ${srs(v.ci80[0])} … ${srs(v.ci80[1])}`:''))])])}
     <h3 style="margin-top:12px">Experiment cycle</h3>
     ${kv([['Eligible',exp.eligible?badge('yes','ok'):badge('no','n')],['Why',esc((exp.reasons||[]).join('; ')||'—')],['Ran',exp.ran?esc(exp.cycle):'no'],
          ['Candidates',(exp.candidates||[]).map(c=>`${esc(c.path)} = ${esc(c.value)} ${c.qualified?badge('to shadow','ok'):badge('rejected','n')}`).join('<br>')||'—']])}</div>
   <div class="sf-card"><h3>Challengers in shadow ${badge(chal.length,'n')}</h3>
     ${chal.length?chal.map(c=>{const a=assessed[c.id]||{};const ss=a.shadow_sessions||0;
       return `<div class="sf-ch"><b>${esc(c.id)}</b> ${c.kind==='last_known_good'?badge('last known good','info'):''} ${badge(a.decision||'gathering','warn')}
        <div class="sf-sub">${esc(c.reason||'')} · since ${esc(c.added_on)}</div>
        <div class="sf-bar"><span style="width:${Math.min(100,ss/(minS||1)*100)}%"></span></div>
        <div class="sf-sub">${ss}/${minS} shadow sessions · ${(a.metrics||{}).settled??0}/${minT} trades</div>
        <ul class="sf-req">${(a.requirements||[]).map(r=>`<li>${r.met?'✓':'✗'} ${esc(r.name)} <span class="sf-tm">${esc(JSON.stringify(r.value))}</span></li>`).join('')}</ul></div>`;}).join('')
       :empty('No challenger is in shadow. Candidates are generated only when an experiment cycle is eligible.')}
     ${promo.action?`<p class="sf-note"><b>Action:</b> ${esc(promo.action.type)} ${esc(promo.action.reason||'')}</p>`:''}
     ${promo.rollback?`<p class="sf-note"><b>Rollback watch:</b> ${esc(promo.rollback.decision)} ${esc((promo.rollback.triggers||[]).join('; '))}</p>`:''}</div></div>
  <div class="sf-grid sf-g2">
   <div class="sf-card"><h3>Promotion requirements ${h.policy?.promotion?.provisional?badge('provisional','warn'):''}</h3>
     ${kv([['Forward shadow sessions','≥ '+minS],['Settled shadow trades','≥ '+minT],['Paired daily P&L vs champion',`${Math.round((h.policy?.promotion?.ci_level||0)*100)}% lower bound > 0 (stricter when more candidates are compared)`],
          ['Cost stress',`+${h.policy?.promotion?.stress_extra_ticks_per_side} tick per side, still net positive`],['Drawdown',`≤ ${h.policy?.promotion?.max_drawdown_vs_champion}× champion and ≤ ${rs(h.policy?.promotion?.max_drawdown_abs_rs)}`],
          ['Stability',`${h.policy?.promotion?.min_positive_subperiods} of ${h.policy?.promotion?.subperiods} sub-periods ahead`],['Rejected if',esc((h.policy?.promotion?.reject_if||[]).join(', '))],
          ['Evidence needed',esc(h.policy?.promotion?.needs||'')]])}
     <p class="sf-note">${esc(h.policy?.note||'')}</p></div>
   <div class="sf-card"><h3>Profiles</h3><div class="sf-scroll"><table class="sf-t sf-narrow"><thead><tr><th></th><th>Kind</th><th>Exit plan</th><th>Regime</th><th>Opening range first bar</th></tr></thead><tbody>${prof}</tbody></table></div>
     <h3 style="margin-top:12px">Search space ${badge('automatic changes only here','n')}</h3>
     <ul class="sf-req">${Object.entries(h.search_space?.params||{}).map(([k,r])=>`<li>${esc(k.replace(/^profiles[.·]/,'').replace(/·/g,'.'))} <span class="sf-tm">${r.choices?esc(r.choices.join(' / ')):`${r.min} … ${r.max}`}</span></li>`).join('')}</ul>
     <p class="sf-note">${esc(h.search_space?.description||'')}</p></div></div>
  <div class="sf-grid sf-g2">
   <div class="sf-card"><h3>Decision history</h3><div class="sf-scroll"><table class="sf-t sf-narrow"><thead><tr><th>Session</th><th>Status</th><th>Version</th><th>Decision</th></tr></thead><tbody>
     ${(h.history||[]).slice().reverse().map(x=>`<tr><td>${esc(x.session)}</td><td>${hst(x.status)}</td><td>${x.active_version?'v'+x.active_version:'—'}</td><td class="sf-why">${esc(x.decision||x.reason||'')}</td></tr>`).join('')||`<tr><td colspan="4">${empty('No evaluations yet.')}</td></tr>`}</tbody></table></div></div>
   <div class="sf-card"><h3>Version history</h3><div class="sf-scroll"><table class="sf-t sf-narrow"><thead><tr><th>Version</th><th>Created</th><th>By</th><th>Label</th></tr></thead><tbody>
     ${(h.versions||[]).slice().reverse().map(v=>`<tr><td>v${v.version} ${v.active?badge('active','ok'):''} ${v.last_known_good?badge('last known good','info'):''}<div class="sf-tm">${esc(v.hash)}</div></td><td>${dt(v.created)}</td><td>${esc(v.actor)}</td><td class="sf-why">${esc(v.label||'')}</td></tr>`).join('')}</tbody></table></div>
     <p class="sf-note">Experiment records: data/futures/experiments/registry.jsonl · decisions: data/futures/eval/&lt;session&gt;.json and audit.jsonl on the server.</p></div></div>
  ${fails.length?`<div class="sf-card"><h3>Failing checks</h3><ul class="sf-req">${fails.map(f=>`<li>✗ <b>${esc(f.check)}</b> ${esc(f.detail)}</li>`).join('')}</ul></div>`:''}`;
}

/* ------------------------------------------------------------ market */
// Sector and mover bar charts for the stock futures, from Market Pulse's stock_oi rows
// (/live/pulse on Firebase, /api/pulse on the API page). Nothing here trades.
async function loadPulse(){
  try{
    if(typeof SRC!=='undefined'&&SRC.mode==='firebase'){ const v=SRC.live?.pulse; if(v?.stock_oi?.available){PK='live';return v;} }
    else if(typeof get==='function'){
      // /api/pulse does real work on the server; ask at most once a minute.
      if(Date.now()-PCACHE.at<60000&&PCACHE.v) return PCACHE.v;
      const v=await get('/api/pulse'); PCACHE.at=Date.now(); PCACHE.v=v?.stock_oi?.available?v:null;
      if(PCACHE.v){PK='api';return v;}
    }
  }catch(e){}
  try{ const r=await fetch('/futures-market-demo.json',{cache:'no-cache'}); if(r.ok){PK='demo';return await r.json();} }catch(e){}
  return null;
}
const MPAIRS=[
 [{id:'g',t:'Top gainers',note:'% change vs yesterday’s close',k:'price_chg_pct',f:r=>r.price_chg_pct>0,d:-1},
  {id:'l',t:'Top losers',note:'% change vs yesterday’s close',k:'price_chg_pct',f:r=>r.price_chg_pct<0,d:1}],
 [{id:'ig',t:'Intraday gainers',note:'% change since the open (09:15 minute close)',k:'intraday_pct',f:r=>r.intraday_pct>0,d:-1},
  {id:'il',t:'Intraday losers',note:'% change since the open (09:15 minute close)',k:'intraday_pct',f:r=>r.intraday_pct<0,d:1}],
 [{id:'gu',t:'Top gap up',note:'Open (09:15 minute close) vs yesterday’s close',k:'gap_pct',f:r=>r.gap_pct>0,d:-1},
  {id:'gd',t:'Top gap down',note:'Open (09:15 minute close) vs yesterday’s close',k:'gap_pct',f:r=>r.gap_pct<0,d:1}],
 [{id:'lb',t:'Long buildup',note:'Price up, OI up. Bar = OI change since the open; price change beside the name',k:'oi_chg_pct',f:r=>r.read==='long buildup',d:-1,lab:1},
  {id:'sb',t:'Short buildup',note:'Price down, OI up. Bar = OI change since the open; price change beside the name',k:'oi_chg_pct',f:r=>r.read==='short buildup',d:-1,lab:1}],
 [{id:'sc',t:'Short covering',note:'Price up, OI down. Bar = OI change since the open; price change beside the name',k:'oi_chg_pct',f:r=>r.read==='short covering',d:1,lab:1},
  {id:'lu',t:'Long unwinding',note:'Price down, OI down. Bar = OI change since the open; price change beside the name',k:'oi_chg_pct',f:r=>r.read==='long unwinding',d:1,lab:1}],
 [{id:'h52',t:'52-week high',note:'Within 5% of the stock’s 52-week high (0% = at the high)',k:'from_high_pct',f:r=>fin(r.from_high_pct)&&r.from_high_pct>=-5,d:-1,c:'nu',need:'52-week figures appear once the daily NSE history has been downloaded.'},
  {id:'l52',t:'52-week low',note:'Within 5% of the stock’s 52-week low (0% = at the low)',k:'from_low_pct',f:r=>fin(r.from_low_pct)&&r.from_low_pct<=5,d:1,c:'nu',need:'52-week figures appear once the daily NSE history has been downloaded.'}]];
const MJUMP=[['mk-sec','Sectors'],['mk-g','Gainers / losers'],['mk-ig','Intraday'],['mk-gu','Gap'],['mk-lb','Buildup'],['mk-sc','Covering / unwinding'],['mk-h52','52-week']];
const sortOf=(id,d)=>MK.sort[id]??d;
const sorter=(k,d)=>(a,b)=>d<0?(b[k]-a[k]):(a[k]-b[k]);
const sortBtn=(id,d)=>`<button class="sf-btn sf-sort" data-msort="${id}" data-d="${d}" aria-label="Sort">${sortOf(id,d)<0?'High → low':'Low → high'}</button>`;
function hbars(rows,k,o={}){
  if(!rows.length) return empty(o.none||'None right now.');
  const m=Math.max(...rows.map(r=>Math.abs(r[k]||0)),0.01);
  return `<div class="sf-hb">${rows.map(r=>{const v=r[k]||0,w=Math.abs(v)/m*50,u=v>=0;
    const att=o.click?` data-msec="${esc(r.symbol)}" role="button" tabindex="0" title="${esc(`${r.symbol}: ${r.n} futures. Open the sector`)}"`
      :` data-mstk="${esc(r.symbol)}" data-of="${esc(r.sector||'Others')}" role="button" tabindex="0" title="${esc(`${r.symbol}: open its chart`)}"`;
    return `<div class="sf-hr sf-click${!o.click&&MK.stock===r.symbol?' sf-on':''}"${att}>
      <span class="sf-hl">${o.click?'▸ ':''}${esc(r.symbol)}${o.lab?` <i class="${cls(r.price_chg_pct)}">${pc(r.price_chg_pct)}</i>`:''}</span>
      <span class="sf-ht"><span class="sf-hm"></span><span class="sf-hbar ${o.c||(u?'sf-hup':'sf-hdn')}" style="${u?'left':'right'}:50%;width:${w.toFixed(1)}%"></span></span>
      <span class="sf-hv ${o.c?'':cls(v)}">${pc(v)}</span></div>`;}).join('')}</div>`;
}
function moverCard(c,rows){
  const has=rows.some(r=>fin(r[c.k]));
  const all=has?rows.filter(r=>fin(r[c.k])&&c.f(r)):[];
  const d=sortOf(c.id,c.d), lim=MK.n[c.id]||10;
  const shown=all.slice().sort(sorter(c.k,c.d)).slice(0,lim).sort(sorter(c.k,d));
  return `<div class="sf-card" id="mk-${c.id}"><h3>${esc(c.t)} <span class="sf-b">${all.length}</span><span style="flex:1"></span>${has?sortBtn(c.id,c.d):''}</h3>
    <div class="sf-sub">${esc(c.note)}</div>
    ${has?hbars(shown,c.k,c):empty(c.need||'Appears after the next server update.')}
    ${all.length>10?`<button class="sf-btn sf-more" data-mmore="${c.id}">${lim>10?'Show less':`Show more (${Math.min(25,all.length)})`}</button>`:''}</div>`;
}
function sectorRows(rows){
  const by={}; rows.forEach(r=>(by[r.sector||'Others']??=[]).push(r));
  return Object.entries(by).map(([s,xs])=>({symbol:s,n:xs.length,price_chg_pct:Math.round(xs.reduce((a,r)=>a+r.price_chg_pct,0)/xs.length*100)/100,xs}));
}
function marketView(p){
  if(!p) return empty('Market data appears once Market Pulse has stock-futures data for the session.');
  const rows=(p.stock_oi?.rows||[]).filter(r=>fin(r.price_chg_pct));
  const secs=sectorRows(rows);
  const demo=PK==='demo'?`<div class="sf-banner sf-warnb"><b>FIXTURE DEMO</b> · synthetic numbers, shown because no live stock-futures data is available here.</div>`:'';
  const one=MK.sector&&secs.find(x=>x.symbol===MK.sector);
  if(one) return demo+sectorPage(one,p);
  MK.sector=null;
  const sd=sortOf('sec',-1); secs.sort(sorter('price_chg_pct',sd));
  return demo+`<div class="sf-jump">${MJUMP.map(([id,l])=>`<button class="sf-b" data-mjump="${id}">${l}</button>`).join('')}</div>
   <div class="sf-card" id="mk-sec"><h3>Sectors · stock futures <span class="sf-b">${secs.length}</span><span style="flex:1"></span>${sortBtn('sec',-1)}</h3>
    <div class="sf-sub">Average futures % change vs yesterday’s close, equal weight per stock. Click a sector to open its futures; click any stock for its chart. Session ${esc(p.session||'—')}.</div>
    ${hbars(secs,'price_chg_pct',{click:1})}</div>
   <div class="sf-grid sf-g3m">${MPAIRS.flat().map(c=>moverCard(c,rows)).join('')}</div>
   <p class="sf-note">Nearest-month stock futures. “Open” is the close of the 09:15 minute. OI change is measured from today’s first bar. 52-week range is the stock’s cash-market high and low from NSE’s daily files, not adjusted for splits or bonuses. Research and paper trading only.</p>`;
}
function sectorPage(s,p){
  const d=sortOf('one',-1), lim=MK.n.one||10;
  const all=s.xs.slice().sort(sorter('price_chg_pct',d));
  const pick=s.xs.find(r=>r.symbol===MK.stock);
  return `<div class="sf-card" id="mk-one"><h3><button class="sf-btn sf-sort" data-mback>← Back to market</button>${esc(s.symbol)} <span class="sf-b">${s.n} futures</span> <span class="sf-b ${s.price_chg_pct>=0?'sf-ok':'sf-bad'}">avg ${pc(s.price_chg_pct)}</span><span style="flex:1"></span>${sortBtn('one',-1)}</h3>
    <div class="sf-sub">Futures % change vs yesterday’s close. Click a stock for its 15-minute candles. Session ${esc(p.session||'—')}.</div>
    <div class="sf-split"><div>${hbars(all.slice(0,lim),'price_chg_pct')}
      ${all.length>10?`<button class="sf-btn sf-more" data-mmore="one">${lim>10?'View less':`View more (${all.length})`}</button>`:''}</div>
      <aside class="sf-panel" id="mk-panel">${pick?stockPanel(pick,p):`<div class="sf-sub" style="margin:0">Click a stock to see its chart and numbers here.</div>`}</aside></div></div>`;
}
const CC={};                         // symbol -> {at, v} candle cache
function candlesFor(r,p){
  const hit=CC[r.symbol]; if(hit&&(Date.now()-hit.at<60000||hit.v==='loading')) return hit.v;
  CC[r.symbol]={at:Date.now(),v:'loading'};
  (async()=>{let v=null;
    try{
      if(PK==='demo') v=demoCandles(r);
      else if(typeof SRC!=='undefined'&&SRC.mode==='firebase'){const x=await fetch(`${SRC.db}/candles/fut/${encodeURIComponent(r.symbol)}.json`,{cache:'no-store'}); v=x.ok?await x.json():null;}
      else if(typeof get==='function') v=await get('/api/pulse/candles?symbol='+encodeURIComponent(r.symbol));
    }catch(e){v=null;}
    CC[r.symbol]={at:Date.now(),v:v&&v.c?.length?v:null}; if(MK.stock===r.symbol) paint(true);})();
  return 'loading';
}
function demoCandles(r){
  let x=r.open||100,seed=[...r.symbol].reduce((a,c)=>a+c.charCodeAt(0),0);const rnd=()=>(seed=(seed*9301+49297)%233280)/233280;
  const o=[],h=[],l=[],c=[],vw=[],t=[];let pv=0,vv=0;const step=((r.last||x)-x)/25;
  for(let i=0;i<25;i++){const cl=x+step+(rnd()-.5)*x*.004,hi=Math.max(x,cl)+rnd()*x*.002,lo=Math.min(x,cl)-rnd()*x*.002,v=50+rnd()*100;
    pv+=(hi+lo+cl)/3*v;vv+=v;o.push(x);h.push(hi);l.push(lo);c.push(cl);vw.push(pv/vv);const m=555+i*15;t.push(`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`);x=cl;}
  return {t,o,h,l,c,vwap:vw,minutes:15};
}
function candleSvg(v){
  const N=v.c.length,W=300,H=170,top=6,bot=20,vw=(v.vwap||[]).filter(fin);
  const hi=Math.max(...v.h,...vw),lo=Math.min(...v.l,...vw),span=(hi-lo)||1;
  const y=x=>top+(hi-x)/span*(H-top-bot),cw=(W-44)/Math.max(N,25);
  const ticks=v.t.map((t,i)=>[t,i]).filter(([t])=>/^(09:15|11:15|13:15|15:15)$/.test(t));
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="15-minute candles with VWAP">
    ${v.c.map((c,i)=>{const o=v.o[i],u=c>=o,col=u?'var(--sf-up)':'var(--sf-down)',X=i*cw+cw/2;
      return `<line x1="${X}" x2="${X}" y1="${y(v.h[i]).toFixed(1)}" y2="${y(v.l[i]).toFixed(1)}" stroke="${col}"/><rect x="${(X-cw*.32).toFixed(1)}" y="${y(Math.max(o,c)).toFixed(1)}" width="${(cw*.64).toFixed(1)}" height="${Math.max(1,Math.abs(y(o)-y(c))).toFixed(1)}" fill="${col}"><title>${v.t[i]}  O ${n(o)} H ${n(v.h[i])} L ${n(v.l[i])} C ${n(c)}</title></rect>`;}).join('')}
    <polyline fill="none" stroke="var(--sf-info)" stroke-width="1.5" stroke-dasharray="4 3" points="${(v.vwap||[]).map((w,i)=>fin(w)?`${(i*cw+cw/2).toFixed(1)},${y(w).toFixed(1)}`:'').filter(Boolean).join(' ')}"/>
    ${ticks.map(([t,i])=>`<text x="${(i*cw+cw/2).toFixed(1)}" y="${H-5}" font-size="10" text-anchor="middle" fill="var(--sf-muted)">${t}</text>`).join('')}
    <text x="${W-2}" y="12" font-size="10" text-anchor="end" fill="var(--sf-muted)">${n(hi)}</text><text x="${W-2}" y="${H-bot-2}" font-size="10" text-anchor="end" fill="var(--sf-muted)">${n(lo)}</text></svg>`;
}
function stockPanel(r,p){
  const v=candlesFor(r,p);
  const chart=v==='loading'?`<div class="sf-sub">Loading candles…</div>`:v?candleSvg(v):`<div class="sf-sub">No candles published for ${esc(r.symbol)} yet.</div>`;
  const read=r.read||'—', rk=read==='long buildup'||read==='short covering'?'sf-ok':read==='neutral'?'':'sf-bad';
  const kv=[['Last',n(r.last)],['Open',n(r.open)],['Gap',`<span class="${cls(r.gap_pct)}">${pc(r.gap_pct)}</span>`],['Day',`<span class="${cls(r.price_chg_pct)}">${pc(r.price_chg_pct)}</span>`],
    ['Intraday',`<span class="${cls(r.intraday_pct)}">${pc(r.intraday_pct)}</span>`],['OI',fin(r.oi)?(r.oi/1e5).toLocaleString('en-IN',{maximumFractionDigits:1})+' L':'—'],['OI change',`<span class="${cls(r.oi_chg_pct)}">${pc(r.oi_chg_pct)}</span>`],['VWAP',v&&v!=='loading'?n([...(v.vwap||[])].reverse().find(fin)):'—']];
  return `<div class="sf-ph"><b>${esc(r.symbol)}</b> <span class="sf-b ${rk}">${esc(read)}</span><span style="flex:1"></span><button class="sf-btn sf-x" data-mclose aria-label="Close">✕</button></div>
    ${chart}<div class="sf-legend"><span class="sf-vw"></span> VWAP · 15-minute candles, nearest-month future</div>
    <div class="sf-kv4">${kv.map(([k,x])=>`<div><span>${k}</span><b>${x}</b></div>`).join('')}</div>`;
}
function bindMarket(){
  const act=(b,go)=>{b.onclick=go; b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}};};
  qa('[data-msort]',sec).forEach(b=>b.onclick=()=>{const id=b.dataset.msort;MK.sort[id]=-sortOf(id,+b.dataset.d);paint(true);});
  qa('[data-mmore]',sec).forEach(b=>b.onclick=()=>{const id=b.dataset.mmore;MK.n[id]=(MK.n[id]||10)>10?10:(id==='one'?999:25);paint(true);});
  qa('[data-msec]',sec).forEach(b=>act(b,()=>{MK.sector=b.dataset.msec;MK.stock=null;MK.n.one=10;paint(true);sec.scrollIntoView({block:'start'});}));
  qa('[data-mstk]',sec).forEach(b=>act(b,()=>{const was=MK.sector;MK.sector=b.dataset.of;MK.stock=b.dataset.mstk;paint(true);
    if(was!==MK.sector) sec.scrollIntoView({block:'start'}); else if(innerWidth<900) q('#mk-panel')?.scrollIntoView({behavior:'smooth',block:'nearest'});}));
  qa('[data-mback]',sec).forEach(b=>b.onclick=()=>{MK.sector=null;MK.stock=null;paint(true);});
  qa('[data-mclose]',sec).forEach(b=>b.onclick=()=>{MK.stock=null;paint(true);});
  qa('[data-mjump]',sec).forEach(b=>b.onclick=()=>q('#'+b.dataset.mjump)?.scrollIntoView({behavior:'smooth',block:'start'}));
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

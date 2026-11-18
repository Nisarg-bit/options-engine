/* Options page (Phase E, Nov 2026).
   Reads /live/options_view (or /api/options_view): for NIFTY and BANKNIFTY,
   a rule-based Up / Sideways / Down view, the volatility state, the inputs
   behind it, and up to three loss-capped strategies from the builder's
   catalogue, sized on the Rs 20 lakh paper book. Styles come from pulse.css. */
(function(){
'use strict';
const q=(s,r=document)=>r.querySelector(s), qa=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fin=v=>v!=null&&isFinite(v);
const n=(v,d=0)=>fin(v)?Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const rs=v=>fin(v)?(v<0?'−':'')+'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN'):'—';
const srs=v=>fin(v)?(v>0?'+':'')+rs(v):'—';
const pct=(v,d=0)=>fin(v)?(v*100).toFixed(d)+'%':'—';
const cls=v=>!fin(v)||v===0?'':v>0?'up':'down';
const info=t=>`<span class="pl-i" tabindex="0" title="${esc(t)}" aria-label="${esc(t)}">i</span>`;
const empty=t=>`<div class="pl-empty">${esc(t)}</div>`;

const main=q('main'); if(!main) return;
const sec=document.createElement('section'); sec.id='opts';
sec.innerHTML=`<div class="pl-head"><div><h1>Options</h1><div class="pl-sub" id="op-sub">Loading…</div></div>
 <div class="pl-tabs" role="tablist" aria-label="Index">
  <button role="tab" data-u="NIFTY" aria-selected="true">NIFTY</button>
  <button role="tab" data-u="BANKNIFTY" aria-selected="false">BANKNIFTY</button></div></div>
 <div id="op-body"></div><div class="pl-foot" id="op-foot"></div>`;
const pulse=q('#pulse'); pulse?pulse.after(sec):main.prepend(sec);
const btn=document.createElement('button'); btn.dataset.t='opts'; btn.textContent='Options'; btn.setAttribute('aria-label','Options');
const pb=q('nav button[data-t="pulse"]'); pb?pb.after(btn):(q('nav .chapters')||q('nav')).prepend(btn);
function show(){
  qa('nav button[data-t]').forEach(x=>{x.classList.toggle('on',x===btn);x.setAttribute('aria-current',x===btn?'page':'false');});
  qa('main section').forEach(s=>s.classList.toggle('on',s===sec));
  history.replaceState(null,'','#opts'); paint(true);
}
btn.addEventListener('click',show);
if(location.hash==='#opts') show();

let UNDX='NIFTY', SIG='', P=null;
qa('.pl-tabs button',sec).forEach(b=>b.onclick=()=>{UNDX=b.dataset.u;qa('.pl-tabs button',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});

async function load(){
  try{
    if(typeof SRC!=='undefined'&&SRC.mode==='firebase') return SRC.live?.options_view||null;
    if(typeof get==='function') return await get('/api/options_view');
  }catch(e){ return {error:e.message}; }
  return null;
}
async function loadBook(){
  try{
    if(typeof SRC!=='undefined'&&SRC.mode==='firebase') return SRC.live?.paper_book||null;
    if(typeof get==='function') return await get('/api/paper_book');
  }catch(e){ return null; }
  return null;
}
let BOOK='intraday', PBK=null;
async function paint(force){
  if(!sec.classList.contains('on')) return;
  const p=await load(), pb=await loadBook(), sig=JSON.stringify(p)+JSON.stringify(pb)+UNDX+BOOK; PBK=pb;
  if(!force&&sig===SIG) return; SIG=sig; P=p;
  const body=q('#op-body');
  if(!p){ q('#op-sub').textContent='Waiting for the first update'; body.innerHTML=empty('No options view yet. The server publishes it every minute once Phase E is deployed.'); return; }
  if(p.error){ body.innerHTML=empty('Options view unavailable: '+p.error); return; }
  const g=p.generated?new Date(p.generated):null;
  q('#op-sub').textContent=`Session ${p.session||'—'} · updated ${g?g.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'})+' IST':'—'} · paper book ${rs(p.capital)} · ${pct(p.risk_pct)} risk per trade`;
  q('#op-foot').innerHTML=(p.notes||[]).map(esc).join('<br>');
  const v=(p.indices||{})[UNDX];
  body.innerHTML=!v?empty('No data for '+UNDX):!v.available?empty(UNDX+': '+(v.reason||'not available yet')):render(v,p);
  body.insertAdjacentHTML('beforeend',bookView(PBK));
  qa('[data-open]',body).forEach(b=>b.onclick=()=>openInBuilder(b.dataset.open,b.dataset.group));
  qa('[data-book]',body).forEach(b=>b.onclick=()=>{BOOK=b.dataset.book;paint(true);});
}
setInterval(()=>paint(false),5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)paint(false);});

const CALL={up:['Leaning up','up'],down:['Leaning down','down'],sideways:['Range-bound','flat'],unclear:['No clear view','flat']};
const VOL={low:['Low volatility','up'],normal:['Normal volatility','flat'],high:['High volatility','warn']};
function render(v,p){
  const o=v.odds, c=CALL[v.call]||CALL.unclear, vs=VOL[v.vol?.state];
  const seg=(k,lab,col)=>o[k]>0.02?`<span style="width:${o[k]*100}%;background:${col}" data-tip="${lab}: ${pct(o[k])}">${o[k]>=0.18?lab+' '+pct(o[k]):o[k]>=0.08?pct(o[k]):''}</span>`:'';
  const view=`<div class="pl-card"><div class="pl-ch">Today's view · ${esc(UNDX)} ${info('Rule-based. Five inputs give a direction score; a weak score, the index between its OI walls and a small day range give a range score. They are turned into three shares of 100. Not calibrated probabilities yet: Phase H replaces this with a trained model.')}<span class="pl-sp"></span>
     ${vs?`<span class="pl-pill ${vs[1]}">${vs[0]}</span>`:''} ${v.vol?.premium?`<span class="pl-pill flat" title="Live ATM straddle against the site's fair value">premium ${esc(v.vol.premium)}</span>`:''}</div>
    <div class="op-call ${c[1]==='flat'?'':c[1]}">${c[0]}</div>
    <div class="op-odds" role="img" aria-label="Up ${pct(o.up)}, sideways ${pct(o.sideways)}, down ${pct(o.down)}">${seg('up','Up','var(--pl-up)')}${seg('sideways','Sideways','#5F6F81')}${seg('down','Down','var(--pl-down)')}</div>
    <div class="pl-muted">${v.call==='unclear'?'No side has a clear lead (45%+). The cards below follow the leading side and are marked as a weak lead.':'Strategies below fit this view and today\'s volatility.'}</div></div>`;
  const inputs=`<div class="pl-card"><div class="pl-ch">Why ${info('Each input on a −1 (bearish) … +1 (bullish) scale, with its weight in the direction score.')}</div>
    ${v.inputs.map(x=>{const w=Math.abs(x.value)*50;return `<div class="pl-row" data-tip="${esc(x.name)}: ${x.value>0?'+':''}${x.value} · weight ${Math.round(x.weight*100)}%"><span class="l" style="width:170px">${esc(x.name)}</span><span class="pl-bar"><span style="left:${x.value<0?50-w:50}%;width:${w}%;background:var(${x.value<0?'--pl-down':'--pl-up'})"></span><span style="left:50%;width:1px;background:var(--pl-muted)"></span></span><span class="v ${cls(x.value)}">${x.value>0?'+':''}${x.value.toFixed(2)}</span></div>`;}).join('')}
    ${v.missing?.length?`<div class="pl-muted">Not available yet: ${v.missing.map(esc).join(', ')}.</div>`:''}</div>`;
  const lv=`<div class="pl-card"><div class="pl-ch">Levels and positioning</div><div class="op-kv">
    <span>Index</span><b>${n(v.last??v.spot,2)} <span class="${cls(v.chg_pct)}">${fin(v.chg_pct)?(v.chg_pct>0?'+':'')+v.chg_pct.toFixed(2)+'%':''}</span></b>
    <span>Opening range (15 min)</span><b>${v.opening_range?n(v.opening_range[0])+' – '+n(v.opening_range[1]):'—'}</b>
    <span title="Strike with the most put OI">Put wall (support)</span><b>${n(v.support)}</b>
    <span title="Strike with the most call OI">Call wall (resistance)</span><b>${n(v.resistance)}</b>
    <span>Between the walls</span><b>${v.inside_walls==null?'—':v.inside_walls?'yes':'no'}</b>
    <span title="Today's high−low as a share of a normal day's move">Day range used</span><b>${fin(v.range_used)?pct(v.range_used):'—'}</b>
    <span>PCR (OI · today's change)</span><b>${fin(v.pcr_oi)?v.pcr_oi.toFixed(2):'—'} · ${fin(v.pcr_oi_change)?v.pcr_oi_change.toFixed(2):'—'}</b>
    <span>Max pain</span><b>${n(v.max_pain)}</b>
    <span>Expiry</span><b>${esc(v.expiry||'—')}${fin(v.dte)?' · '+v.dte+' days':''}</b>
    <span>Lot size</span><b>${n(v.lot)}</b></div></div>`;
  const weak=v.call==='unclear'&&v.lean_strategies?.length;
  const list=weak?v.lean_strategies:v.strategies;
  const cards=list?.length?(weak?`<div class="pl-muted" style="margin:-4px 0 10px">Weak lead: these fit the leading side (<b>${esc(v.lean)}</b>). The paper book still trades the first one.</div>`:'')+`<div class="pl-grid pl-g3">${list.map(s=>strat(s,v)).join('')}</div>`
    :empty('No loss-capped strategy for this view is quoted on the chain right now.');
  return `<div class="pl-grid pl-g3">${view}${inputs}${lv}</div>
   <div class="pl-h2" style="margin-top:22px">Strategies that fit ${info('From a fixed playbook for this view and volatility, best fit first. Only structures with a capped loss. Priced on the live chain with the site\'s model; EV is after estimated costs.')}</div>${cards}`;
}
function strat(s,v){
  const z=s.sizing||{}, lots=z.lots||0, risk=fin(s.max_loss_rs)?Math.abs(s.max_loss_rs)*lots:null;
  const legs=(s.legs||[]).map(l=>`${l.qty>0?'Buy':'Sell'} ${Math.abs(l.qty)} × ${n(l.strike)} ${esc(l.kind)}${l.expiry&&l.expiry!==v.expiry?' ('+esc(l.expiry)+')':''} @ ${n(l.premium,2)}`).join('<br>');
  return `<div class="pl-card op-strat"><h3>${esc(s.label)}</h3><div class="pl-muted">${esc(s.why)}</div>
    <div class="op-lots"><span class="pl-big" style="font-size:28px">${lots}</span><span class="pl-muted">${lots===1?'lot':'lots'}${lots?` · ${rs(risk)} at risk`:` · ${esc(z.reason||'')}`}</span></div>
    <div class="op-kv"><span>POP</span><b>${pct(s.pop,1)}</b><span>EV after costs / lot</span><b class="${cls(s.net_ev_rs)}">${srs(s.net_ev_rs)}</b>
      <span>Max profit / lot</span><b>${fin(s.max_profit_rs)?rs(s.max_profit_rs):'—'}</b><span>Max loss / lot</span><b class="down">${rs(-Math.abs(s.max_loss_rs))}</b>
      <span>${(s.net_premium_rs||0)>=0?'Credit':'Debit'} / lot</span><b>${rs(Math.abs(s.net_premium_rs))}</b><span>Breakeven</span><b>${(s.breakevens||[]).map(b=>n(b)).join(' / ')||'—'}</b>
      <span>Margin / lot (est.)</span><b>${rs(s.margin_est)}</b><span>Lots: risk · margin</span><b>${z.by_risk??'—'} · ${z.by_margin??'—'}</b></div>
    <div class="op-legs">${legs||'—'}</div>
    ${fin(s.net_ev_rs)&&s.net_ev_rs<0?'<div class="pl-muted" style="margin-bottom:8px">Negative EV after costs: fits the view, but the price is not in your favour.</div>':''}
    <button class="pl-btn primary" data-open="${esc(s.key)}" data-group="${esc(s.group||'')}">Open in Strategy builder →</button></div>`;
}

/* The Rs 20 lakh paper book (agent step 1) */
function bookView(pb){
  const head=`<div class="pl-h2" style="margin-top:26px">Paper book · ₹20 lakh ${info('The agent’s first step. Every trading day at 10:15 IST it takes the top card for NIFTY and BANKNIFTY and logs it in two books. Intraday: closes by 15:15 the same day. Hold: can carry to expiry. Both exit at 50% of max profit or 50% of max loss. Marked on chain mid prices every minute; costs included. No real orders.')}<span class="pl-sp" style="flex:1"></span>
    <span class="pl-chips" style="margin:0">${['intraday','hold'].map(k=>`<button data-book="${k}" aria-pressed="${BOOK===k}">${k==='intraday'?'Intraday book':'Hold-to-expiry book'}</button>`).join('')}</span></div>`;
  if(!pb||!pb.books) return head+empty('The paper book starts with the first 10:15 IST entry after it is deployed.');
  const b=pb.books[BOOK]; if(!b) return head+empty('No data for this book yet.');
  const kpi=(l,v,c='')=>`<div class="pl-card pl-tile"><div class="pl-ch">${l}</div><div class="pl-big ${c}" style="font-size:24px">${v}</div></div>`;
  const ret=(b.equity-b.capital)/b.capital;
  const row=t=>`<tr><td>${esc(t.date)}</td><td><b>${esc(t.underlying)}</b></td><td>${esc(t.label)}${t.weak?' <span class="pl-pill flat">weak lead</span>':''}</td><td class="num">${t.lots}</td>
     <td>${esc((t.opened||'').slice(11,16))} → ${t.closed?esc(t.closed.slice(5,10)+' '+t.closed.slice(11,16)):'open'}</td><td>${esc(t.exit_reason||'—')}</td>
     <td class="num ${cls(t.status==='open'?t.mark?.pnl_rs:t.pnl_rs)}">${srs(t.status==='open'?t.mark?.pnl_rs:t.pnl_rs)}</td></tr>`;
  const table=(rows,emptyTxt)=>rows.length?`<div style="overflow:auto"><table class="pl-t"><thead><tr><th>Date</th><th>Index</th><th>Strategy</th><th style="text-align:right">Lots</th><th>Time</th><th>Exit</th><th style="text-align:right">Net P&amp;L</th></tr></thead><tbody>${rows.map(row).join('')}</tbody></table></div>`:empty(emptyTxt);
  const skips=(pb.skips_today||[]).map(s=>`${esc(s.underlying)}: ${esc(s.reason)}`).join(' · ');
  return head+`<div class="pl-grid pl-g4">${kpi('Equity',rs(b.equity))}${kpi('Return',(ret>=0?'+':'')+(ret*100).toFixed(2)+'%',cls(ret))}
     ${kpi('Closed trades · win rate',`${b.trades} · ${fin(b.win_rate)?pct(b.win_rate):'—'}`)}${kpi('Max drawdown',rs(-b.max_drawdown_rs),b.max_drawdown_rs?'down':'')}</div>
   ${equity(b)}
   <div class="pl-card" style="margin-top:16px"><div class="pl-ch">Open positions · live mark after costs</div>${table(b.open||[],'No open positions.')}${skips?`<div class="pl-muted" style="margin-top:8px">Not traded today: ${skips}</div>`:''}</div>
   <div class="pl-card" style="margin-top:16px"><div class="pl-ch">Recent trades</div>${table(b.recent||[],'No closed trades yet.')}</div>
   ${b.by_strategy?.length?`<div class="pl-card" style="margin-top:16px"><div class="pl-ch">By strategy</div><table class="pl-t"><thead><tr><th>Strategy</th><th style="text-align:right">Trades</th><th style="text-align:right">Wins</th><th style="text-align:right">Net P&amp;L</th></tr></thead><tbody>${b.by_strategy.map(x=>`<tr><td>${esc(x.label)}</td><td class="num">${x.trades}</td><td class="num">${x.wins}</td><td class="num ${cls(x.pnl_rs)}">${srs(x.pnl_rs)}</td></tr>`).join('')}</tbody></table></div>`:''}`;
}
function equity(b){
  const c=b.curve||[]; if(c.length<2) return '';
  const W=1200,H=160,pad=12,ys=c.map(x=>x[1]).concat([b.capital]),lo=Math.min(...ys),hi=Math.max(...ys);
  const X=i=>pad+i*(W-2*pad)/(c.length-1), Y=v=>pad+(hi-v)/((hi-lo)||1)*(H-2*pad);
  const d=c.map((x,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(x[1]).toFixed(1)).join(' ');
  return `<div class="pl-card" style="margin-top:16px"><div class="pl-ch">Equity after each closed trade</div>
   <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Equity curve"><line x1="0" x2="${W}" y1="${Y(b.capital)}" y2="${Y(b.capital)}" stroke="var(--pl-line)" stroke-dasharray="4 4"/>
   <path d="${d}" fill="none" stroke="var(--pl-accent)" stroke-width="2"/>${c.map((x,i)=>`<circle cx="${X(i)}" cy="${Y(x[1])}" r="9" fill="transparent" data-tip="${esc(x[0])}: ${rs(x[1])}"/>`).join('')}</svg></div>`;
}

/* Hand the strategy to the builder: same index, same catalogue entry. */
function openInBuilder(key,group){
  const sel=q('#undsel');
  if(sel&&sel.value!==UNDX&&[...sel.options].some(o=>o.value===UNDX)){sel.value=UNDX;sel.dispatchEvent(new Event('change',{bubbles:true}));}
  q('nav button[data-t="builder"]')?.click();
  let tries=0;
  (function go(){
    const tab=q(`#sbtiles .sbtab.g-${group}`), tile=q(`#sbtiles .sbtile[data-k="${key}"]`);
    if(tab&&!tab.classList.contains('on')){tab.click();}
    const t2=q(`#sbtiles .sbtile[data-k="${key}"]`);
    if(t2){t2.click();t2.scrollIntoView({behavior:'smooth',block:'center'});return;}
    if(++tries<40) setTimeout(go,150);
  })();
}

/* shared tooltip with the Pulse page */
sec.addEventListener('mousemove',e=>{const tip=q('#pl-tip'); if(!tip) return; const t=e.target.closest('[data-tip]');if(!t){tip.style.display='none';return;}
  tip.innerHTML=t.dataset.tip;tip.style.display='block';
  tip.style.left=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=Math.min(e.clientY+14,innerHeight-tip.offsetHeight-8)+'px';});
sec.addEventListener('mouseleave',()=>{const tip=q('#pl-tip'); if(tip) tip.style.display='none';});
})();

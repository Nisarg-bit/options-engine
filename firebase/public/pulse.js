/* Market Pulse page (Nov 2026).
   Reads the /live/pulse node the publisher writes every 60 s (or /api/pulse in
   API mode) and draws three views: Overview, Money flow, OI & order flow.
   It adds its own nav button and section, so index.html is changed by two lines. */
(function(){
'use strict';
const q=(s,r=document)=>r.querySelector(s), qa=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fin=v=>v!=null&&isFinite(v);
const n=(v,d=2)=>fin(v)?Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const pct=v=>fin(v)?(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(2)+'%':'—';
const cr=v=>fin(v)?(v>0?'+':v<0?'−':'')+'₹'+Math.abs(v).toLocaleString('en-IN',{maximumFractionDigits:v&&Math.abs(v)<100?1:0})+' cr':'—';
const cls=v=>!fin(v)||v===0?'':v>0?'up':'down';
const pill=v=>`<span class="pl-pill ${!fin(v)||Math.abs(v)<0.05?'flat':v>0?'up':'down'}">${pct(v)}</span>`;
const info=t=>`<span class="pl-i" tabindex="0" title="${esc(t)}" aria-label="${esc(t)}">i</span>`;
const empty=t=>`<div class="pl-empty">${esc(t)}</div>`;
const css=name=>getComputedStyle(q('#pulse')).getPropertyValue(name).trim();

/* ------------------------------------------------------------ mount */
const main=q('main'); if(!main) return;
const sec=document.createElement('section'); sec.id='pulse';
sec.innerHTML=`<div class="pl-head"><div><h1>Market Pulse</h1><div class="pl-sub" id="pl-sub">Loading…</div></div>
 <div class="pl-tabs" role="tablist" aria-label="Pulse views">
  <button role="tab" data-v="ov" aria-selected="true">Overview</button>
  <button role="tab" data-v="mf" aria-selected="false">Money flow</button>
  <button role="tab" data-v="oi" aria-selected="false">OI &amp; order flow</button></div></div>
 <div id="pl-body"></div>
 <div class="pl-foot" id="pl-foot"></div>
 <div id="pl-tip" role="tooltip" style="position:fixed;pointer-events:none;z-index:50;display:none;background:#0F2A44;color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.25);font:12px/1.4 Inter,system-ui,sans-serif;padding:6px 9px;border-radius:8px;max-width:240px"></div>`;
main.prepend(sec);
const nav=q('nav'), host=q('nav .chapters')||nav;
const btn=document.createElement('button'); btn.dataset.t='pulse'; btn.textContent='Market Pulse'; btn.setAttribute('aria-label','Market Pulse');
host.prepend(btn);
function show(){
  qa('nav button[data-t]').forEach(x=>{x.classList.toggle('on',x===btn);x.setAttribute('aria-current',x===btn?'page':'false');});
  qa('main section').forEach(s=>s.classList.toggle('on',s===sec));
  history.replaceState(null,'','#pulse'); paint(true);
}
btn.addEventListener('click',show);
// Pulse is the landing page unless the address asks for another one.
if(!location.hash||location.hash==='#pulse') show();

let VIEW='ov', P=null, SIG='', OBF='all';
qa('.pl-tabs button',sec).forEach(b=>b.onclick=()=>{VIEW=b.dataset.v;qa('.pl-tabs button',sec).forEach(x=>x.setAttribute('aria-selected',x===b));paint(true);});

/* ------------------------------------------------------------ tooltip */
const tip=q('#pl-tip'); document.body.append(tip);   // outside any section, so the Options page can share it
sec.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');if(!t){tip.style.display='none';return;}
  tip.innerHTML=t.dataset.tip;tip.style.display='block';
  const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8), y=Math.min(e.clientY+14,innerHeight-tip.offsetHeight-8);
  tip.style.left=x+'px';tip.style.top=y+'px';});
sec.addEventListener('mouseleave',()=>tip.style.display='none');

/* ------------------------------------------------------------ data */
async function load(){
  try{
    if(typeof SRC!=='undefined'&&SRC.mode==='firebase') return SRC.live?.pulse||null;
    if(typeof get==='function') return await get('/api/pulse');
  }catch(e){ return {error:e.message}; }
  return null;
}
async function paint(force){
  if(!sec.classList.contains('on')) return;
  const p=await load(); const sig=JSON.stringify(p);
  if(!force&&sig===SIG) return; SIG=sig; P=p;
  const body=q('#pl-body');
  if(!p){ q('#pl-sub').textContent='Waiting for the first pulse update'; body.innerHTML=empty('No pulse data yet. The server publishes it every minute once the stock layer is deployed.'); return; }
  if(p.error){ body.innerHTML=empty('Pulse unavailable: '+p.error); return; }
  const g=p.generated?new Date(p.generated):null;
  q('#pl-sub').textContent=`Session ${p.session||'—'} · updated ${g?g.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'})+' IST':'—'} · ${p.breadth?.stocks??0} F&O stocks`;
  q('#pl-foot').innerHTML=(p.notes||[]).map(esc).join('<br>');
  body.innerHTML=VIEW==='ov'?overview(p):VIEW==='mf'?flowView(p):oiView(p);
  if(VIEW==='ov') treemap(q('#pl-tree'),p.sectors);
  if(VIEW==='oi'){ bindOb(); bindOi(); }
}
setInterval(()=>paint(false),5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)paint(false);});

/* ------------------------------------------------------------ overview */
function gauge(m){
  if(!m?.available) return empty('Mood needs breadth, flow or index data.');
  const s=m.score, a=Math.PI*(1-s/100), cx=110, cy=104, r=84;
  const arc=(f,t,c)=>{const p=v=>[cx+r*Math.cos(Math.PI*(1-v/100)),cy-r*Math.sin(Math.PI*(1-v/100))];const[a1,b1]=p(f),[a2,b2]=p(t);return `<path d="M${a1} ${b1}A${r} ${r} 0 0 1 ${a2} ${b2}" stroke="${c}" stroke-width="14" fill="none"/>`;};
  const col=s<40?'var(--pl-down)':s<60?'var(--pl-flat)':'var(--pl-up)';
  const names={breadth:'Breadth',flow:'Money flow',trend:'NIFTY trend',volatility:'Volatility (VIX)',global:'US futures'};
  return `<svg viewBox="0 0 220 122" width="100%" style="max-width:260px;display:block;margin:0 auto" role="img" aria-label="Mood ${s} of 100, ${esc(m.label)}">
    ${arc(0,39.5,'var(--pl-down-soft)')}${arc(40.5,59.5,'var(--pl-flat-soft)')}${arc(60.5,100,'var(--pl-up-soft)')}${arc(0,Math.max(.5,s),col)}
    <line x1="${cx}" y1="${cy}" x2="${cx+(r-22)*Math.cos(a)}" y2="${cy-(r-22)*Math.sin(a)}" stroke="var(--pl-ink)" stroke-width="3" stroke-linecap="round"/>
    <circle cx="${cx}" cy="${cy}" r="5" fill="var(--pl-ink)"/></svg>
   <div style="text-align:center;margin-top:-6px"><div class="pl-big">${s}</div><div style="font-weight:700;color:var(--pl-ink)">${esc(m.label)}</div></div>
   ${m.parts.map(x=>{const w=Math.abs(x.value)*50;return `<div class="pl-row" data-tip="${esc(names[x.name]||x.name)}: ${x.value>0?'+':''}${x.value} on a −1…+1 scale · weight ${Math.round(x.weight*100)}%"><span class="l">${esc(names[x.name]||x.name)}</span><span class="pl-bar"><span style="left:${x.value<0?50-w:50}%;width:${w}%;background:var(${x.value<0?'--pl-down':'--pl-up'})"></span><span style="left:50%;width:1px;background:var(--pl-muted)"></span></span><span class="v ${cls(x.value)}">${x.value>0?'+':''}${x.value.toFixed(2)}</span></div>`;}).join('')}
   ${m.missing?.length?`<div class="pl-muted">Not counted today: ${m.missing.map(k=>esc(names[k]||k)).join(', ')}. The rest are re-weighted.</div>`:''}`;
}
function indexCard(name,i){
  if(!i) return `<div class="pl-card"><div class="pl-ch">${esc(name)}</div>${empty('No data yet')}</div>`;
  const r=i.range; let rng='';
  if(r&&fin(r.lo)&&fin(r.hi)){
    const lo=r.lo-r.sigma_pts*.25, hi=r.hi+r.sigma_pts*.25, at=Math.max(0,Math.min(100,(i.last-lo)/(hi-lo)*100));
    const L=(r.lo-lo)/(hi-lo)*100, R=(r.hi-lo)/(hi-lo)*100;
    rng=`<div class="pl-range" data-tip="Expected 1-σ range to expiry ${esc(i.expiry||'')} from India VIX: ${n(r.lo,0)} – ${n(r.hi,0)} (±${n(r.sigma_pts,0)} pts)"><div class="t"></div>
      <span style="position:absolute;top:10px;left:${L}%;width:${R-L}%;height:12px;border:1.5px dashed var(--pl-muted);border-radius:6px;box-sizing:border-box"></span>
      <div class="n" style="left:calc(${at}% - 1.5px)"></div></div><div class="pl-ends"><span>${n(r.lo,0)}</span><span>to ${esc(i.expiry||'expiry')}</span><span>${n(r.hi,0)}</span></div>`;
  }
  return `<div class="pl-card"><div class="pl-ch">${esc(name)}<span class="pl-sp"></span>${pill(i.chg_pct)}</div>
    <div class="pl-big">${n(i.last,2)}</div><div class="pl-muted"><span class="${cls(i.chg)}">${fin(i.chg)?(i.chg>0?'+':'')+n(i.chg,2):'—'}</span> · prev ${n(i.prev_close,2)}</div>${rng}</div>`;
}
function vixCard(v){
  if(!v||!fin(v.last)) return `<div class="pl-card"><div class="pl-ch">India VIX</div>${empty('No VIX yet')}</div>`;
  const span=fin(v.low_1y)&&fin(v.high_1y)&&v.high_1y>v.low_1y, at=span?(v.last-v.low_1y)/(v.high_1y-v.low_1y)*100:null;
  return `<div class="pl-card"><div class="pl-ch">India VIX ${info('Implied 30-day volatility of NIFTY options. Percentile = share of the last year’s closes at or below today.')}<span class="pl-sp"></span>${fin(v.percentile)?`<span class="pl-pill ${v.percentile>70?'warn':'flat'}">${v.percentile}th pct</span>`:''}</div>
    <div class="pl-big">${n(v.last,2)}</div><div class="pl-muted">Implies about ±${n(v.day_move_pct,2)}% for NIFTY on a normal day</div>
    ${span?`<div class="pl-range" data-tip="1-year range ${n(v.low_1y,2)} – ${n(v.high_1y,2)}"><div class="t" style="background:linear-gradient(90deg,var(--pl-accent-soft),var(--pl-warn-soft))"></div><div class="n" style="left:calc(${at}% - 1.5px)"></div></div><div class="pl-ends"><span>${n(v.low_1y,1)}</span><span>1-year range</span><span>${n(v.high_1y,1)}</span></div>`:''}</div>`;
}
function breadthCard(b){
  if(!b?.available) return `<div class="pl-card"><div class="pl-ch">Breadth</div>${empty('Stock data starts after the stock layer is deployed.')}</div>`;
  const t=b.advancers+b.decliners+b.unchanged||1, w=x=>x/t*100;
  return `<div class="pl-card"><div class="pl-ch">Breadth · ${b.stocks} F&O stocks ${info('Advancers vs decliners against yesterday’s close; |change| ≤ 0.05% counts as unchanged.')}</div>
   <div style="display:flex;gap:18px;align-items:baseline"><div><div class="pl-big up">${b.advancers}</div><div class="pl-muted">up</div></div><div><div class="pl-big down">${b.decliners}</div><div class="pl-muted">down</div></div><div><div class="pl-big" style="color:var(--pl-flat)">${b.unchanged}</div><div class="pl-muted">flat</div></div></div>
   <div class="pl-split" role="img" aria-label="${b.advancers} up, ${b.unchanged} flat, ${b.decliners} down"><span style="width:${w(b.advancers)}%;background:var(--pl-up)"></span><span style="width:${w(b.unchanged)}%;background:var(--pl-flat-soft)"></span><span style="width:${w(b.decliners)}%;background:var(--pl-down)"></span></div>
   <div class="pl-muted">${fin(b.above_vwap_pct)?`${b.above_vwap_pct}% trade above their VWAP`:''}</div></div>`;
}
function overview(p){
  const ix=p.indices||{}, s=p.sectors;
  const lead=s?.available?`<div class="pl-muted" style="margin-top:8px">Leading: ${s.leaders.map(x=>`<b>${esc(x.sector)}</b> ${pct(x.chg_pct)}`).join(', ')} · Lagging: ${s.laggards.map(x=>`<b>${esc(x.sector)}</b> ${pct(x.chg_pct)}`).join(', ')}</div>`:'';
  return `<div class="pl-grid pl-g3"><div class="pl-card"><div class="pl-ch">Market mood ${info('0–100 score from breadth 30%, money flow 25%, NIFTY trend 20%, volatility 15% and US futures 10%. A rule-based summary, not a forecast.')}</div>${gauge(p.mood)}</div>
     ${breadthCard(p.breadth)}${vixCard(p.vix)}</div>
   <div class="pl-grid pl-g3" style="margin-top:16px">${indexCard('NIFTY 50',ix.NIFTY)}${indexCard('NIFTY BANK',ix.BANKNIFTY)}${indexCard('SENSEX',ix.SENSEX)}</div>
   <div class="pl-card" style="margin-top:16px"><div class="pl-ch">Sector map ${info('Box size = turnover today. Colour = turnover-weighted % change vs yesterday. Hover for the stocks.')}<span class="pl-sp"></span>
     <span class="pl-muted" style="display:flex;align-items:center;gap:6px">−2%<span style="width:120px;height:8px;border-radius:99px;background:linear-gradient(90deg,var(--pl-down),var(--pl-flat-soft),var(--pl-up))"></span>+2%</span></div>
     ${s?.available?'<div class="pl-tree" id="pl-tree"></div>'+lead:empty('Sector map appears once stock data is collected.')}</div>`;
}
// Squarified treemap, sized by turnover; colour is a diverging scale clipped at ±2%.
function mix(a,b,t){const h=x=>[1,3,5].map(i=>parseInt(x.slice(i,i+2),16));const A=h(a),B=h(b);return'rgb('+A.map((v,i)=>Math.round(v+(B[i]-v)*t)).join(',')+')';}
function shade(c){const m=css('--pl-flat-soft')||'#EEF2F6';const t=Math.min(1,Math.abs(c||0)/2);return c>=0?mix(m,css('--pl-up')||'#178A4C',t*.85):mix(m,css('--pl-down')||'#C8423F',t*.85);}
function treemap(el,s){
  if(!el||!s?.available) return;
  const W=el.clientWidth||800,H=el.clientHeight||360, items=s.sectors.filter(x=>x.turnover_cr>0);
  const tot=items.reduce((a,x)=>a+x.turnover_cr,0)||1, sc=W*H/tot, out=[];
  let rest=items.map(x=>({...x,a:x.turnover_cr*sc})), box={x:0,y:0,w:W,h:H};
  const worst=(row,len)=>{const s=row.reduce((a,x)=>a+x.a,0),mx=Math.max(...row.map(x=>x.a)),mn=Math.min(...row.map(x=>x.a));return Math.max(len*len*mx/(s*s),s*s/(len*len*mn));};
  while(rest.length){const len=Math.min(box.w,box.h);let row=[rest[0]],i=1;
    while(i<rest.length&&worst([...row,rest[i]],len)<=worst(row,len)){row.push(rest[i]);i++;}
    rest=rest.slice(i);const s2=row.reduce((a,x)=>a+x.a,0);
    if(box.w>=box.h){const w=s2/box.h;let y=box.y;row.forEach(x=>{const h=x.a/w;out.push({...x,x:box.x,y,w,h});y+=h;});box={x:box.x+w,y:box.y,w:box.w-w,h:box.h};}
    else{const h=s2/box.w;let x0=box.x;row.forEach(x=>{const w=x.a/h;out.push({...x,x:x0,y:box.y,w,h});x0+=w;});box={x:box.x,y:box.y+h,w:box.w,h:box.h-h};}}
  el.innerHTML=out.map(r=>{const top=r.stocks.slice(0,6).map(t=>`${esc(t.symbol)} ${pct(t.chg_pct)}`).join('<br>');
    const dark=Math.abs(r.chg_pct)>1.2;return `<div class="s" style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;background:${shade(r.chg_pct)};${dark?'color:#fff':''}"
      data-tip="<b>${esc(r.sector)}</b> ${pct(r.chg_pct)} · ${r.n} stocks<br>Turnover ₹${n(r.turnover_cr,0)} cr · net flow ${cr(r.net_cr)}<br>${top}">${r.w>60&&r.h>34?`${esc(r.sector)}<small>${pct(r.chg_pct)}</small>`:''}</div>`;}).join('');
}

/* ------------------------------------------------------------ money flow */
function list(rows,key='net_cr'){return rows?.length?`<ul class="pl-list">${rows.map(r=>`<li><span><b>${esc(r.symbol)}</b> <span class="pl-muted">${esc(r.sector||'')}</span></span><span class="num ${cls(r[key])}">${cr(r[key])}</span></li>`).join('')}</ul>`:empty('—');}
function ribbons(s){
  if(!s?.available) return empty('Sector flows appear once stock data is collected.');
  const rows=[...s.sectors].sort((a,b)=>Math.abs(b.net_cr)-Math.abs(a.net_cr)).slice(0,10);
  const mx=Math.max(...rows.map(r=>Math.abs(r.net_cr)),1), H=30*rows.length+20, W=1100, inY=H*.3, outY=H*.7;
  const ins=rows.filter(r=>r.net_cr>=0).reduce((a,r)=>a+r.net_cr,0), outs=rows.filter(r=>r.net_cr<0).reduce((a,r)=>a-r.net_cr,0);
  return `<svg class="pl-flow" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Net money flow by sector">
   ${rows.map((r,i)=>{const y=20+i*30, w=Math.max(2,Math.abs(r.net_cr)/mx*16), up=r.net_cr>=0, ty=up?inY:outY, c=up?'var(--pl-up)':'var(--pl-down)';
     const d=`M230 ${y} C 520 ${y}, 560 ${ty}, 820 ${ty}`;
     return `<g data-tip="<b>${esc(r.sector)}</b>: net ${cr(r.net_cr)}<br>Turnover ₹${n(r.turnover_cr,0)} cr · ${pct(r.chg_pct)}"><path class="r" d="${d}" stroke="${c}" stroke-width="${w}"/><path class="r anim" d="${d}" stroke="${c}" stroke-width="${Math.max(1.5,w/3)}"/>
       <path d="${d}" stroke="transparent" stroke-width="18" fill="none"/><text x="222" y="${y+4}" text-anchor="end">${esc(r.sector)} <tspan class="${up?'up':'down'}" style="fill:${c}">${cr(r.net_cr)}</tspan></text></g>`;}).join('')}
   <rect x="820" y="${inY-16}" width="6" height="32" rx="3" fill="var(--pl-up)"/><text x="834" y="${inY-2}" style="font-weight:600">Net buying</text><text x="834" y="${inY+14}" style="fill:var(--pl-up)">${cr(ins)}</text>
   <rect x="820" y="${outY-16}" width="6" height="32" rx="3" fill="var(--pl-down)"/><text x="834" y="${outY-2}" style="font-weight:600">Net selling</text><text x="834" y="${outY+14}" style="fill:var(--pl-down)">${cr(-outs)}</text></svg>`;
}
function instChart(h){
  if(!h?.length) return '';
  const W=1200,H=170,pad=10,mx=Math.max(1,...h.flatMap(x=>[Math.abs(x.fii_net_cr||0),Math.abs(x.dii_net_cr||0)]));
  const y=v=>H/2-(v||0)/mx*(H/2-pad), bw=(W-20)/h.length, w=Math.max(2,bw*0.36);
  const bar=(x,v,c,lab,d)=>`<rect x="${x}" y="${Math.min(y(v),H/2)}" width="${w}" height="${Math.max(1,Math.abs(y(v)-H/2))}" rx="2" fill="${c}" data-tip="${d}: ${lab} net ${cr(v)}"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;margin-top:8px" role="img" aria-label="FII and DII net cash flows, last ${h.length} days">
    <line x1="0" x2="${W}" y1="${H/2}" y2="${H/2}" stroke="var(--pl-line)"/>
    ${h.map((d,i)=>{const x=10+i*bw+bw*0.12;return bar(x,d.fii_net_cr,'var(--pl-accent)','FII',d.date)+bar(x+w+2,d.dii_net_cr,'#5B6BD6','DII',d.date);}).join('')}
    <text x="0" y="${H-2}" style="font-size:11px;fill:var(--pl-muted)">${esc(h[0].date)}</text><text x="${W}" y="${H-2}" text-anchor="end" style="font-size:11px;fill:var(--pl-muted)">${esc(h[h.length-1].date)}</text></svg>
   <div class="pl-muted" style="display:flex;gap:14px;margin-top:4px"><span><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:var(--pl-accent);vertical-align:-1px"></span> FII/FPI</span><span><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:#5B6BD6;vertical-align:-1px"></span> DII</span><span>net cash, ₹ crore per day</span></div>`;
}
function instCard(x){
  if(!x?.available) return `<div class="pl-card"><div class="pl-ch">Institutional flows · NSE</div>${empty(x?.reason||'FII/DII numbers appear after NSE publishes them in the evening.')}</div>`;
  const c=x.cash||{}, fo=x.fii_fo;
  const tile=(lab,v,sub)=>`<div><div class="pl-muted">${lab}</div><div class="pl-big ${cls(v)}" style="font-size:24px">${cr(v)}</div><div class="pl-muted">${sub}</div></div>`;
  return `<div class="pl-card"><div class="pl-ch">Institutional flows · NSE ${info('NSE’s published provisional numbers for the cash market, and participant-wise open interest in index F&O. Actual figures, unlike the estimate below. Updated after about 18:00 IST.')}<span class="pl-sp"></span><span class="pl-pill flat">${esc(x.date)}</span></div>
   <div class="pl-grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
    ${tile('FII/FPI net · cash',c.fii?.net_cr,`buy ₹${n(c.fii?.buy_cr,0)} · sell ₹${n(c.fii?.sell_cr,0)} cr`)}
    ${tile('DII net · cash',c.dii?.net_cr,`buy ₹${n(c.dii?.buy_cr,0)} · sell ₹${n(c.dii?.sell_cr,0)} cr`)}
    ${tile('FII last 5 days',x.fii_5d_cr,'sum of daily net')}
    ${tile('DII last 5 days',x.dii_5d_cr,'sum of daily net')}
    ${fo?`<div data-tip="FII index futures: ${n(fo.fut_long,0)} long vs ${n(fo.fut_short,0)} short contracts"><div class="pl-muted">FII index futures</div><div class="pl-big" style="font-size:24px">${fin(fo.fut_long_pct)?fo.fut_long_pct.toFixed(0)+'% long':'—'}</div><div class="pl-muted">net ${n(fo.fut_net,0)} contracts${fin(fo.fut_net_chg)?` (<span class="${cls(fo.fut_net_chg)}">${fo.fut_net_chg>0?'+':''}${n(fo.fut_net_chg,0)}</span> vs last day)`:''}</div></div>
    <div data-tip="Long minus short contracts in index options"><div class="pl-muted">FII index options, net</div><div style="font:600 14px var(--pl-num);color:var(--pl-ink);margin-top:6px">calls <span class="${cls(fo.calls_net)}">${n(fo.calls_net,0)}</span><br>puts <span class="${cls(fo.puts_net)}">${n(fo.puts_net,0)}</span></div></div>`:''}
   </div>${instChart(x.history)}</div>`;
}
function flowView(p){
  const f=p.flow, inst=`<div style="margin-bottom:16px">${instCard(p.fii_dii)}</div>`;
  if(!f?.available) return inst+empty('Money flow appears once stock data is collected (after the stock layer is deployed).');
  const t=f.inflow_cr+f.outflow_cr||1;
  return inst+`<div class="pl-h2">Estimated money flow · 200 F&amp;O stocks</div><div class="pl-grid pl-g4">
    <div class="pl-card pl-tile"><div class="pl-ch">Buying turnover ${info('Turnover of 1-minute bars that closed above the previous bar. An estimate from price × volume, not actual fund flows.')}</div><div class="pl-big up">₹${n(f.inflow_cr,0)} cr</div></div>
    <div class="pl-card pl-tile"><div class="pl-ch">Selling turnover</div><div class="pl-big down">₹${n(f.outflow_cr,0)} cr</div></div>
    <div class="pl-card pl-tile"><div class="pl-ch">Net</div><div class="pl-big ${cls(f.net_cr)}">${cr(f.net_cr)}</div></div>
    <div class="pl-card pl-tile"><div class="pl-ch">Buy share</div><div class="pl-big">${Math.round(f.inflow_cr/t*100)}%</div>
      <div class="pl-split"><span style="width:${f.inflow_cr/t*100}%;background:var(--pl-up)"></span><span style="flex:1;background:var(--pl-down)"></span></div></div></div>
   <div class="pl-card" style="margin-top:16px"><div class="pl-ch">Where the money went · sectors ${info('Ribbon thickness = size of net flow. Top 10 sectors by absolute net flow.')}<span class="pl-sp"></span><button class="pl-btn" id="pl-still">${sec.classList.contains('pl-still')?'Animate':'Pause animation'}</button></div>${ribbons(p.sectors)}</div>
   <div class="pl-grid pl-g4" style="margin-top:16px">
    <div class="pl-card"><div class="pl-ch">Top net buying</div>${list(f.top_in)}</div>
    <div class="pl-card"><div class="pl-ch">Top net selling</div>${list(f.top_out)}</div>
    <div class="pl-card"><div class="pl-ch">Fastest in · last 15 min</div>${list(f.fastest_in,'recent_cr')}</div>
    <div class="pl-card"><div class="pl-ch">Fastest out · last 15 min</div>${list(f.fastest_out,'recent_cr')}</div></div>`;
}
document.addEventListener('click',e=>{if(e.target.id==='pl-still'){sec.classList.toggle('pl-still');e.target.textContent=sec.classList.contains('pl-still')?'Animate':'Pause animation';}});

/* ------------------------------------------------------------ OI & order blocks */
const READS={'long buildup':['up','Price up, OI up'],'short buildup':['down','Price down, OI up'],'short covering':['warn','Price up, OI down'],'long unwinding':['warn','Price down, OI down'],'neutral':['flat','Little change']};
let OIF='all';
function oiCard(o){
  if(!o?.available) return empty('Stock futures OI appears once the stock layer is collecting.');
  const c=o.counts, tot=Object.values(c).reduce((a,b)=>a+b,0)||1;
  const card=k=>`<button class="pl-card pl-oif" data-oif="${esc(k)}" aria-pressed="${OIF===k}" data-tip="${READS[k][1]} · click to list these stocks"><div class="pl-ch" style="margin:0 0 4px"><span class="pl-pill ${READS[k][0]}">${esc(k)}</span></div><div class="pl-big" style="font-size:24px">${c[k]||0}</div><div class="pl-muted">${Math.round((c[k]||0)/tot*100)}% of futures</div></button>`;
  const rows=OIF==='all'?o.rows.slice(0,15):o.rows.filter(r=>r.read===OIF);
  const title=OIF==='all'?'Biggest OI changes (top 15)':`${OIF.charAt(0).toUpperCase()+OIF.slice(1)} · ${rows.length} stock${rows.length===1?'':'s'}`;
  return `<div class="pl-grid" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">${Object.keys(READS).map(card).join('')}</div>
   <div class="pl-card" style="margin-top:16px;overflow:auto"><div class="pl-ch">${title} ${info('Nearest-month stock futures. Price change vs yesterday’s close; OI change since today’s first bar. Click a category above to filter.')}<span class="pl-sp"></span>${OIF!=='all'?'<button class="pl-btn" data-oif="all">Show all</button>':''}</div>
   ${rows.length?`<table class="pl-t"><thead><tr><th>Stock</th><th style="text-align:right">Price</th><th style="text-align:right">OI change</th><th style="text-align:right">OI</th><th>Reading</th></tr></thead><tbody>
   ${rows.map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td class="num ${cls(r.price_chg_pct)}">${pct(r.price_chg_pct)}</td><td class="num">${pct(r.oi_chg_pct)}</td><td class="num">${n(r.oi,0)}</td><td><span class="pl-pill ${READS[r.read]?.[0]||'flat'}">${esc(r.read)}</span></td></tr>`).join('')}</tbody></table>`:empty('No stocks in this category right now.')}</div>`;
}
function bindOi(){qa('[data-oif]',sec).forEach(b=>b.onclick=()=>{const k=b.dataset.oif;OIF=(OIF===k&&k!=='all')?'all':k;q('#pl-oi').innerHTML=oiCard(P.stock_oi);bindOi();});}
function candles(c){
  const k=c.candles||[]; if(k.length<2) return '<svg></svg>';
  const zs=c.blocks||[], lo=Math.min(...k.map(x=>x[3]),...zs.map(z=>z.lo)), hi=Math.max(...k.map(x=>x[2]),...zs.map(z=>z.hi));
  const W=300,H=120,pad=4,y=v=>pad+(hi-v)/(hi-lo||1)*(H-2*pad), bw=Math.max(1.5,W/k.length*.6), x=i=>(i+.5)*W/k.length;
  const z=zs.map(b=>`<rect x="0" y="${y(b.hi)}" width="${W}" height="${Math.max(2,y(b.lo)-y(b.hi))}" fill="var(${b.side==='buy'?'--pl-up-soft':'--pl-down-soft'})" stroke="var(${b.side==='buy'?'--pl-up':'--pl-down'})" stroke-width="1" ${b.tested?'stroke-dasharray="3 3" opacity=".6"':''}
      data-tip="${b.side==='buy'?'Buy':'Sell'} zone ${n(b.lo,2)} – ${n(b.hi,2)} · formed ${esc(String(b.at).slice(11,16))} · ${b.tested?'already retested':'not yet retested'}"/>`).join('');
  const bars=k.map((d,i)=>{const up=d[4]>=d[1],col=up?'var(--pl-up)':'var(--pl-down)';return `<g data-tip="${esc(d[0])} · O ${n(d[1])} H ${n(d[2])} L ${n(d[3])} C ${n(d[4])}"><line x1="${x(i)}" x2="${x(i)}" y1="${y(d[2])}" y2="${y(d[3])}" stroke="${col}" stroke-width="1"/><rect x="${x(i)-bw/2}" y="${y(Math.max(d[1],d[4]))}" width="${bw}" height="${Math.max(1,Math.abs(y(d[1])-y(d[4])))}" fill="${col}" rx="1"/></g>`;}).join('');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="5-minute candles for ${esc(c.symbol)} with order-block zones">${z}${bars}</svg>`;
}
function obView(cards){
  if(!cards?.length) return empty('Order blocks appear once there are enough 5-minute candles for the most active stocks.');
  const f=OBF==='all'?cards:cards.filter(c=>c.nearest?.side===OBF);
  return `<div class="pl-chips" role="group" aria-label="Filter order blocks">${[['all','All'],['buy','Near buy zone'],['sell','Near sell zone']].map(([k,l])=>`<button data-ob="${k}" aria-pressed="${OBF===k}">${l}</button>`).join('')}</div>
   <div class="pl-obgrid">${f.map(c=>{const z=c.nearest;return `<div class="pl-card pl-ob"><h3><span>${esc(c.symbol)} <span class="pl-muted" style="font-weight:500">${esc(c.sector||'')}</span></span>${pill(c.chg_pct)}</h3>
     ${candles(c)}<div class="pl-muted">${z?`Nearest untested <b class="${z.side==='buy'?'up':'down'}">${z.side} zone</b> ${n(z.lo)} – ${n(z.hi)} · last ${n(c.last)}`:'No untested zone today'}</div></div>`;}).join('')||empty('No stocks match this filter.')}</div>`;
}
function oiView(p){
  return `<div class="pl-h2" style="margin-top:4px">Stock futures OI build-up</div><div id="pl-oi">${oiCard(p.stock_oi)}</div>
   <div class="pl-h2" style="margin-top:22px">Order blocks · 24 most active stocks ${info('Buy zone: the last down candle before a sharp move up (close beyond 1.5 × ATR within 3 candles). Sell zone: the mirror. Dashed = price has already come back into it. Found by a fixed rule, not a recommendation.')}</div>
   <div id="pl-ob">${obView(p.order_blocks)}</div>`;
}
function bindOb(){qa('[data-ob]',sec).forEach(b=>b.onclick=()=>{OBF=b.dataset.ob;q('#pl-ob').innerHTML=obView(P.order_blocks);bindOb();});}
addEventListener('resize',()=>{if(VIEW==='ov'&&P)treemap(q('#pl-tree'),P.sectors);});
})();

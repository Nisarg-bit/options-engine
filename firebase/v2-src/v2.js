/* ============================================================ v2: storyline
 *
 * Chapters, theme, the verdict, the plain-English brief, the rich/cheap dial,
 * the range cone, risk in rupees and the track record.
 *
 * Reads the same DATA every other block reads. The one thing it computes that
 * the server does not publish is the scenario repricing in "risk in rupees":
 * the site's own normal (Bachelier) model, one volatility solved so the model
 * reproduces the structure's credit at entry, scaled by remaining time. That
 * is stated on the card, with what it leaves out.
 */
(function(){
'use strict';

/* ------------------------------------------------------------------ theme */
const THEME_KEY = 'desk-theme';
function setTheme(t){
  document.documentElement.dataset.theme = t;
  try{ localStorage.setItem(THEME_KEY, t); }catch(e){}
  const b = $('#themebtn');
  if(b) b.title = t === 'light' ? 'Switch to dark' : 'Switch to light';
}
setTheme(document.documentElement.dataset.theme || 'light');
$('#themebtn').onclick = () =>
  setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');

/* ------------------------------------------------------- chapter "next" */
document.addEventListener('click', ev => {
  const b = ev.target.closest && ev.target.closest('[data-go]');
  if(!b) return;
  const t = document.querySelector(`nav button[data-t="${b.dataset.go}"]`);
  if(t){ t.click(); window.scrollTo({top:0, behavior:'smooth'}); }
});

/* ------------------------------------------------------------------ maths */
const SQ2PI = Math.sqrt(2 * Math.PI);
const npdf = z => Math.exp(-0.5 * z * z) / SQ2PI;
const ncdf = z => 0.5 * (1 + erf(z / Math.SQRT2));
function bach(x, K, kind, sg){
  if(sg <= 1e-9) return kind === 'CE' ? Math.max(x - K, 0) : Math.max(K - x, 0);
  const d = kind === 'CE' ? (x - K) / sg : (K - x) / sg;
  return sg * npdf(d) + d * sg * ncdf(d);
}
/** What it costs to close the structure, per unit, at index level x. */
function closeCost(st, x, sg){
  let v = 0;
  for(const l of st.legs){ const q = l.qty < 0 ? -1 : 1; v += -q * bach(x, +l.strike, l.kind, sg); }
  return v;
}
/** One sigma (points, to expiry) that makes the model price = the credit. */
function solveSigma(st, spot, hint){
  let lo = 1e-3, hi = 6 * Math.max(hint || 100, 1);
  const f = s => closeCost(st, spot, s) - st.credit_pts;
  if(!(f(lo) <= 0 && f(hi) >= 0)) return null;
  for(let i = 0; i < 70; i++){ const m = (lo + hi) / 2; if(f(m) < 0) lo = m; else hi = m; }
  return (lo + hi) / 2;
}
function wilson(k, n, z = 1.96){
  if(!n) return null;
  const p = k / n, d = 1 + z * z / n, c = (p + z * z / (2 * n)) / d;
  const h = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}
const daysBetween = (a, b) => Math.round((new Date(b + 'T00:00:00Z') - new Date(a + 'T00:00:00Z')) / 864e5);
// Dates are calendar dates, not instants: parse and print them in UTC so a
// viewer's time zone can never shift a label by a day.
const fmtDate = d => new Date(d + 'T00:00:00Z').toLocaleDateString('en-IN',
  {day:'numeric', month:'short', timeZone:'UTC'});
const rsK = v => (v < 0 ? '−' : '') + '₹' + num(Math.abs(v));

/* -------------------------------------------------------------- shared data */
async function context(){
  let a = null, w = {}, d = null;
  try{ a = await DATA.analytics(EXPIRY); }catch(e){}
  try{ w = (await DATA.world()) || {}; }catch(e){}
  try{ d = await DATA.daytrade(); }catch(e){}
  return {a, w, d};
}
const rec = s => s && s.structures ? (s.structures.find(x => x.recommended) || null) : null;
const straddle = s => s && s.structures ? s.structures.find(x => x.name === 'short_straddle') : null;
function richWord(r){
  if(r == null) return null;
  return r >= 1.10 ? 'rich' : r <= 0.90 ? 'cheap' : 'fair';
}
function allEvents(w){ return (w && w.events && w.events.events) || []; }
function eventsBefore(w, s){
  return allEvents(w).filter(e => s && e.date <= s.expiry && e.kind !== 'holiday');
}
function globalMood(w){
  const rows = (w && w.prices && w.prices.rows) || [];
  const g = k => rows.find(r => r.key === k);
  const es = g('es'), nq = g('nq'), vix = g('us_vix');
  const fut = [es, nq].filter(r => r && r.chg_pct != null).map(r => r.chg_pct);
  if(!fut.length) return null;
  const avg = fut.reduce((x, y) => x + y, 0) / fut.length;
  const word = avg <= -1 ? 'falling hard' : avg <= -0.3 ? 'a little weak'
             : avg >= 1 ? 'rallying' : avg >= 0.3 ? 'a little firm' : 'flat';
  return {avg, word, es, vix};
}

/* ================================================================ TODAY */
function renderVerdict(s, c){
  const host = $('#verdict');
  if(!host) return;
  if(!s){
    // loadDash failed and wrote why into #gates. The usual case is the
    // evening of an expiry day: the default expiry has just settled and its
    // chain is empty. Say that, and point at the fix, not "waiting".
    const why = (($('#gates') || {}).innerText || '').trim();
    const settled = /too thin/i.test(why);
    host.className = 'card wide verdict na';
    host.innerHTML = `<div class="vk">Today’s read</div><div class="vh">${settled
      ? 'This expiry has settled — pick the next one in the expiry menu at the top.'
      : 'No reading for this expiry yet.'}</div>${why ? `<div class="sub">${esc(why)}</div>` : ''}`;
    return;
  }
  const r = rec(s), rw = richWord(s.ratio), evs = eventsBefore(c.w, s), mood = globalMood(c.w);
  let tone, head;
  if(s.cs_gate == null){
    tone = 'na'; head = `No verdict for ${esc(UND)}: the entry rule was only calibrated on NIFTY.`;
  }else if(s.gate_passed){
    tone = 'go'; head = `The model <em>would sell premium</em> today — ${r ? esc(r.label.toLowerCase()) : 'see chapter 3'}, paper only.`;
  }else{
    tone = 'no'; head = `<em>No trade today.</em> ${s.c_over_sigma != null && s.c_over_sigma < s.cs_gate
      ? 'The premium on offer is too thin for the risk.' : 'Nothing clears its costs.'}`;
  }
  const chips = [];
  if(rw) chips.push([rw === 'rich' ? 'good' : rw === 'cheap' ? 'bad' : '',
    `Premium is <b>${rw}</b> — ${num(s.ratio, 2)}× the model’s fair value`]);
  if(s.cs_gate != null) chips.push([s.gate_passed ? 'good' : 'bad',
    `Entry rule: ${num(s.c_over_sigma, 2)} vs ${num(s.cs_gate, 2)} needed`]);
  chips.push([evs.length ? 'warn' : 'good', evs.length
    ? `${evs.length} scheduled event${evs.length > 1 ? 's' : ''} before expiry — next: ${esc(evs[0].title)}`
    : 'No scheduled events before expiry']);
  if(mood) chips.push([Math.abs(mood.avg) >= 1 ? 'warn' : '',
    `US futures ${mood.word} (${mood.avg >= 0 ? '+' : ''}${mood.avg.toFixed(2)}%)`]);
  const ps = r && pStopAt(r, 0.30);
  if(ps) chips.push([ps.p > 0.5 ? 'bad' : ps.p > 0.3 ? 'warn' : 'good',
    `Chance the 30% stop is hit: ${pct(ps.p, 0)}`]);
  chips.push(['bad', 'Reminder: the tested weekly strategy lost after costs']);
  host.className = 'card wide verdict ' + tone;
  host.innerHTML = `<div class="vk">Today’s read · ${esc(UND)} · expiry ${fmtDate(s.expiry)} (${s.dte === 0 ? 'today' : s.dte + 'd'})</div>
    <div class="vh">${head}</div>
    <div class="reasons">${chips.map(([k, t]) => `<div class="reason ${k}"><span class="d"></span>${t}</div>`).join('')}</div>`;
}

function renderBrief(s, c){
  const host = $('#brief');
  if(!host || !s) return;
  const sd = s.sigma, sp = s.spot, rw = richWord(s.ratio);
  const P = [];
  P.push(`${esc(UND)} is at <b>${num(sp, 0)}</b>. The options market is pricing a typical move of about
    <b>±${num(sd, 0)} points</b> (${(sd / sp * 100).toFixed(1)}%) by the ${fmtDate(s.expiry)} expiry,
    ${s.dte === 0 ? 'which is today' : s.dte + ' day' + (s.dte === 1 ? '' : 's') + ' away'}. Two days in three should end inside
    <b>${num(sp - sd, 0)}–${num(sp + sd, 0)}</b> — if the usual model holds, which in big moves it often does not.`);
  if(rw) P.push(`Selling the at-the-money straddle pays <b>${num(s.straddle, 1)}</b> points against a
    model fair value of <b>${num(s.fair, 1)}</b>, so premium looks <span class="hl">${rw}</span>.
    ${rw === 'rich' ? 'Sellers are being paid more than the model thinks the move is worth.'
      : rw === 'cheap' ? 'Sellers are being paid less than the model thinks the move is worth.'
      : 'Sellers are being paid roughly what the model thinks the move is worth.'}`);
  if(s.cs_gate != null) P.push(s.gate_passed
    ? `The entry rule is <b>open</b>: the premium is large enough, relative to the expected move, for the model to consider selling.`
    : `The entry rule is <b>shut</b>: the premium is too small relative to the expected move, so the model does not sell today.`);
  const m = globalMood(c.w), evs = eventsBefore(c.w, s), all = allEvents(c.w);
  let wl = '';
  if(m) wl += `Overnight, US stock futures are ${m.word}${m.vix && m.vix.last != null ? `, and US VIX is at <b>${num(m.vix.last, 1)}</b>` : ''}. `;
  if(evs.length) wl += `<span class="hl">${evs.length} scheduled event${evs.length > 1 ? 's fall' : ' falls'} before this expiry</span>
    — ${evs.slice(0, 2).map(e => `${esc(e.title)} (${fmtDate(e.date)})`).join(', ')}. Big announcements can move the index more than the model expects.`;
  else if(all.length) wl += `Nothing scheduled before this expiry; the next event is ${esc(all[0].title)} on ${fmtDate(all[0].date)}.`;
  if(wl) P.push(wl);
  const st = straddle(s);
  if(st){
    const loss = (st.credit_pts - closeCost(st, sp - 2 * sd, 0)) * (s.lot || 65);
    P.push(`To put a rupee figure on it: one lot of the straddle collects about <b>${rsK(st.credit_rs)}</b>.
      If ${esc(UND)} finished <b>${num(2 * sd, 0)} points</b> lower at expiry (a 2-standard-deviation move), that lot would
      ${loss < 0 ? `lose about <b class="neg">${rsK(loss)}</b>` : `still make ${rsK(loss)}`} before charges.
      Chapter 3 lets you try any move.`);
  }
  P.push(`<span class="honest">Honest note: when this weekly premium-selling strategy was tested on two years of data, it did not make money after costs. Everything here is research and paper trading, not advice.</span>`);
  host.innerHTML = `<div class="brief">${P.map(p => `<p>${p}</p>`).join('')}</div>`;
}

function dialChart(host, ratio){
  host.innerHTML = '';
  if(ratio == null){ host.innerHTML = '<div class="empty">no reading</div>'; return; }
  const W = 360, H = 222, cx = 180, cy = 170, R = 140, lo = 0.6, hi = 1.6;
  const ang = v => Math.PI * (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo));
  const pt = (v, r) => [cx + r * Math.cos(ang(v)), cy - r * Math.sin(ang(v))];
  const arc = (a, b, r) => { const [x1, y1] = pt(a, r), [x2, y2] = pt(b, r);
    return `M${x1},${y1} A${r},${r} 0 0 1 ${x2},${y2}`; };
  const s = svg(W, H);
  [[lo, 0.9, 'var(--a1)', 'Cheap — sellers are under-paid'],
   [0.9, 1.1, 'var(--faint)', 'Fair — roughly what the move is worth'],
   [1.1, hi, 'var(--up)', 'Rich — sellers are paid more than the model’s fair value']]
  .forEach(([a, b, col, t]) => {
    const p = node('path', {d: arc(a, b, R), fill: 'none', 'stroke-width': 22,
      style: `stroke:${col};opacity:.85;cursor:help`});
    p.addEventListener('mouseenter', () => { const [x, y] = pt((a + b) / 2, R);
      showTip(host, x / W * host.clientWidth, y / H * host.clientHeight, `<b>${t}</b><br><span class="r">${a.toFixed(2)}–${b.toFixed(2)}×</span>`); });
    p.addEventListener('mouseleave', hideTip);
    s.appendChild(p);
  });
  [0.6, 0.9, 1.1, 1.6].forEach(v => { const [x, y] = pt(v, R + 24);
    s.appendChild(node('text', {x, y: y + 4, 'text-anchor': 'middle', 'font-size': 11,
      style: 'fill:var(--faint);font-family:var(--mono)'}, v.toFixed(1) + '×')); });
  const g = node('g', {class: 'needle', style: 'transform:rotate(0deg)'});
  g.appendChild(node('line', {x1: cx, y1: cy, x2: cx - R + 18, y2: cy, 'stroke-width': 5,
    'stroke-linecap': 'round', style: 'stroke:var(--ink)'}));
  g.appendChild(node('circle', {cx, cy, r: 10, style: 'fill:var(--ink)'}));
  s.appendChild(g);
  s.appendChild(node('text', {x: cx, y: cy + 46, 'text-anchor': 'middle', 'font-size': 30,
    'font-weight': 700, style: 'fill:var(--ink);font-family:var(--mono)'}, ratio.toFixed(2) + '×'));
  host.appendChild(s);
  const deg = (Math.max(lo, Math.min(hi, ratio)) - lo) / (hi - lo) * 180;
  requestAnimationFrame(() => requestAnimationFrame(() => { g.style.transform = `rotate(${deg}deg)`; }));
}

function coneChart(host, s, c){
  host.innerHTML = '';
  if(!s || !s.sigma){ host.innerHTML = '<div class="empty">no range yet</div>'; return; }
  const W = 1100, H = 330, PL = 70, PR = 104, PT = 18, PB = 44;
  const T = Math.max(s.dte, 1), sp = s.spot, sd = s.sigma;
  const r = rec(s) || straddle(s);
  const ys = [sp - 2.3 * sd, sp + 2.3 * sd];
  if(r) ys.push(r.lower_be, r.upper_be);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const X = t => PL + t / T * (W - PL - PR), Y = v => PT + (hi - v) / (hi - lo) * (H - PT - PB);
  const N = 48, tt = [...Array(N + 1)].map((_, i) => T * i / N);
  const band = k => 'M' + tt.map(t => `${X(t)},${Y(sp + k * sd * Math.sqrt(t / T))}`).join(' L')
      + ' L' + tt.slice().reverse().map(t => `${X(t)},${Y(sp - k * sd * Math.sqrt(t / T))}`).join(' L') + ' Z';
  const g = svg(W, H);
  niceTicks(lo, hi, 5).forEach(v => {
    g.appendChild(node('line', {x1: PL, y1: Y(v), x2: W - PR, y2: Y(v), style: 'stroke:var(--edge2)'}));
    g.appendChild(node('text', {x: PL - 10, y: Y(v) + 4, 'text-anchor': 'end', 'font-size': 11,
      style: 'fill:var(--faint);font-family:var(--mono)'}, num(v)));
  });
  g.appendChild(node('path', {d: band(2), style: 'fill:#4f63f6;opacity:.13'}));
  g.appendChild(node('path', {d: band(1), style: 'fill:#8b5cf6;opacity:.24'}));
  g.appendChild(node('line', {x1: X(0), y1: Y(sp), x2: X(T), y2: Y(sp), 'stroke-dasharray': '2 5',
    style: 'stroke:var(--ink);opacity:.5'}));
  const lab = (y, t, col) => g.appendChild(node('text', {x: W - PR + 8, y: y + 4, 'font-size': 11.5,
    style: `fill:${col};font-family:var(--mono)`}, t));
  lab(Y(sp + sd), '+1σ ' + num(sp + sd), 'var(--dim)'); lab(Y(sp - sd), '−1σ ' + num(sp - sd), 'var(--dim)');
  lab(Y(sp + 2 * sd), '+2σ ' + num(sp + 2 * sd), 'var(--faint)'); lab(Y(sp - 2 * sd), '−2σ ' + num(sp - 2 * sd), 'var(--faint)');
  if(r){
    [r.lower_be, r.upper_be].forEach(b => g.appendChild(node('line', {x1: PL, y1: Y(b), x2: W - PR, y2: Y(b),
      'stroke-dasharray': '6 5', 'stroke-width': 1.6, style: 'stroke:#f59e0b'})));
    g.appendChild(node('text', {x: PL + 8, y: Y(r.upper_be) - 7, 'font-size': 11.5,
      style: 'fill:#f59e0b'}, `breakevens of the ${r.label.toLowerCase()}: ${num(r.lower_be)} / ${num(r.upper_be)}`));
  }
  g.appendChild(node('circle', {cx: X(0), cy: Y(sp), r: 6, style: 'fill:var(--ink)'}));
  g.appendChild(node('text', {x: X(0) + 10, y: Y(sp) - 10, 'font-size': 12, 'font-weight': 650,
    style: 'fill:var(--ink);font-family:var(--mono)'}, 'now ' + num(sp)));
  const base = new Date(s.session + 'T00:00:00Z').getTime();
  const step = Math.max(1, Math.ceil(T / 8));
  for(let d = 0; d <= T; d += step){
    const day = new Date(base + d * 864e5).toISOString().slice(0, 10);
    g.appendChild(node('text', {x: X(d), y: H - 20, 'text-anchor': d === 0 ? 'start' : 'middle', 'font-size': 11,
      style: 'fill:var(--faint);font-family:var(--mono)'}, fmtDate(day)));
  }
  allEvents(c.w).filter(e => e.date <= s.expiry && e.date >= s.session).forEach(e => {
    const d = daysBetween(s.session, e.date), x = X(Math.min(d, T)), hol = e.kind === 'holiday';
    const col = hol ? 'var(--faint)' : '#ec4899';
    g.appendChild(node('line', {x1: x, y1: PT, x2: x, y2: H - PB, 'stroke-dasharray': '2 4', style: `stroke:${col};opacity:.7`}));
    const pin = node('path', {d: `M${x - 8},${H - PB + 3} L${x + 8},${H - PB + 3} L${x},${H - PB - 11} Z`,
      style: `fill:${col};cursor:help`});
    pin.addEventListener('mouseenter', () => showTip(host, x / W * host.clientWidth, (H - PB - 12) / H * host.clientHeight,
      `<b>${esc(e.title)}</b><br><span class="r">${fmtDate(e.date)}${e.time_ist ? ' · ' + esc(e.time_ist) + ' IST' : ''}</span>`));
    pin.addEventListener('mouseleave', hideTip);
    g.appendChild(pin);
  });
  const cross = node('line', {x1: 0, y1: PT, x2: 0, y2: H - PB, 'stroke-dasharray': '3 3', opacity: 0, style: 'stroke:var(--dim)'});
  g.appendChild(cross);
  const hit = node('rect', {x: PL, y: PT, width: W - PL - PR, height: H - PT - PB - 16, fill: 'transparent', style: 'cursor:crosshair'});
  hit.addEventListener('mousemove', ev => {
    const b = g.getBoundingClientRect(), px = (ev.clientX - b.left) / b.width * W;
    const t = Math.max(0, Math.min(T, (px - PL) / (W - PL - PR) * T)), k = sd * Math.sqrt(t / T);
    cross.setAttribute('x1', X(t)); cross.setAttribute('x2', X(t)); cross.setAttribute('opacity', 1);
    showTip(host, X(t) / W * host.clientWidth, Y(sp + k) / H * host.clientHeight,
      `<b>${t < 1 ? Math.round(t * 24) + ' hours' : t.toFixed(1) + ' days'} ahead</b><br>`
      + `<span class="r">2 in 3 chance inside</span> ${num(sp - k)}–${num(sp + k)}<br>`
      + `<span class="r">19 in 20 inside</span> ${num(sp - 2 * k)}–${num(sp + 2 * k)}`);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  g.appendChild(hit);
  host.appendChild(g);
}

async function today(){
  const s = S, c = await context();
  renderVerdict(s, c);
  renderBrief(s, c);
  if(!s) return;
  dialChart($('#dial'), s.ratio);
  const rw = richWord(s.ratio);
  $('#dialnote').innerHTML = rw ? `<b>${num(s.ratio, 2)}×</b> means the at-the-money straddle costs
    ${num(s.ratio, 2)} times what the model says it is worth (${num(s.straddle, 1)} against ${num(s.fair, 1)}).
    Premium is <b>${rw}</b>. Rich is what a seller wants — but rich for a reason (a big event coming) is not a gift.
    <span class="dimtxt">Hover the bands.</span>` : '';
  coneChart($('#cone'), s, c);
}

/* ========================================================= RISK IN RUPEES */
let RKST = null;
function riskSetup(s){
  const sel = $('#rkst');
  if(!sel || !s || !s.structures) return;
  const names = s.structures.map(x => x.name).join();
  if(sel.dataset.names !== names){
    sel.innerHTML = s.structures.map(x =>
      `<option value="${x.name}">${esc(x.label)}${x.recommended ? ' ★ model’s pick' : ''}${x.defined_risk ? ' · defined risk' : ''}</option>`).join('');
    sel.dataset.names = names;
    const r = rec(s) || straddle(s) || s.structures[0];
    sel.value = RKST && names.split(',').includes(RKST) ? RKST : r.name;
  }
  const T = Math.max(s.dte, 1), sd = s.sigma;
  const mv = $('#rkmove'), dy = $('#rkday');
  mv.min = Math.round(-3 * sd); mv.max = Math.round(3 * sd); mv.step = 5;
  dy.min = 0; dy.max = T; dy.step = T <= 2 ? 0.25 : 1;
  if(!mv.dataset.init){ mv.value = Math.round(-sd); dy.value = T; mv.dataset.init = 1; }
  $('#rkchips').innerHTML = [[-2, '−2σ'], [-1, '−1σ'], [0, 'no move'], [1, '+1σ'], [2, '+2σ']]
    .map(([k, l]) => `<button class="chip" data-k="${k}">${l}${k ? ` (${k > 0 ? '+' : '−'}${num(Math.abs(k * sd))})` : ''}</button>`).join('')
    + `<button class="chip" data-exp="1">at expiry</button>`;
  $('#rkchips').querySelectorAll('button').forEach(b => b.onclick = () => {
    if(b.dataset.exp) dy.value = T; else mv.value = Math.round(+b.dataset.k * sd);
    riskDraw(); });
  sel.onchange = mv.oninput = dy.oninput = $('#rklots').oninput = riskDraw;
  riskDraw();
}
function riskDraw(){
  const s = S; if(!s || !s.structures) return;
  const st = s.structures.find(x => x.name === $('#rkst').value); if(!st) return;
  RKST = st.name;
  const T = Math.max(s.dte, 1), sd = s.sigma, sp = s.spot, lot = s.lot || 65;
  const lots = Math.max(1, Math.round(+$('#rklots').value || 1));
  const move = +$('#rkmove').value, day = Math.min(T, +$('#rkday').value);
  const sg0 = solveSigma(st, sp, sd), matched = sg0 != null, sgE = matched ? sg0 : sd;
  const sgAt = d => sgE * Math.sqrt(Math.max(0, (T - d) / T));
  const costs = ((st.fees_rs || 0) + (st.slippage_rs || 0)) * lots;
  const pnl = (x, d) => (st.credit_pts - closeCost(st, x, sgAt(d))) * lot * lots - costs;
  const slPct = (+$('#slpct').value || 30) / 100;
  const stopped = closeCost(st, sp + move, sgAt(day)) >= st.credit_pts * (1 + slPct);
  const v = pnl(sp + move, day), cap = +$('#capital').value || 0;
  const dword = d => d < 1 ? Math.round(d * 24) + ' hours' : d + ' day' + (d === 1 ? '' : 's');
  const when = day >= T ? 'at expiry' : 'in ' + dword(day);
  $('#rkmoveval').innerHTML = `${move > 0 ? '+' : ''}${num(move)} pts → <b>${num(sp + move)}</b> (${move > 0 ? '+' : ''}${(move / sp * 100).toFixed(2)}%)`;
  $('#rkdayval').innerHTML = day >= T ? '<b>at expiry</b>' : `<b>${dword(day)}</b> from now`;
  const stopLossRs = -(st.credit_pts * slPct) * lot * lots - costs;
  $('#rkout').innerHTML = `
    <div class="sub">${lots} lot${lots > 1 ? 's' : ''} · ${esc(st.label.toLowerCase())} · ${esc(UND)} at ${num(sp + move)} ${when}</div>
    <div class="big ${v >= 0 ? 'pos' : 'neg'}">${v >= 0 ? '+' : ''}${rsK(v)}</div>
    ${cap ? `<div class="sub">${v >= 0 ? '+' : ''}${(v / cap * 100).toFixed(2)}% of your ₹${num(cap)} capital (set on the sizing card)</div>` : ''}
    <p>If ${esc(UND)} is at <b>${num(sp + move)}</b> ${when}, this position would be
    ${v >= 0 ? `up about <b class="pos">${rsK(v)}</b>` : `down about <b class="neg">${rsK(v)}</b>`} after estimated charges.
    ${stopped
      ? `<br><span class="neg"><b>The ${Math.round(slPct * 100)}% stop would already have closed it</b></span>, near <b>${rsK(stopLossRs)}</b> — unless the market jumped straight past the stop, which overnight gaps can do.`
      : `The ${Math.round(slPct * 100)}% stop is not hit at this point.`}
    ${st.defined_risk ? '' : '<br><span class="dimtxt">No protective wings: the loss keeps growing the further the index moves.</span>'}</p>`;
  riskChart($('#rkchart'), {sp, sd, T, day, move, pnl, stopLossRs, matched});
}
function riskChart(host, o){
  host.innerHTML = '';
  const W = 1100, H = 300, PL = 76, PR = 20, PT = 18, PB = 36;
  const xs = [...Array(121)].map((_, i) => -3 * o.sd + 6 * o.sd * i / 120);
  const now = xs.map(m => o.pnl(o.sp + m, o.day)), exp = xs.map(m => o.pnl(o.sp + m, o.T));
  let lo = Math.min(...now, ...exp, o.stopLossRs), hi = Math.max(...now, ...exp, 0);
  const floor = -4 * Math.max(hi, Math.abs(o.stopLossRs), 1); if(lo < floor) lo = floor;
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const X = m => PL + (m + 3 * o.sd) / (6 * o.sd) * (W - PL - PR);
  const Y = v => PT + (hi - Math.max(lo, Math.min(hi, v))) / (hi - lo) * (H - PT - PB);
  const toMove = ev => { const b = g.getBoundingClientRect(), px = (ev.clientX - b.left) / b.width * W;
    return Math.round(((px - PL) / (W - PL - PR) * 6 - 3) * o.sd / 5) * 5; };
  const g = svg(W, H);
  niceTicks(lo, hi, 5).forEach(v => {
    g.appendChild(node('line', {x1: PL, y1: Y(v), x2: W - PR, y2: Y(v), style: v === 0 ? 'stroke:var(--dim);opacity:.6' : 'stroke:var(--edge2)'}));
    g.appendChild(node('text', {x: PL - 10, y: Y(v) + 4, 'text-anchor': 'end', 'font-size': 11,
      style: 'fill:var(--faint);font-family:var(--mono)'}, Math.abs(v) >= 1000 ? (v / 1000).toFixed(0) + 'k' : v.toFixed(0)));
  });
  [-2, -1, 0, 1, 2].forEach(k => g.appendChild(node('text', {x: X(k * o.sd), y: H - 12, 'text-anchor': 'middle', 'font-size': 11,
    style: 'fill:var(--faint);font-family:var(--mono)'}, k === 0 ? 'no move' : `${k > 0 ? '+' : '−'}${Math.abs(k)}σ · ${num(o.sp + k * o.sd)}`)));
  g.appendChild(node('line', {x1: PL, y1: Y(o.stopLossRs), x2: W - PR, y2: Y(o.stopLossRs), 'stroke-dasharray': '6 5',
    style: 'stroke:var(--down);opacity:.8'}));
  g.appendChild(node('text', {x: W - PR, y: Y(o.stopLossRs) - 6, 'text-anchor': 'end', 'font-size': 11,
    style: 'fill:var(--down)'}, 'loss if the stop fills at its level'));
  const line = (vals, c, w, dash) => g.appendChild(node('polyline', {points: xs.map((m, i) => `${X(m)},${Y(vals[i])}`).join(' '),
    fill: 'none', 'stroke-width': w, 'stroke-dasharray': dash || 'none', style: `stroke:${c}`}));
  line(exp, 'var(--faint)', 1.6, '5 5');
  line(now, '#f59e0b', 3);
  g.appendChild(node('circle', {cx: X(o.move), cy: Y(o.pnl(o.sp + o.move, o.day)), r: 7,
    style: 'fill:#f59e0b;stroke:var(--bg0);stroke-width:3'}));
  const hit = node('rect', {x: PL, y: PT, width: W - PL - PR, height: H - PT - PB, fill: 'transparent', style: 'cursor:pointer'});
  hit.addEventListener('mousemove', ev => { const m = toMove(ev);
    showTip(host, X(m) / W * host.clientWidth, Y(o.pnl(o.sp + m, o.day)) / H * host.clientHeight,
      `<b>${num(o.sp + m)}</b> <span class="r">${m > 0 ? '+' : ''}${num(m)} pts</span><br>`
      + `<span class="r">then</span> ${sign(o.pnl(o.sp + m, o.day))} · <span class="r">at expiry</span> ${sign(o.pnl(o.sp + m, o.T))}<br><span class="r">click to pick this move</span>`); });
  hit.addEventListener('mouseleave', hideTip);
  hit.addEventListener('click', ev => { $('#rkmove').value = toMove(ev); riskDraw(); });
  g.appendChild(hit);
  host.appendChild(g);
  const lg = el('div', 'legend');
  lg.innerHTML = `<span><i class="swatch" style="background:#f59e0b"></i>at the time you picked</span>
    <span><i class="swatch" style="background:var(--faint)"></i>at expiry</span>
    <span><i class="swatch" style="background:var(--down)"></i>stop level</span>
    ${o.matched ? '' : '<span class="neg">the model could not match the market price; shown on the site’s σ</span>'}`;
  host.appendChild(lg);
}
async function trade(){ riskSetup(S); }

/* ============================================================ TRACK RECORD */
async function proof(){
  const host = $('#track'); if(!host) return;
  let d, j = null;
  try{ d = await DATA.daytrade(); }catch(e){ host.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  try{ j = await DATA.journal(); }catch(e){}
  const sm = d.summary || {}, n = sm.settled || 0, floor = sm.floor || 40;
  const rows = (d.journal || []).filter(r => r.status === 'settled');
  const nets = rows.map(r => +r.net_rs);
  let cum = 0, peak = 0, dd = 0;
  const curve = nets.map(v => { cum += v; peak = Math.max(peak, cum); dd = Math.min(dd, cum - peak); return cum; });
  const wins = sm.wins || 0, wr = n ? wins / n : null, wci = wilson(wins, n), sci = sm.stop_rate_ci;
  const best = nets.length ? Math.max(...nets) : null, worst = nets.length ? Math.min(...nets) : null;
  const ciBar = (p, ci) => ci ? `<div class="ci" title="95% range ${pct(ci[0], 0)}–${pct(ci[1], 0)}"><i style="left:${ci[0] * 100}%;right:${100 - ci[1] * 100}%"></i><b style="left:calc(${p * 100}% - 1px)"></b></div>` : '';
  const partial = rows.length < n ? ` <span class="dimtxt" style="text-transform:none;letter-spacing:0">(the last ${rows.length} of ${n})</span>` : '';
  const tot = sm.total_net || 0;
  const skew = sm.median_net != null && sm.mean_net != null && sm.median_net > 0 && sm.mean_net < sm.median_net / 2;
  host.innerHTML = `
    <div class="brief" style="margin-bottom:18px"><p>Every market day the desk paper-trades the same simple trade —
      sell the at-the-money straddle at 09:20, stop at 30% of the premium, close by 15:15 — and records what really
      happened, at real bid/ask prices and real charges. <b>${n}</b> sessions are finished so far (target ${floor}).
      It made money on <b>${wins}</b>${wr != null ? ` (${pct(wr, 0)})` : ''}${wci ? `, but with this few sessions the true
      win rate could be anywhere from <b>${pct(wci[0], 0)}</b> to <b>${pct(wci[1], 0)}</b>` : ''}.
      Running total: <b class="${tot >= 0 ? 'pos' : 'neg'}">${tot >= 0 ? '+' : ''}${rsK(tot)}</b> per lot.
      ${skew ? `A typical day made <b>${rsK(sm.median_net)}</b> but the average is <b>${rsK(sm.mean_net)}</b> — most days pay a little and a few big ones take it back. That shape is what selling options looks like.` : ''}
      ${n < floor ? `<span class="hl">Too few sessions to call this a result yet.</span>` : ''}</p></div>
    <div class="kpis">
      <div class="kpi"><div class="k">Sessions recorded</div><div class="v">${n}<span class="dimtxt" style="font-size:14px"> / ${floor}</span></div>
        <div class="bar good" style="margin-top:10px"><i style="width:${Math.min(100, n / floor * 100)}%"></i></div></div>
      <div class="kpi"><div class="k">Win rate</div><div class="v">${wr == null ? '–' : pct(wr, 0)}</div>
        <div class="s">${wci ? `could really be ${pct(wci[0], 0)}–${pct(wci[1], 0)}` : ''}</div>${wr != null ? ciBar(wr, wci) : ''}</div>
      <div class="kpi"><div class="k">Running total, per lot</div><div class="v ${tot >= 0 ? 'pos' : 'neg'}">${tot >= 0 ? '+' : ''}${rsK(tot)}</div><div class="s">after spread and charges</div></div>
      <div class="kpi"><div class="k">Typical · average day</div><div class="v" style="font-size:19px">${sm.median_net == null ? '–' : rsK(sm.median_net)} <span class="dimtxt">·</span> ${sm.mean_net == null ? '–' : rsK(sm.mean_net)}</div><div class="s">median · mean</div></div>
      <div class="kpi"><div class="k">Best · worst day</div><div class="v" style="font-size:19px"><span class="pos">${best == null ? '–' : rsK(best)}</span> <span class="dimtxt">·</span> <span class="neg">${worst == null ? '–' : rsK(worst)}</span></div><div class="s">sessions shown below</div></div>
      <div class="kpi"><div class="k">Deepest drop</div><div class="v neg">${rsK(dd)}</div><div class="s">peak to trough, sessions shown</div></div>
      <div class="kpi"><div class="k">Stopped out</div><div class="v">${sm.stops ?? '–'}<span class="dimtxt" style="font-size:14px"> / ${n}</span></div>
        <div class="s">${sci ? `could really be ${pct(sci[0], 0)}–${pct(sci[1], 0)}` : ''}${sm.p_stop_vix_mean != null ? ` · model expected ${pct(sm.p_stop_vix_mean, 0)}` : ''}</div></div>
      ${j && j.recommended_pop != null && j.recommended_hit != null ? `<div class="kpi"><div class="k">Weekly model: said → did</div>
        <div class="v" style="font-size:19px">${j.recommended_pop}% <span class="dimtxt">→</span> <span class="${j.recommended_hit < j.recommended_pop ? 'neg' : 'pos'}">${j.recommended_hit}%</span></div>
        <div class="s">predicted vs actual hit rate · n=${j.recommended_n || 0}</div></div>` : ''}
    </div>
    <h2 style="margin-top:24px">Every session, oldest to newest${partial}</h2>
    <div class="strip" id="trstrip"></div>
    <div class="legend"><span><i class="swatch" style="background:var(--up)"></i>made money</span>
      <span><i class="swatch" style="background:var(--down)"></i>lost money</span><span>● stopped out</span><span>stronger colour = bigger day · hover for details</span></div>
    <h2 style="margin-top:24px">Running total, per lot</h2>
    <div class="chartbox" id="trcurve"></div>`;
  const mx = Math.max(1, ...nets.map(Math.abs));
  const strip = $('#trstrip');
  rows.forEach(r => {
    const v = +r.net_rs, a = 0.45 + 0.55 * Math.abs(v) / mx;
    const q = el('div', 'sq' + (r.exit_reason === 'stop' ? ' stop' : ''));
    q.style.background = `color-mix(in srgb, ${v >= 0 ? 'var(--up)' : 'var(--down)'} ${Math.round(a * 100)}%, transparent)`;
    q.onmouseenter = () => { const b = q.getBoundingClientRect(), h = strip.getBoundingClientRect();
      showTip(strip, b.left - h.left + 13, b.top - h.top,
        `<b>${fmtDate(r.date)}</b><br>${sign(v)} <span class="r">· ${esc(r.exit_reason || '')}${r.exit ? ' at ' + esc(r.exit) : ''}</span>`); };
    q.onmouseleave = hideTip;
    strip.appendChild(q);
  });
  if(!rows.length) strip.innerHTML = '<div class="empty" style="width:100%">no settled sessions yet</div>';
  if(curve.length > 1) lineChart($('#trcurve'), [{name: 'running total ₹', values: curve, color: '#10b981', w: 3}],
    {xLabels: rows.map(r => String(r.date).slice(5)), fmtY: v => (Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)), W: 1100, H: 260});
  else $('#trcurve').innerHTML = '<div class="empty">needs two settled sessions</div>';
}

window.V2 = {today, trade, proof};
})();

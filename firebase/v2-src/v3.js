/* ======================================================= v3: strategy builder
 *
 * Replaces the old builder, which had two faults: it needed the SIGNAL payload
 * (S) for spot and sigma, so when the structures step failed -- as it did every
 * expiry evening on the settled contract -- it drew nothing at all; and it had
 * no catalogue, only the engine's seven short-premium structures.
 *
 * This one stands on the option CHAIN alone (spot, forward, lot, per-strike IV)
 * and offers the catalogue from catalogue.py, grouped Bullish / Bearish /
 * Neutral. The definitions are baked in by build.py from that one Python file,
 * so the tiles and the server's scored table cannot disagree about what a
 * strategy is.
 *
 * Two models, each labelled where it is shown:
 *   - the CURVES (at expiry and on a target date) reprice every leg with
 *     Black-Scholes at that strike's own implied volatility, off the parity
 *     forward -- the same inputs the chain's IV column is solved from;
 *   - POP uses the site's normal (Bachelier) model at the chain's sigma, the
 *     model every other POP on the site uses.
 * Margin is a rough, labelled estimate -- never a broker's number.
 *
 * Also here: the scored catalogue table in chapter 3 (never recommended), and
 * the headline card's India / Global / High-impact filters.
 */
(function(){
'use strict';

const CATDEF = /*CATALOGUE*/{"groups":[],"strategies":[]}/*END*/;
const SB = window.SB = {};

/* ------------------------------------------------------------------ maths */
const SQ2PI = Math.sqrt(2 * Math.PI);
const npdf = z => Math.exp(-0.5 * z * z) / SQ2PI;
const ncdf = z => 0.5 * (1 + erf(z / Math.SQRT2));
const intr = (x, K, kind) => kind === 'CE' ? Math.max(0, x - K) : Math.max(0, K - x);

/** Black-Scholes on a forward, zero rate. T in years, vol a fraction. */
function bs(F, K, T, vol, kind){
  if(!(T > 0) || !(vol > 0) || F <= 0) return intr(F, K, kind);
  const s = vol * Math.sqrt(T), d1 = (Math.log(F / K) + 0.5 * s * s) / s, d2 = d1 - s;
  return kind === 'CE' ? F * ncdf(d1) - K * ncdf(d2) : K * ncdf(-d2) - F * ncdf(-d1);
}
/** Per unit: delta (per point), gamma, theta (per calendar day), vega (per vol point). */
function bsGreeks(F, K, T, vol, kind){
  if(!(T > 0) || !(vol > 0)){
    const itm = kind === 'CE' ? F > K : F < K;
    return {delta: itm ? (kind === 'CE' ? 1 : -1) : 0, gamma: 0, theta: 0, vega: 0};
  }
  const s = vol * Math.sqrt(T), d1 = (Math.log(F / K) + 0.5 * s * s) / s;
  const pdf = npdf(d1);
  return {
    delta: kind === 'CE' ? ncdf(d1) : ncdf(d1) - 1,
    gamma: pdf / (F * s),
    theta: -(F * pdf * vol) / (2 * Math.sqrt(T)) / 365,
    vega: F * pdf * Math.sqrt(T) / 100,
  };
}

/* ------------------------------------------------------ calibrated POP */
// The same distribution calpop.py uses on the server (published by the
// publisher as /live/calibration): realised NIFTY moves in session-clock
// sigma, centred on the forward, chosen on a held-out year. Tabulated once.
let CALTAB = null;
function calTable(){
  if(CALTAB !== null) return CALTAB;
  const c = (SRC && SRC.live && SRC.live.calibration) || null;
  if(!c || !c.z || !c.z.length){ return null; }
  const lo = -10, hi = 10, n = 4001, dq = (hi - lo) / (n - 1), h = c.bandwidth, zs = c.z;
  const t = new Float64Array(n);
  for(let i = 0; i < n; i++){
    const q = lo + i * dq; let acc = 0;
    for(const z of zs) acc += ncdf((q - z) / h);
    t[i] = acc / zs.length;
  }
  CALTAB = {t, lo, hi, n, meta: c};
  return CALTAB;
}
function calCdf(level, fwd, sig){
  const T = calTable(); if(!T) return null;
  const q = (level - fwd) / sig;
  if(q <= T.lo) return 0; if(q >= T.hi) return 1;
  const x = (q - T.lo) / (T.hi - T.lo) * (T.n - 1), i = Math.floor(x);
  return T.t[i] + (T.t[Math.min(i + 1, T.n - 1)] - T.t[i]) * (x - i);
}

/* ------------------------------------------------------------------ state */
let LEGSX = [];              // {id, exp, strike, kind, side(+1/-1), lots, premium, custom}
let GROUP = 'bullish';
let WIDTHMULT = 2;           // strike width = WIDTHMULT x step
let DAYOFF = 0;              // target date, days after the priced session
let CHAINS = {};             // expiry -> chain payload
let EXPS = [];               // live expiries offered, near first
let NEAR = null;
let CHAINEXP = null;         // which expiry the click-to-add chain shows
let KEY = '';                // UND|SESSION the legs belong to
let PINS = {A: null, B: null};
let SCORED = null;           // structures payload (for tile badges), may be null
let NEXTID = 1;
let STRATNAME = '';

const esc2 = t => String(t == null ? '' : t).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtExp = e => e ? new Date(e + 'T00:00:00Z').toLocaleDateString('en-IN',
  {day: 'numeric', month: 'short', timeZone: 'UTC'}) : '–';
const rsS = v => v == null ? '–' : (v < 0 ? '−' : '') + '₹' + num(Math.abs(Math.round(v)));
const signRs = v => v == null ? '–'
  : `<span class="${v > 0 ? 'pos' : (v < 0 ? 'neg' : 'dimtxt')}">${v > 0 ? '+' : ''}${rsS(v)}</span>`;

function near(){ return CHAINS[NEAR]; }
function row(exp, strike){
  const c = CHAINS[exp];
  return c && c.rows ? c.rows.find(r => r.strike === strike) : null;
}
function mid(exp, strike, kind){
  const r = row(exp, strike), q = r && r[kind];
  return q && q.mid != null ? q.mid : null;
}
function strikesOf(exp){ const c = CHAINS[exp]; return c && c.rows ? c.rows.map(r => r.strike) : []; }
function stepOf(){ const c = near(); return (c && c.step) || 50; }
function atmOf(){
  const c = near(); if(!c || !c.rows || !c.rows.length) return null;
  const r = c.rows.find(x => x.atm);
  return r ? r.strike : c.atm_strike;
}
function dteOf(exp){ const c = CHAINS[exp]; return c ? (c.dte || 0) : 0; }
function lotOf(){ const c = near(); return (c && c.lot) || 65; }
function spotOf(){ const c = near(); return c ? (c.spot || c.forward) : null; }

/** Annual vol for a leg: its own IV, else the chain's ATM IV, else the chain's
 *  volatility in VIX units. The source is shown in the legs table. */
function volOf(exp, strike, kind){
  const r = row(exp, strike), q = r && r[kind];
  if(q && q.iv) return {v: q.iv / 100, src: 'iv'};
  const c = CHAINS[exp];
  const a = c && c.rows && c.rows.find(x => x.atm);
  const aiv = a && ((a.CE && a.CE.iv) || (a.PE && a.PE.iv));
  if(aiv) return {v: aiv / 100, src: 'atm'};
  const u = c && (c.sigma_vol_units || c.vix);
  return {v: (u || 12) / 100, src: 'vix'};
}
/** Forward for a leg's expiry when the index is at x, t days after the
 *  session. The chain's forward/spot basis shrinks with the time left, so a
 *  leg at its own expiry prices off the index itself -- without that, a
 *  calendar's far leg carried the whole basis to the near expiry and the
 *  calendar looked like it had unlimited profit deep in the money. */
function fwdAt(exp, x, t){
  const c = CHAINS[exp];
  const b = c && c.forward && c.spot ? c.forward / c.spot : 1;
  const d = dteOf(exp);
  const f = d > 0 ? Math.max(d - (t || 0), 0) / d : 0;
  return x * Math.pow(b, f);
}

/* --------------------------------------------------------------- analysis */

/** Value per unit of one leg at index x, `t` days after the session. */
function legValue(l, x, t){
  const rem = dteOf(l.exp) - t;
  if(rem <= 0) return intr(x, l.strike, l.kind);
  const days = t === 0 ? Math.max(dteOf(l.exp), 1) : rem;
  return bs(fwdAt(l.exp, x, t), l.strike, days / 365, volOf(l.exp, l.strike, l.kind).v, l.kind);
}
function pnlAt(legs, x, t, lot){
  let v = 0;
  for(const l of legs) v += l.side * l.lots * (legValue(l, x, t) - l.premium);
  return v * lot;
}
function horizonOf(legs){ return Math.min(...legs.map(l => dteOf(l.exp))); }
function horizonExp(legs){
  const h = horizonOf(legs);
  return (legs.find(l => dteOf(l.exp) === h) || {}).exp;
}

function analyse(legs){
  const c = near(); const lot = lotOf(), spot = spotOf();
  if(!legs.length || !c || !spot) return null;
  const H = horizonOf(legs), hexp = horizonExp(legs);
  const tday = Math.min(DAYOFF, H);
  const hsig = (CHAINS[hexp] && CHAINS[hexp].sigma) || c.sigma || spot * 0.01;
  // The distribution is centred on the first expiry's parity forward -- where
  // its premiums say the index is expected to be -- not on the spot print.
  const ctr = (CHAINS[hexp] && CHAINS[hexp].forward) || spot;

  // plotting window
  const reach = Math.max(...legs.map(l => Math.abs(l.strike / spot - 1)));
  const span = Math.min(0.14, Math.max(0.03, reach * 1.9 + (hsig / spot) * 1.4));
  const lo = spot * (1 - span), hi = spot * (1 + span), n = 241;
  let xs = Array.from({length: n}, (_, i) => lo + (hi - lo) * i / (n - 1));
  xs = [...new Set(xs.concat(legs.map(l => l.strike).filter(k => k > lo && k < hi)))].sort((a, b) => a - b);
  const curve = xs.map(x => ({x, e: pnlAt(legs, x, H, lot), t: pnlAt(legs, x, tday, lot)}));

  // bounds, breakevens and POP on a WIDE grid, so a wing just off the chart
  // still counts and "unlimited" means still falling at +/-45%.
  const wl = spot * 0.55, wh = spot * 1.45, wn = 1801;
  let wx = Array.from({length: wn}, (_, i) => wl + (wh - wl) * i / (wn - 1));
  wx = [...new Set(wx.concat(legs.map(l => l.strike)))].sort((a, b) => a - b);
  const wy = wx.map(x => pnlAt(legs, x, H, lot));
  // "Still moving" at an edge means a slope of at least 0.05 per point per
  // unit -- far below any naked leg (1 per point), far above what a forward
  // basis or a rounding error produces.
  const eps = 0.05 * lot * (wh - wl) / (wn - 1);
  const lossUnb = wy[0] < wy[1] - eps || wy[wy.length - 1] < wy[wy.length - 2] - eps;
  const profUnb = wy[0] > wy[1] + eps || wy[wy.length - 1] > wy[wy.length - 2] + eps;
  const maxP = Math.max(...wy), maxL = Math.min(...wy);
  // Breakevens within 6 sigma of spot, the same window the server scores on;
  // a crossing 40% away is a basis artefact, not a level anyone trades to.
  const bes = [];
  for(let i = 0; i < wx.length - 1; i++){
    if(Math.abs(wx[i] - ctr) > 6 * hsig) continue;
    const a = wy[i], b = wy[i + 1];
    if(a === 0 && (i === 0 || wy[i - 1] !== 0)) bes.push(wx[i]);
    else if(a * b < 0) bes.push(wx[i] + (-a / (b - a)) * (wx[i + 1] - wx[i]));
  }
  let pop = 0;
  for(let i = 0; i < wx.length - 1; i++)
    if((wy[i] + wy[i + 1]) / 2 > 0)
      pop += ncdf((wx[i + 1] - ctr) / hsig) - ncdf((wx[i] - ctr) / hsig);

  // Calibrated POP -- only where the server published the calibration for
  // this expiry (NIFTY, the first expiry the structures payload covers).
  let popCal = null;
  const calib = SCORED && SCORED.calibration;
  if(calib && calib.sigma_session && SCORED.expiry === hexp && calTable()){
    popCal = 0;
    let c0 = calCdf(wx[0], calib.forward, calib.sigma_session);
    if(wy[0] > 0) popCal += c0;
    for(let i = 0; i < wx.length - 1; i++){
      const c1 = calCdf(wx[i + 1], calib.forward, calib.sigma_session);
      if((wy[i] + wy[i + 1]) / 2 > 0) popCal += c1 - c0;
      c0 = c1;
    }
    if(wy[wy.length - 1] > 0) popCal += 1 - c0;
  }

  const net = legs.reduce((s, l) => s - l.side * l.lots * l.premium, 0) * lot;   // + credit
  // margin: the same rule as catalogue.margin_estimate
  const mpl = c.margin_per_lot || 0.10 * spot * lot;
  let margin;
  if(!lossUnb) margin = Math.max(Math.abs(Math.min(0, maxL)), Math.max(0, -net));
  else {
    let naked = 0;
    for(const k of ['CE', 'PE']){
      const sh = legs.filter(l => l.kind === k && l.side < 0).reduce((s, l) => s + l.lots, 0);
      const lg = legs.filter(l => l.kind === k && l.side > 0).reduce((s, l) => s + l.lots, 0);
      naked += Math.max(0, sh - lg);
    }
    margin = mpl * Math.max(1, naked);
  }
  // greeks, today, at spot
  const g = {delta: 0, gamma: 0, theta: 0, vega: 0};
  for(const l of legs){
    const T = Math.max(dteOf(l.exp), 1) / 365;
    const gg = bsGreeks(fwdAt(l.exp, spot, 0), l.strike, T, volOf(l.exp, l.strike, l.kind).v, l.kind);
    for(const k in g) g[k] += l.side * l.lots * gg[k] * lot;
  }
  return {curve, spot, ctr, hsig, H, hexp, tday, lot, maxP, maxL, lossUnb, profUnb,
          bes, pop, popCal, net, margin, greeks: g,
          rr: (!lossUnb && !profUnb && maxL < 0) ? maxP / Math.abs(maxL) : null};
}

/* ------------------------------------------------------------------ chart */
function sbChart(host, A, opts){
  opts = opts || {};
  // Drawn at the box's real width (clamped), so tick labels stay legible on a
  // phone instead of a 1100-unit drawing shrunk to a third of its size.
  const bw = host.clientWidth || 1100;
  const W = opts.W ? Math.min(opts.W, Math.max(340, bw)) : Math.min(1100, Math.max(340, bw));
  const Hh = opts.H || (W < 600 ? 280 : 360), PL = W < 600 ? 52 : 70, PR = 16, PT = 26, PB = 34;
  host.innerHTML = '';
  const cv = A.curve, xs = cv.map(p => p.x);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const showT = A.tday < A.H;
  const all = cv.flatMap(p => showT ? [p.e, p.t] : [p.e]);
  let yHi = Math.max(...all), yLo = Math.min(...all);
  let clipped = false;
  const ref = Math.max(Math.abs(A.net), 1);
  if(A.lossUnb && yLo < -3 * Math.max(yHi, ref)){ yLo = -3 * Math.max(yHi, ref); clipped = true; }
  if(A.profUnb && yHi > 3 * Math.max(Math.abs(yLo), ref)){ yHi = 3 * Math.max(Math.abs(yLo), ref); clipped = true; }
  const pad = (yHi - yLo) * 0.12 || 1; yHi += pad; yLo -= pad;
  if(yHi < 0) yHi = pad;
  if(yLo > 0) yLo = -pad;
  if(opts.yRange){ yLo = opts.yRange[0]; yHi = opts.yRange[1]; }
  const X = v => PL + (v - x0) / (x1 - x0) * (W - PL - PR);
  const Y = v => PT + (yHi - Math.max(yLo, Math.min(yHi, v))) / (yHi - yLo) * (Hh - PT - PB);
  const s = svg(W, Hh); s.classList.add('sbsvg');

  if(A.hsig){
    const a = X(Math.max(x0, A.ctr - A.hsig)), b = X(Math.min(x1, A.ctr + A.hsig));
    s.appendChild(node('rect', {x: a, y: PT, width: Math.max(0, b - a), height: Hh - PT - PB, class: 'sb-band'}));
    s.appendChild(node('text', {x: (a + b) / 2, y: PT + 13, class: 'sb-bandlbl', 'text-anchor': 'middle'}, '±1σ by expiry'));
  }
  niceTicks(yLo, yHi, 5).forEach(v => {
    s.appendChild(node('line', {x1: PL, y1: Y(v), x2: W - PR, y2: Y(v), class: v === 0 ? 'sb-zero' : 'sb-grid'}));
    s.appendChild(node('text', {x: PL - 9, y: Y(v) + 4, class: 'sb-tick', 'text-anchor': 'end'},
      Math.abs(v) >= 1000 ? (v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1) + 'k' : v.toFixed(0)));
  });
  niceTicks(x0, x1, W < 600 ? 3 : 6).forEach(v => {
    s.appendChild(node('line', {x1: X(v), y1: PT, x2: X(v), y2: Hh - PB, class: 'sb-grid'}));
    s.appendChild(node('text', {x: X(v), y: Hh - 12, class: 'sb-tick', 'text-anchor': 'middle'}, num(v)));
  });
  const zY = Y(0);
  const area = f => 'M' + X(x0) + ',' + zY + ' ' + cv.map(p => 'L' + X(p.x).toFixed(1) + ',' + Y(f(p.e)).toFixed(1)).join(' ') + ' L' + X(x1) + ',' + zY + ' Z';
  s.appendChild(node('path', {d: area(v => Math.max(0, v)), class: 'sb-profit'}));
  s.appendChild(node('path', {d: area(v => Math.min(0, v)), class: 'sb-loss'}));
  A.bes.filter(b => b > x0 && b < x1).forEach(b => {
    s.appendChild(node('line', {x1: X(b), y1: PT, x2: X(b), y2: Hh - PB, class: 'sb-be'}));
    s.appendChild(node('text', {x: X(b), y: PT - 8, class: 'sb-belbl', 'text-anchor': 'middle'}, num(b)));
  });
  s.appendChild(node('line', {x1: X(A.spot), y1: PT, x2: X(A.spot), y2: Hh - PB, class: 'sb-spot'}));
  s.appendChild(node('text', {x: X(A.spot), y: Hh - PB - 6, class: 'sb-spotlbl', 'text-anchor': 'middle'}, 'spot ' + num(A.spot)));
  const line = (f, cls) => s.appendChild(node('polyline', {points: cv.map(p => X(p.x).toFixed(1) + ',' + Y(f(p)).toFixed(1)).join(' '), class: cls}));
  if(showT) line(p => p.t, 'sb-target');
  line(p => p.e, 'sb-expiry');

  const hit = node('rect', {x: PL, y: PT, width: W - PL - PR, height: Hh - PT - PB, fill: 'transparent', style: 'cursor:crosshair'});
  const cross = node('line', {x1: 0, y1: PT, x2: 0, y2: Hh - PB, class: 'sb-cross', opacity: 0});
  const k1 = node('circle', {r: 4.5, class: 'sb-knob', opacity: 0});
  const k2 = node('circle', {r: 4, class: 'sb-knob2', opacity: 0});
  s.appendChild(cross); s.appendChild(k1); s.appendChild(k2); s.appendChild(hit);
  hit.addEventListener('mousemove', ev => {
    const r = s.getBoundingClientRect();
    const lv = x0 + ((ev.clientX - r.left) / r.width * W - PL) / (W - PL - PR) * (x1 - x0);
    let b = cv[0]; for(const p of cv) if(Math.abs(p.x - lv) < Math.abs(b.x - lv)) b = p;
    cross.setAttribute('x1', X(b.x)); cross.setAttribute('x2', X(b.x)); cross.setAttribute('opacity', 1);
    k1.setAttribute('cx', X(b.x)); k1.setAttribute('cy', Y(b.e)); k1.setAttribute('opacity', 1);
    k2.setAttribute('cx', X(b.x)); k2.setAttribute('cy', Y(b.t)); k2.setAttribute('opacity', showT ? 1 : 0);
    const mv = (b.x / A.spot - 1) * 100;
    showTip(host, X(b.x) / W * host.clientWidth, Y(b.e) / Hh * host.clientHeight,
      `<b>${num(b.x)}</b> <span class="r">${mv > 0 ? '+' : ''}${mv.toFixed(2)}%</span><br>`
      + `at expiry ${signRs(b.e)}` + (showT ? `<br>on target date ${signRs(b.t)}` : ''));
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); k1.setAttribute('opacity', 0); k2.setAttribute('opacity', 0); hideTip(); });
  host.appendChild(s);
  const lg = el('div', 'legend');
  lg.innerHTML = `<span><i class="sbkey e"></i> at expiry (${fmtExp(A.hexp)})</span>`
    + (showT ? `<span><i class="sbkey t"></i> target date (${A.tday === 0 ? 'today' : '+' + A.tday + 'd'})</span>` : '')
    + `<span><i class="sbkey be"></i> breakeven</span><span><i class="sbkey sd"></i> ±1σ range</span>`;
  host.appendChild(lg);
  if(clipped) host.appendChild(el('p', 'note', 'The vertical axis is clipped: the line keeps going past the edge on the unlimited side.'));
}

/* ------------------------------------------------------------- tile icons */
function iconSvg(def){
  // Generic strikes (100, width 10) from the definition alone, so an icon
  // draws before any chain has loaded. A calendar's tent is drawn by hand.
  const s = svg(80, 36); s.classList.add('sbico');
  let pts;
  if(def.type === 'calendar'){
    pts = [[0, 28], [24, 24], [40, 6], [56, 24], [80, 28]];
  } else {
    const ys = [];
    for(let i = 0; i <= 40; i++){
      const x = 70 + i * 1.5; let v = 0;
      for(const l of def.legs) v += l.qty * intr(x, 100 + l.off * 10, l.kind);
      ys.push(v);
    }
    const lo = Math.min(...ys), hi = Math.max(...ys), r = (hi - lo) || 1;
    pts = ys.map((v, i) => [i * 2, 32 - (v - lo) / r * 28]);
  }
  s.appendChild(node('polyline', {points: pts.map(p => p.join(',')).join(' '), class: 'sbico-l'}));
  return s.outerHTML;
}

/* ---------------------------------------------------------------- loading */
async function loadChains(){
  const list = (typeof expiryRows === 'function' ? expiryRows() : []).map(x => x.expiry);
  NEAR = EXPIRY || list[0] || null;
  const i = list.indexOf(NEAR);
  EXPS = NEAR ? [NEAR].concat(i >= 0 && list[i + 1] ? [list[i + 1]] : []) : [];
  const errs = {};
  for(const e of EXPS){
    try{ CHAINS[e] = await DATA.chain(e); }
    catch(err){ delete CHAINS[e]; errs[e] = err.message || String(err); }
  }
  for(const k of Object.keys(CHAINS)) if(!EXPS.includes(k)) delete CHAINS[k];
  if(!CHAINEXP || !CHAINS[CHAINEXP]) CHAINEXP = NEAR;
  return {list, errs};
}

SB.load = async function(){
  const key = UND + '|' + (SESSION || '');
  if(key !== KEY){ LEGSX = []; PINS = {A: null, B: null}; KEY = key; CHAINS = {}; STRATNAME = ''; }
  let info;
  try{ info = await loadChains(); }catch(e){ info = {list: [], errs: {_: e.message}}; }
  try{ SCORED = NEAR ? await DATA.structures(NEAR) : null; }catch(e){ SCORED = null; }
  // Unedited legs follow the market; a price you typed stays put.
  for(const l of LEGSX) if(!l.custom){ const p = mid(l.exp, l.strike, l.kind); if(p != null) l.premium = p; }
  LEGSX = LEGSX.filter(l => CHAINS[l.exp]);
  // The page refreshes every 30 seconds. Repainting under someone who is
  // typing a price or dragging the date slider would throw the edit away, so
  // that refresh is skipped; the next one catches up.
  const a = document.activeElement, host = $('#sb');
  if(host && a && host.contains(a) && /^(INPUT|SELECT)$/.test(a.tagName) && host.dataset.painted) return;
  if(host) host.dataset.painted = '1';
  renderAll(info);
};

/* -------------------------------------------------------------- rendering */
function renderAll(info){
  const host = $('#sb');
  if(!host) return;
  const c = near();
  const status = $('#sbstatus');
  if(!c){
    const other = (info.list || []).filter(e => e !== NEAR);
    status.innerHTML = `<div class="sbwarn"><b>No option chain for ${esc2(NEAR ? fmtExp(NEAR) : 'this expiry')}.</b>
      ${esc2((info.errs || {})[NEAR] || 'Nothing has been published for it yet.')}
      ${other.length ? '<br>Try another expiry: ' + other.map(e => `<button class="btn sbgo" data-exp="${e}">${fmtExp(e)}</button>`).join(' ') : ''}</div>`;
    status.querySelectorAll('.sbgo').forEach(b => b.onclick = () => goExpiry(b.dataset.exp));
    for(const id of ['#sbtiles', '#sblegs', '#sbout', '#sbchain']) $(id).innerHTML = '';
    return;
  }
  const notes = [];
  if(c.usable < 15) notes.push(`Only <b>${c.usable}</b> strikes on ${fmtExp(NEAR)} have a two-sided quote `
    + `(${c.dropped_one_sided} one-sided, ${c.dropped_wide} too wide). Strategies needing a missing strike are greyed out.`);
  if(EXPS.length < 2) notes.push('The next expiry is not published, so calendar spreads are unavailable.');
  else if(!CHAINS[EXPS[1]]) notes.push(`The ${fmtExp(EXPS[1])} chain did not load: ${esc2((info.errs || {})[EXPS[1]] || '')}. Calendars are unavailable.`);
  status.innerHTML = `<div class="sbmeta">
      <span>${esc2(UND)} spot <b>${num(c.spot, 2)}</b></span>
      <span>forward <b>${num(c.forward, 1)}</b></span>
      <span>ATM <b>${num(atmOf())}</b></span>
      <span>lot <b>${lotOf()}</b></span>
      <span>expiries <b>${EXPS.map(fmtExp).join(' · ')}</b></span>
      <span class="dimtxt">chain as of ${esc2((c.as_of || '').slice(11, 16))} IST · session ${esc2(c.session || '')}</span>
    </div>` + (notes.length ? `<div class="sbwarn">${notes.join('<br>')}</div>` : '');
  renderTiles(); renderLegsX(); renderOut(); renderChainX(); renderPins();
}

function goExpiry(e){
  const sel = $('#expsel');
  if(sel && [...sel.options].some(o => o.value === e)){ sel.value = e; if(sel.onchange) sel.onchange(); }
}

function buildDef(def){
  const atm = atmOf(), w = WIDTHMULT * stepOf();
  const legs = [];
  for(const l of def.legs){
    const exp = EXPS[l.exp];
    if(!exp || !CHAINS[exp]) return {why: 'needs the next expiry, which is not loaded'};
    const k = atm + l.off * w, p = mid(exp, k, l.kind);
    if(p == null) return {why: `no two-sided quote at ${num(k)} ${l.kind} (${fmtExp(exp)})`};
    legs.push({id: NEXTID++, exp, strike: k, kind: l.kind, side: l.qty > 0 ? 1 : -1,
               lots: Math.abs(l.qty), premium: p, custom: false});
  }
  return {legs};
}

function badge(def){
  const r = SCORED && SCORED.catalogue && SCORED.catalogue.find(x => x.key === def.key);
  if(!r || !r.available || (r.width && r.width !== WIDTHMULT * stepOf())) return '';
  return `<span class="sbbadge" title="Scored on the published chain at the default width: model EV after estimated costs, and POP. Never a recommendation.">`
    + `EV ${signRs(r.net_ev_rs)} · POP ${pct(r.pop, 0)}</span>`;
}

function renderTiles(){
  const groups = CATDEF.groups || [];
  const tabs = groups.map(g => `<button class="sbtab ${g.key === GROUP ? 'on' : ''} g-${g.key}" data-g="${g.key}">${esc2(g.label)}
      <small>${CATDEF.strategies.filter(s => s.group === g.key).length}</small></button>`).join('');
  const widths = [1, 2, 3, 4, 6].map(m => `<option value="${m}"${m === WIDTHMULT ? ' selected' : ''}>${num(m * stepOf())} pts</option>`).join('');
  const tiles = CATDEF.strategies.filter(s => s.group === GROUP).map(def => {
    const t = buildDef(def);
    const dis = !!t.why;
    return `<button class="sbtile${dis ? ' dis' : ''}${STRATNAME === def.label ? ' on' : ''}" data-k="${def.key}" title="${esc2(dis ? 'Unavailable: ' + t.why : def.blurb)}">
      ${iconSvg(def)}<span class="nm">${esc2(def.label)}</span>
      <span class="fam">${esc2(def.family || def.type)}</span>${dis ? `<span class="why">${esc2(t.why)}</span>` : badge(def)}</button>`;
  }).join('');
  const engine = (GROUP === 'neutral' && SCORED && SCORED.structures) ? `<div class="sbeng"><span class="dimtxt">Today's engine structures:</span> `
    + SCORED.structures.map(x => `<button class="chip sbengb" data-n="${esc2(x.name)}">${esc2(x.label.toLowerCase())}${x.recommended ? ' ★' : ''}</button>`).join('') + '</div>' : '';
  $('#sbtiles').innerHTML = `<div class="sbtabs">${tabs}<label class="sbw">strike width <select id="sbwidth">${widths}</select></label></div>
    <div class="sbgrid">${tiles}</div>${engine}`;
  $('#sbtiles').querySelectorAll('.sbtab').forEach(b => b.onclick = () => { GROUP = b.dataset.g; renderTiles(); });
  $('#sbwidth').onchange = e => { WIDTHMULT = +e.target.value; renderTiles(); };
  $('#sbtiles').querySelectorAll('.sbtile').forEach(b => b.onclick = () => {
    const def = CATDEF.strategies.find(s => s.key === b.dataset.k);
    const t = buildDef(def);
    if(t.why) return;
    LEGSX = t.legs; STRATNAME = def.label; DAYOFF = 0;
    renderTiles(); renderLegsX(); renderOut(); renderChainX();
  });
  $('#sbtiles').querySelectorAll('.sbengb').forEach(b => b.onclick = () => {
    const st = SCORED.structures.find(x => x.name === b.dataset.n);
    LEGSX = st.legs.map(l => {
      const m = mid(NEAR, l.strike, l.kind);
      return {id: NEXTID++, exp: NEAR, strike: l.strike, kind: l.kind, side: l.qty < 0 ? -1 : 1,
              lots: Math.abs(l.qty) || 1, premium: m != null ? m : l.premium, custom: false};
    });
    STRATNAME = st.label.toLowerCase(); DAYOFF = 0;
    renderTiles(); renderLegsX(); renderOut(); renderChainX();
  });
}

function changed(){ STRATNAME = ''; renderTiles(); renderLegsX(); renderOut(); renderChainX(); }

function renderLegsX(){
  const box = $('#sblegs');
  const head = `<div class="sblhead"><h2>Legs${STRATNAME ? ' &mdash; <span class="dimtxt">' + esc2(STRATNAME) + '</span>' : ''}</h2>
    <div class="row" style="gap:6px"><button class="btn" id="sbadd">+ add leg</button><button class="btn" id="sbclear">clear</button></div></div>`;
  if(!LEGSX.length){
    box.innerHTML = head + '<div class="empty">Pick a strategy above, add a leg, or click a premium in the chain below.</div>';
  } else {
    const rows = LEGSX.map(l => {
      const v = volOf(l.exp, l.strike, l.kind), m = mid(l.exp, l.strike, l.kind);
      const ks = strikesOf(l.exp);
      const vt = v.src === 'iv' ? "this strike's implied volatility"
               : v.src === 'atm' ? 'no IV at this strike: the ATM IV is used'
               : 'no IV: the chain volatility (VIX units) is used';
      return `<tr data-id="${l.id}">
        <td><button class="sbside ${l.side > 0 ? 'b' : 's'}" data-a="side" title="click to switch buy / sell">${l.side > 0 ? 'B' : 'S'}</button></td>
        <td><select data-a="exp">${EXPS.filter(e => CHAINS[e]).map(e => `<option value="${e}"${e === l.exp ? ' selected' : ''}>${fmtExp(e)}</option>`).join('')}</select></td>
        <td><select data-a="strike">${ks.map(k => `<option value="${k}"${k === l.strike ? ' selected' : ''}>${num(k)}</option>`).join('')}</select></td>
        <td><button class="sbkind" data-a="kind" title="click to switch call / put">${l.kind}</button></td>
        <td><span class="stepper"><button data-a="lm">–</button><span>${l.lots}</span><button data-a="lp">+</button></span></td>
        <td><input class="sbpx${l.custom ? ' custom' : ''}" data-a="px" type="number" step="0.05" min="0" value="${(+l.premium).toFixed(2)}" title="Entry price. Type one to price a position you already hold; untouched legs follow the chain's mid."></td>
        <td class="dimtxt">${m != null ? num(m, 2) : '–'}</td>
        <td class="dimtxt" title="${vt}">${(v.v * 100).toFixed(1)}%${v.src === 'iv' ? '' : '*'}</td>
        <td><button class="x" data-a="del" title="remove leg">×</button></td></tr>`;
    }).join('');
    box.innerHTML = head + `<div class="scroll"><table class="sbltbl"><thead><tr><th>B/S</th><th>expiry</th><th>strike</th><th>type</th><th>lots</th><th>entry</th><th>mid</th><th>IV</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
    box.querySelectorAll('tr[data-id]').forEach(tr => {
      const l = LEGSX.find(x => x.id === +tr.dataset.id);
      const reprice = () => { const p = mid(l.exp, l.strike, l.kind); if(p != null){ l.premium = p; l.custom = false; return true; } return false; };
      tr.querySelectorAll('[data-a]').forEach(c => {
        const a = c.dataset.a;
        if(a === 'side') c.onclick = () => { l.side = -l.side; changed(); };
        if(a === 'kind') c.onclick = () => { const was = l.kind; l.kind = l.kind === 'CE' ? 'PE' : 'CE'; if(!reprice()) l.kind = was; changed(); };
        if(a === 'lm') c.onclick = () => { l.lots = Math.max(1, l.lots - 1); changed(); };
        if(a === 'lp') c.onclick = () => { l.lots += 1; changed(); };
        if(a === 'del') c.onclick = () => { LEGSX = LEGSX.filter(x => x !== l); changed(); };
        if(a === 'strike') c.onchange = e => { const was = l.strike; l.strike = +e.target.value; if(!reprice()) l.strike = was; changed(); };
        if(a === 'exp') c.onchange = e => {
          const was = l.exp; l.exp = e.target.value;
          if(!strikesOf(l.exp).includes(l.strike) || !reprice()) l.exp = was;
          changed(); };
        if(a === 'px') c.onchange = e => { const v = +e.target.value; if(v >= 0){ l.premium = v; l.custom = true; } renderLegsX(); renderOut(); };
      });
    });
  }
  $('#sbadd').onclick = () => {
    const k = atmOf(), p = mid(NEAR, k, 'CE');
    if(p == null) return;
    LEGSX.push({id: NEXTID++, exp: NEAR, strike: k, kind: 'CE', side: 1, lots: 1, premium: p, custom: false});
    changed();
  };
  $('#sbclear').onclick = () => { LEGSX = []; changed(); };
}

function pnlRows(A){
  const ks = [-2, -1, -0.5, 0, 0.5, 1, 2], stp = stepOf();
  return ks.map(k => {
    const x = k === 0 ? A.spot : Math.round((A.ctr + k * A.hsig) / stp) * stp;
    return `<tr${k === 0 ? ' class="atm"' : ''}><td class="l">${k === 0 ? 'spot' : (k > 0 ? '+' : '−') + Math.abs(k) + 'σ'}</td><td>${num(x)}</td>
      <td class="dimtxt">${((x / A.spot - 1) * 100).toFixed(2)}%</td>
      <td>${signRs(pnlAt(LEGSX, x, A.tday, A.lot))}</td><td>${signRs(pnlAt(LEGSX, x, A.H, A.lot))}</td></tr>`;
  }).join('');
}

function renderOut(){
  const out = $('#sbout');
  if(!LEGSX.length){ out.innerHTML = '<div class="card"><h2>Payoff</h2><div class="empty">Add legs to see the payoff chart.</div></div>'; return; }
  const A = analyse(LEGSX);
  if(!A){ out.innerHTML = '<div class="card"><div class="empty">Cannot price: the chain has no spot.</div></div>'; return; }
  const cal = new Set(LEGSX.map(l => l.exp)).size > 1;
  const max = A.profUnb ? '<span class="pos">Unlimited</span>' : signRs(A.maxP);
  const min = A.lossUnb ? '<span class="neg">Unlimited</span>' : signRs(A.maxL);
  const kn = (lbl, val, hint) => `<div class="sbk" title="${esc2(hint || '')}"><span>${lbl}</span><b>${val}</b></div>`;
  const beTxt = A.bes.length ? A.bes.map(b => num(b)).join(' / ') : 'none';
  const tLbl = A.tday === 0 ? 'today' : `+${A.tday} day${A.tday > 1 ? 's' : ''}`;

  const tbl = pnlRows(A);
  const g = A.greeks;
  out.innerHTML = `
   <div class="card sbchartcard">
     <div class="sblhead"><h2>Payoff${STRATNAME ? ' &mdash; ' + esc2(STRATNAME) : ''}</h2>
       <div class="row" style="gap:6px"><button class="btn" id="sbpinA">pin as A</button><button class="btn" id="sbpinB">pin as B</button></div></div>
     <div class="sbkeys">
       ${kn(A.net >= 0 ? 'Net credit' : 'Net debit', signRs(A.net), 'Premium received (+) or paid (−) for all legs at the entry prices, lots included.')}
       ${kn('Max profit', max, 'At the first expiry in the position.')}
       ${kn('Max loss', min, 'At the first expiry in the position.')}
       ${kn('Breakeven' + (A.bes.length > 1 ? 's' : ''), beTxt, 'Index levels where the P&L at the first expiry crosses zero.')}
       ${kn('Reward : risk', A.rr != null ? '1 : ' + (1 / A.rr).toFixed(2) : '–', 'Max loss for every 1 of max profit. Only for positions capped on both sides.')}
       ${kn('POP', pct(A.pop, 1) + (A.popCal != null ? ` <span class="sbcal" title="historical-fit POP">· ${pct(A.popCal, 1)} hist.</span>` : ''), "Model POP: chance of any profit at the first expiry under the site's normal model at the chain's sigma (" + num(A.hsig, 0) + ' pts).' + (A.popCal != null ? ' Historical-fit POP (hist.): the same event under the distribution of past NIFTY moves. Tested year by year 2019-2026 it was no more accurate than model POP - see chapter 3. Shown for reference; changes no decision.' : ''))}
       ${kn('Margin (est.)', rsS(A.margin), 'ROUGH ESTIMATE, not broker margin. Capped risk: the max loss (or the debit). Otherwise ' + rsS(near().margin_per_lot || 0.1 * A.spot * A.lot) + " per naked short lot, the site's own per-lot figure. Can be far off for naked shorts.")}
     </div>
     <div class="chartbox" id="sbchart"></div>
     <div class="sbslider"><label>Target date <b id="sbdaylbl">${tLbl}</b>
       <input type="range" id="sbday" min="0" max="${A.H}" step="1" value="${A.tday}"${A.H === 0 ? ' disabled' : ''}></label>
       <span class="dimtxt">${A.H === 0 ? 'expires today, so there is no earlier date to show' : 'slide to see the P&amp;L before expiry (dashed line); days after session ' + esc2(near().session || '')}</span></div>
     <p class="note">Curves: every leg repriced with Black–Scholes at its own strike's implied volatility, off each expiry's parity forward,
     with IV held constant — a volatility spike or crush is not modelled.${cal ? ' The later-expiry leg is still open at the first expiry; its value there is a model estimate, not a quote.' : ''}
     POP uses the site's normal model. Margin is a rough estimate, labelled as such. Costs are not included here; the scored table in chapter 3 includes estimated costs.</p>
   </div>
   <div class="grid g2" style="margin-top:16px">
     <div class="card"><h2>P&amp;L by index level</h2>
       <div class="scroll"><table class="sbpnl"><thead><tr><th class="l">move</th><th>index</th><th>%</th><th>target date</th><th>expiry</th></tr></thead><tbody id="sbpnlbody">${tbl}</tbody></table></div>
       <p class="note">σ = ${num(A.hsig, 0)} pts to ${fmtExp(A.hexp)}, the chain's own volatility, around the forward ${num(A.ctr, 0)}. Levels rounded to the strike step.</p></div>
     <div class="card"><h2>Net Greeks &mdash; whole position</h2>
       <div class="kv"><span title="Rupees gained per 1-point rise in the index">delta</span><span class="${g.delta >= 0 ? 'pos' : 'neg'}">${g.delta >= 0 ? '+' : ''}${num(g.delta, 1)} ₹/pt</span></div>
       <div class="kv"><span title="How fast delta changes per point">gamma</span><span>${num(g.gamma, 3)}</span></div>
       <div class="kv"><span title="Rupees per calendar day from time passing, index unchanged">theta / day</span><span>${signRs(g.theta)}</span></div>
       <div class="kv"><span title="Rupees per 1 point of implied volatility">vega</span><span>${signRs(g.vega)}</span></div>
       <p class="note">Black–Scholes at each leg's IV, today, at spot, lots included (lot ${A.lot}). Short premium is normally positive theta and negative vega.</p></div>
   </div>`;
  sbChart($('#sbchart'), A);
  const sl = $('#sbday');
  // Only the parts that depend on the date are redrawn, so the slider keeps
  // focus while it is being dragged.
  sl.oninput = () => {
    DAYOFF = +sl.value;
    const B = analyse(LEGSX);
    sbChart($('#sbchart'), B);
    $('#sbdaylbl').textContent = B.tday === 0 ? 'today' : `+${B.tday} day${B.tday > 1 ? 's' : ''}`;
    $('#sbpnlbody').innerHTML = pnlRows(B);
  };
  $('#sbpinA').onclick = () => pin('A');
  $('#sbpinB').onclick = () => pin('B');
}

/* ---------------------------------------------------------------- compare */
function descLegs(legs){
  const multi = new Set(legs.map(x => x.exp)).size > 1;
  return legs.map(l => `${l.side > 0 ? '+' : '−'}${l.lots > 1 ? l.lots + '×' : ''}${num(l.strike)}${l.kind}${multi ? ' ' + fmtExp(l.exp) : ''}`).join(' ');
}
function pin(w){
  if(!LEGSX.length) return;
  PINS[w] = {legs: LEGSX.map(l => ({...l})), name: (STRATNAME || 'custom') + ' · ' + descLegs(LEGSX)};
  renderPins();
}
function renderPins(){
  const host = $('#sbcmp'); if(!host) return;
  if(!PINS.A && !PINS.B){ host.innerHTML = '<p class="note" style="margin:0">Pin two positions (the buttons above the chart) to compare them side by side.</p>'; return; }
  const an = {};
  for(const w of ['A', 'B']) if(PINS[w]){
    PINS[w].legs = PINS[w].legs.filter(l => CHAINS[l.exp]);
    an[w] = PINS[w].legs.length ? analyse(PINS[w].legs) : null;
  }
  host.innerHTML = `<div class="row" style="gap:10px;margin-bottom:10px"><label class="dimtxt" style="font-size:12px"><input type="checkbox" id="sbsame"${SB.same ? ' checked' : ''}> same scale</label>
    <button class="btn" id="sbclrpins">clear pins</button></div>
    <div class="grid g2"><div><div class="dimtxt sbpinname">A: ${esc2(PINS.A ? PINS.A.name : 'nothing pinned')}</div><div class="chartbox" id="sbA"></div></div>
    <div><div class="dimtxt sbpinname">B: ${esc2(PINS.B ? PINS.B.name : 'nothing pinned')}</div><div class="chartbox" id="sbB"></div></div></div>
    <div class="scroll" style="margin-top:12px"><table id="sbcmptbl"></table></div>`;
  let range = null;
  if(SB.same && an.A && an.B){
    const ys = an.A.curve.concat(an.B.curve).map(p => p.e);
    const lo = Math.min(...ys), hi = Math.max(...ys), pd = (hi - lo) * .12 || 1;
    range = [lo - pd, hi + pd];
  }
  for(const w of ['A', 'B']) if(an[w]) sbChart($('#sb' + w), Object.assign({}, an[w], {tday: an[w].H}), {W: 560, H: 300, yRange: range});
  const r = (lbl, f) => `<tr><td class="l dimtxt">${lbl}</td>${['A', 'B'].map(w => `<td>${an[w] ? f(an[w]) : '–'}</td>`).join('')}</tr>`;
  $('#sbcmptbl').innerHTML = '<thead><tr><th class="l">metric</th><th>A</th><th>B</th></tr></thead><tbody>'
    + r('net credit / debit', a => signRs(a.net))
    + r('max profit', a => a.profUnb ? 'unlimited' : signRs(a.maxP))
    + r('max loss', a => a.lossUnb ? '<span class="neg">unlimited</span>' : signRs(a.maxL))
    + r('breakevens', a => a.bes.map(b => num(b)).join(' / ') || '–')
    + r('POP', a => pct(a.pop, 1))
    + r('margin (est.)', a => rsS(a.margin))
    + r('theta / day', a => signRs(a.greeks.theta))
    + r('vega', a => signRs(a.greeks.vega))
    + '</tbody>';
  $('#sbsame').onchange = e => { SB.same = e.target.checked; renderPins(); };
  $('#sbclrpins').onclick = () => { PINS = {A: null, B: null}; renderPins(); };
}

/* ------------------------------------------------------ click-to-add chain */
function renderChainX(){
  const host = $('#sbchain'); if(!host) return;
  const c = CHAINS[CHAINEXP];
  if(!c){ host.innerHTML = ''; return; }
  const tabs = EXPS.filter(e => CHAINS[e]).map(e => `<button class="chip ${e === CHAINEXP ? 'on' : ''}" data-e="${e}">${fmtExp(e)}</button>`).join(' ');
  const st = (k, kind) => { const l = LEGSX.find(x => x.exp === CHAINEXP && x.strike === k && x.kind === kind); return !l ? '' : (l.side < 0 ? 'short' : 'long'); };
  const cell = (r, kind) => { const q = r[kind];
    return `<td class="cell ${st(r.strike, kind)}" data-k="${r.strike}" data-t="${kind}">${q && q.mid != null ? `<b>${num(q.mid, 2)}</b>` : '<span class="dimtxt">–</span>'}</td>`; };
  const iv = q => q && q.iv != null ? q.iv.toFixed(1) : '–';
  host.innerHTML = `<div class="sblhead"><h2>Chain &mdash; click a premium: once to sell, twice to buy, a third time to drop</h2><div>${tabs}</div></div>
   <div class="scroll" style="max-height:52vh"><table class="chain sbchaintbl"><thead><tr><th>call IV</th><th>call</th><th style="text-align:center">strike</th><th>put</th><th>put IV</th></tr></thead><tbody>`
   + c.rows.map(r => `<tr class="${r.atm ? 'atm' : ''}"><td class="dimtxt">${iv(r.CE)}</td>${cell(r, 'CE')}<td class="k">${num(r.strike)}</td>${cell(r, 'PE')}<td class="dimtxt">${iv(r.PE)}</td></tr>`).join('')
   + '</tbody></table></div>';
  host.querySelectorAll('.chip[data-e]').forEach(b => b.onclick = () => { CHAINEXP = b.dataset.e; renderChainX(); });
  host.querySelectorAll('td.cell').forEach(td => td.onclick = () => {
    const k = +td.dataset.k, kind = td.dataset.t, p = mid(CHAINEXP, k, kind);
    if(p == null) return;
    const i = LEGSX.findIndex(x => x.exp === CHAINEXP && x.strike === k && x.kind === kind);
    if(i < 0) LEGSX.push({id: NEXTID++, exp: CHAINEXP, strike: k, kind, side: -1, lots: 1, premium: p, custom: false});
    else if(LEGSX[i].side < 0) LEGSX[i].side = 1;
    else LEGSX.splice(i, 1);
    changed();
  });
}

/* ======================================== chapter 3: the scored catalogue */
SB.scoreTable = async function(){
  const host = $('#cattbl'); if(!host) return;
  const H2 = '<h2>Other strategies &mdash; scored, never recommended</h2>';
  let s = null;
  try{ s = await DATA.structures(EXPIRY); }
  catch(e){ host.innerHTML = `${H2}<div class="empty">${esc2(e.message)}</div>`; return; }
  const rows = (s && s.catalogue) || [];
  if(!rows.length){
    const m = (s && s.catalogue_meta) || {};
    host.innerHTML = `${H2}<div class="empty">${esc2(m.error || m.note || 'Not published yet — the server adds it with the next structures update.')}</div>`;
    return;
  }
  const sl = (+(($('#slpct') || {}).value) || 0) / 100;
  const legsTxt = r => (r.legs || []).map(l => `${l.qty > 0 ? '+' : '−'}${Math.abs(l.qty) > 1 ? Math.abs(l.qty) + '×' : ''}${num(l.strike)}${l.kind}${l.expiry && l.expiry !== s.expiry ? ' ' + fmtExp(l.expiry) : ''}`).join(' ');
  const body = (CATDEF.groups || []).map(g => {
    const rs_ = rows.filter(r => r.group === g.key);
    return `<tr class="sbgrp g-${g.key}"><td class="l" colspan="11">${esc2(g.label)}</td></tr>` + rs_.map(r => {
      if(!r.available) return `<tr class="dimtxt"><td class="l">${esc2(r.label)}</td><td class="l" colspan="10">unavailable: ${esc2(r.why)}</td></tr>`;
      const ps = r.p_stop ? pStopAt(r, sl) : null;
      return `<tr><td class="l"><b>${esc2(r.label)}</b><div class="dimtxt" style="font-size:11px">${esc2(r.family || r.type)}</div></td>
        <td class="l mono" style="font-size:11.5px">${legsTxt(r)}</td>
        <td>${signRs(r.net_premium_rs)}</td>
        <td>${r.profit_unbounded ? 'unlimited' : signRs(r.max_profit_rs)}</td>
        <td>${r.loss_unbounded ? '<span class="neg">unlimited</span>' : signRs(r.max_loss_rs)}</td>
        <td>${(r.breakevens || []).map(b => num(b)).join(' / ') || '–'}</td>
        <td>${pct(r.pop, 1)}</td>
        <td>${r.pop_cal != null ? pct(r.pop_cal, 1) : '–'}</td>
        <td>${signRs(r.net_ev_rs)}</td>
        <td>${r.roi != null ? (r.roi * 100).toFixed(2) + '%' : '–'}</td>
        <td title="${r.p_stop ? 'Modelled chance of touching the stop set on the sizing card (' + (sl * 100).toFixed(0) + '% of credit)' : 'Only for one-lot-per-leg credit strategies on the nearest expiry'}">${ps ? pct(ps.p, 0) : '–'}</td></tr>`;
    }).join('');
  }).join('');
  const m = s.catalogue_meta || {};
  host.innerHTML = `${H2}
    <div class="scroll"><table class="sbcat"><thead><tr><th class="l">strategy</th><th class="l">legs (1 lot each)</th><th>credit / debit</th><th>max profit</th><th>max loss</th><th>breakevens</th><th>POP</th><th title="Historical-fit POP: the same event under the distribution of past NIFTY moves. Tested year by year 2019-2026 it was no more accurate than model POP (chapter 3). Changes no decision.">POP hist.</th><th>net EV</th><th>EV / margin</th><th>P(stop)</th></tr></thead><tbody>${body}</tbody></table></div>
    <p class="note">The Strategy builder's catalogue, scored on the same chain and σ as the seven structures above: ATM ${num(m.atm)}, width ${num(m.width)} pts,
    expiries ${(m.expiries || []).map(fmtExp).join(' and ')}. EV is the site's normal (Bachelier) model after estimated costs; a later-expiry leg is valued
    at its own σ. <b>None of these is ever the model's pick.</b> The entry rule (credit/σ ≥ 0.9346) was fitted on selling premium in the seven structures
    above and says nothing about a debit spread or a back spread. EV / margin uses a rough margin estimate. ${esc2(m.note || '')}</p>`;
};

/* ============================================ chapter 4: last session, in review */
// postmortem.py writes it at 15:55 IST; the publisher sends /live/postmortem.
SB.pmCard = function(){
  const host = $('#pmcard'); if(!host) return;
  const p = (SRC && SRC.live && SRC.live.postmortem) || null;
  if(!p || p.error || !p.day){ host.innerHTML = '<h2>Last session, in review</h2><div class="empty">' +
    (p && p.error ? esc(p.error) : 'No post-mortem yet &mdash; it is written at 15:55 IST on trading days.') + '</div>'; return; }
  const d = p.day, pr = p.priced || {}, st = p.straddle || {}, sp = p.stop || {}, pt = p.paper_trade || null, q = p.quality || {};
  const sg = x => x == null ? '–' : (x > 0 ? '+' : '') + x.toFixed(2) + 'σ';
  const pc = x => x == null ? '–' : Math.round(x * 100) + '%';
  const late = p.full_session === false && pr.from ? ` <span class="neg" title="No two-sided quotes before this time">from ${pr.from} &mdash; no quotes before</span>` : '';
  const warn = q.note ? `<p class="note" style="border-left:3px solid #ffb020;padding-left:8px"><b>Data:</b> ${esc(q.note)}</p>` : '';
  const kv = (k, v, t) => `<div class="kv"><span title="${t || ''}">${k}</span><b>${v}</b></div>`;
  host.innerHTML = `<h2>Last session, in review &mdash; ${fmtExp(p.date)} ${String(p.date).slice(0, 4)}</h2>${warn}
    <div class="grid g2">
      <div>
        ${kv('NIFTY close', num(d.close, 2) + ` <span class="${d.change_pts >= 0 ? 'pos' : 'neg'}">(${d.change_pts >= 0 ? '+' : ''}${num(d.change_pts, 0)})</span>`)}
        ${kv('Overnight gap', sg(d.gap_sigma), "Today's open against yesterday's close, in one-day sigmas (VIX).")}
        ${kv('Day range', num(d.range_pts, 0) + ' pts = ' + (d.range_sigma == null ? '–' : d.range_sigma.toFixed(2) + 'σ'), 'High minus low of minute closes, in one-day sigmas.')}
        ${kv('High / low at', `${d.high_at} / ${d.low_at}`)}
        ${kv('Range set by 10:15 / 12:00', `${pc(d.range_set_by_1015)} / ${pc(d.range_set_by_1200)}`, "How much of the day's final range was already there.")}
      </div>
      <div>
        ${kv(`Priced move (${pr.from || '09:20'} straddle)`, pr.sigma_pts != null ? '±' + num(pr.sigma_pts, 0) + ' pts' + late : '–', 'One session-sigma, from the at-the-money straddle mid when it was first quoted (09:20 on a normal day).')}
        ${kv(`Actual move ${pr.from || '09:20'} → close`, pr.move_pts != null ? `${pr.move_pts >= 0 ? '+' : ''}${num(pr.move_pts, 0)} pts = <b>${sg(pr.move_sigma)}</b>` : '–')}
        ${kv('Straddle decayed by 15:15', st.captured_pct != null ? `${st.captured_pct.toFixed(0)}% (${num(st.start, 1)} → ${num(st.at_1515, 1)}), bottom ${st.bottom_at}` : '–')}
        ${kv('30% stop on that straddle', sp.stopped == null ? '–' : sp.stopped ? `<span class="neg">hit at ${sp.exit_at}</span>` : `<span class="pos">not hit</span> (worst ${pc(sp.mae_session_pct)} of credit)`)}
        ${kv('Paper trade', pt && pt.net_rs != null ? `<span class="${pt.net_rs >= 0 ? 'pos' : 'neg'}">${signRs(pt.net_rs)}</span> · ${esc(String(pt.exit_reason || pt.status || ''))}` : '–')}
      </div>
    </div>
    <div class="chartbox" id="pmchart" style="margin-top:12px"></div>
    <div class="scroll" style="margin-top:12px"><table class="sbcat"><thead><tr><th class="l">session</th><th>change</th><th>range</th>
      <th>move vs priced</th><th>range / priced</th><th>decayed</th><th>30% stop</th><th>paper trade</th><th class="l">data</th></tr></thead><tbody>
      ${(p.history || []).slice(-10).reverse().map(h => `<tr${h.full === false ? ' class="dimtxt" title="Part-session: straddle figures start at ' + h.from + '"' : ''}><td class="l">${fmtExp(h.date)}${h.full === false ? ' <small>(from ' + h.from + ')</small>' : ''}</td>
        <td class="${h.change_pts >= 0 ? 'pos' : 'neg'}">${h.change_pts >= 0 ? '+' : ''}${num(h.change_pts, 0)}</td>
        <td>${h.range_sigma == null ? '–' : h.range_sigma.toFixed(2) + 'σ'}</td><td>${sg(h.move_sigma)}</td>
        <td>${h.range_over_priced == null ? '–' : h.range_over_priced.toFixed(2) + '×'}</td>
        <td>${h.captured_pct == null ? '–' : h.captured_pct.toFixed(0) + '%'}</td>
        <td>${h.stopped == null ? '–' : h.stopped ? '<span class="neg">hit</span>' : 'no'}</td>
        <td>${h.paper_net_rs == null ? '–' : `<span class="${h.paper_net_rs >= 0 ? 'pos' : 'neg'}">${signRs(h.paper_net_rs)}</span>`}</td>
        <td class="l dimtxt">${esc(h.note || '')}</td></tr>`).join('')}</tbody></table></div>
    ${(() => { const sh = p.shadow; if(!sh || !sh.books) return '';
      const live = sh.books.live || {}; const dt = sh.daytrade || {};
      const row = (key, n, b) => `<tr><td class="l">${n}</td><td>${b.settled || 0} (${b.expiries || 0})</td><td>${b.open || 0}</td>
        <td class="${(b.pnl || 0) >= 0 ? 'pos' : 'neg'}">${signRs(b.pnl || 0)}</td><td>${b.worst == null ? '–' : signRs(b.worst)}</td>
        <td>${signRs(-(b.max_dd || 0))}</td><td>${key === 'live' ? '—' : signRs((b.pnl || 0) - (live.pnl || 0))}</td>
        <td class="dimtxt">${b.paused_until ? 'paused to ' + b.paused_until : ''}</td></tr>`;
      const names = {live: 'live rule (sized)', F4: 'F4 · straddle ≥ 1.10σ', F1: 'F1 · one trade per expiry'};
      return `<h2 style="margin-top:18px">Shadow candidates &mdash; paper, since ${fmtExp(sh.since)}</h2>
      <div class="scroll"><table class="sbcat"><thead><tr><th class="l">book</th><th>settled (expiries)</th><th>open</th><th>P&amp;L</th>
        <th>worst trade</th><th>max drawdown</th><th>vs live</th><th></th></tr></thead><tbody>
        ${Object.entries(sh.books).map(([k, b]) => row(k, names[k] || k, b)).join('')}
        ${dt.sessions != null ? `<tr><td class="l">day-trade, all sessions</td><td>${dt.sessions}</td><td></td><td class="${dt.net_all >= 0 ? 'pos' : 'neg'}">${signRs(dt.net_all)}</td><td colspan="4"></td></tr>
        <tr><td class="l">day-trade, no expiry-day sessions</td><td>${dt.sessions_no_expiry_day}</td><td></td><td class="${dt.net_no_expiry_day >= 0 ? 'pos' : 'neg'}">${signRs(dt.net_no_expiry_day)}</td><td colspan="3"></td><td>${signRs(dt.net_no_expiry_day - dt.net_all)}</td></tr>` : ''}
      </tbody></table></div>
      <p class="note">Candidates from the ten-year replay that did <b>not</b> pass an out-of-sample test, tracked forward instead of adopted.
      ${esc(sh.promotion_rule || '')} Until then they change no decision.</p>`; })()}
    <p class="note">Move vs priced: the session's move in the sigma the 09:20 straddle charged for. Below ±1σ most days is the volatility edge
    showing up one day at a time; a string of days above it is the warning. Written at 15:55 IST. Greyed rows are part-sessions (no quotes until the time shown) &mdash; do not read them as full days.</p>`;
  const c = st.curve || {}, ks = Object.keys(c).sort();
  if(ks.length > 1) lineChart($('#pmchart'), [{name: 'ATM straddle (mid)', values: ks.map(k => c[k]), color: '#5b8cff'}],
    {xLabels: ks, fmtY: v => num(v, 0), H: 180, fmtX: i => (ks[i] || '').endsWith(':00') || ks[i] === '09:20' ? ks[i] : ''});
};

/* ======================================= chapter 3: how accurate is POP? */
SB.calCard = function(){
  const host = $('#calcard'); if(!host) return;
  const c = (SRC && SRC.live && SRC.live.calibration) || null;
  const w = c && c.validation_10y, v = c && c.validation;
  if(!w){ host.innerHTML = '<h2>How accurate is POP?</h2><div class="empty">The ten-year test has not been published yet.</div>'; return; }
  const NAMES = {straddle: 'ATM straddle zone', strangle_1sd: '1σ strangle zone', iron_condor: 'Iron condor zone',
                 bull_put_spread: 'Bull put spread', bear_call_spread: 'Bear call spread', long_call: 'Long call'};
  const cell = g => `<span class="${Math.abs(g) < 0.03 ? 'pos' : Math.abs(g) < 0.07 ? '' : 'neg'}">${g > 0 ? '+' : ''}${(g * 100).toFixed(1)}</span>`;
  const rows = Object.entries(w.events).map(([k, e]) => `<tr><td class="l">${NAMES[k] || k}</td><td>${num(e.n)}</td><td><b>${pct(e.real, 1)}</b></td>
      <td>${pct(e.current, 1)}</td><td>${cell(e.current - e.real)}</td><td>${pct(e.hist, 1)}</td><td>${cell(e.hist - e.real)}</td></tr>`).join('');
  const yrs = Object.entries(w.years).map(([y, r]) => `${y}: ${r.model.toFixed(4)} vs ${r.hist.toFixed(4)}`).join(' · ');
  const zone = ['straddle', 'strangle_1sd', 'iron_condor'].map(k => w.events[k]).filter(Boolean);
  const under = zone.length ? zone.reduce((a, e) => a + (e.real - e.current), 0) / zone.length * 100 : null;
  host.innerHTML = `<h2>How accurate is POP? &mdash; tested year by year, 2019&ndash;2026</h2>
    <div class="scroll"><table class="sbcat"><thead><tr><th class="l">event (held to expiry)</th><th>events</th><th>actually happened</th>
      <th>model POP</th><th>gap, pts</th><th>historical-fit POP</th><th>gap, pts</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note"><b>Verdict: ${w.verdict}.</b> Each year from 2019 to 2026 was scored by models fitted only on expiries that settled before it
    (${w.n_expiries} expiries). Brier score, lower is better: model ${w.brier_current}, historical-fit ${w.brier_hist}; the difference is
    ${w.diff > 0 ? '+' : ''}${w.diff} with a 95% range of ${w.ci95[0]} to ${w.ci95[1]} &mdash; indistinguishable. Historical-fit was better in
    ${w.years_hist_better} of ${w.n_years} years (${yrs}). It narrows the zones' gap but learns a drift from the past that did not persist
    (average move vs the forward: ${Object.entries(w.z_mean_by_era).map(([k, x]) => (x > 0 ? '+' : '') + x + 'σ in ' + ({monthly_2016_18: '2016–18 (monthly)', thursday_2019_25: '2019–25 (Thursday weeklies)', tuesday_2025_26: '2025–26 (Tuesday weeklies)'}[k] || k)).join(', ')}),
    and loses the same amount on directional trades.</p>
    <p class="note"><b>What held for ten years:</b> short-premium zones finished inside more often than model POP said &mdash;
    ${under != null ? 'by ' + under.toFixed(1) + ' points on average' : ''} &mdash; because NIFTY moved less than the VIX-implied σ in every era
    (spread of realised moves ${Object.values(w.z_sd_by_era).join(', ')}σ). That is the volatility edge. Direction showed no stable edge.</p>
    ${v ? `<p class="note dimtxt">History: an earlier test on one held-out year (${v.test[0].slice(0, 7)} to ${v.test[1].slice(0, 7)}) had the historical-fit
    POP ${((1 - v.brier_calibrated / v.brier_current) * 100).toFixed(1)}% better. The ten-year test did not confirm it, so it is no longer called "calibrated".</p>` : ''}
    <p class="note"><b>Neither changes a decision</b>: the 0.9346 entry rule stays on the σ it was fitted on, and P(stop) is unchanged. NIFTY only.</p>`;
};

/* ============================================ journal: the audit, on the page */
// Audit, 23 Sep: nothing is broken. All seven structures show the same hit
// rate because the signals sharing an expiry share its outcome, and the zones
// nest: the index finished outside every zone or inside every zone. And the
// "duplicate" strangles are the same trade by construction on 1-DTE signals
// (one daily sigma IS one expiry sigma). Said on the page, next to the table.
const baseLoadJournal = typeof loadJournal === 'function' ? loadJournal : null;
if(baseLoadJournal){
  loadJournal = async function(){
    await baseLoadJournal();
    let j; try{ j = await DATA.journal(); }catch(e){ return; }
    const scored = (j.rows || []).filter(r => r.final_level != null);
    const t = $('#calibtbl'); if(!t || !scored.length) return;
    const exps = [...new Set(scored.map(r => r.expiry))].sort();
    const days = [...new Set(scored.map(r => r.signal_date))];
    const allOut = days.filter(d => scored.filter(r => r.signal_date === d).every(r => String(r.inside_zone) === 'false')).length;
    const allIn = days.filter(d => scored.filter(r => r.signal_date === d).every(r => String(r.inside_zone) === 'true')).length;
    const dup = [...new Set((j.rows || []).filter(r => r.structure === 'intraday_strangle' && (j.rows || []).some(x =>
      x.structure === 'expiry_strangle' && x.signal_date === r.signal_date && x.ce_strike === r.ce_strike && x.pe_strike === r.pe_strike))
      .map(r => r.signal_date))];
    let note = t.parentElement.querySelector('.jaudit');
    if(!note){ note = el('p', 'note jaudit'); t.parentElement.appendChild(note); }
    note.innerHTML = `<b>Read this before the table.</b> ${days.length} scored signal day${days.length === 1 ? '' : 's'}, ${exps.length < days.length ? 'but only' : 'on'}
      <b>${exps.length} expir${exps.length === 1 ? 'y' : 'ies'}</b> (${exps.map(fmtExp).join(', ')}). Signals that share an expiry share its outcome,
      so this is ${exps.length} independent result${exps.length === 1 ? '' : 's'}${exps.length < days.length ? ', not ' + days.length : ''}. Every structure shows the same hit rate because the zones nest:
      on ${allOut} day${allOut === 1 ? '' : 's'} the index finished outside <i>every</i> zone, on ${allIn} inside every one.
      ${dup.length ? `Intraday and expiry strangles are identical on ${dup.map(fmtExp).join(', ')}: with one day to expiry, one daily σ <i>is</i> one expiry σ, so both rules pick the same strikes. Not a bug.` : ''}
      For a real test of the probabilities, see "How accurate is POP?" in chapter 3: eight years of expiries the model never saw.`;
  };
}

/* ============================================ headlines: India filters */
// One tidy list: impact dot, headline, source (and how many other outlets ran
// it), time, and the one-line reason a flag fired. Filters: India / High
// impact / Global, plus a source picker. Every view is newest first -- the
// India list used to put HIGH items first, so it showed exactly the same ten
// headlines as the High-impact list and the buttons looked dead.
let NEWSF = 'india', NEWSSRC = '', NEWSALL = false;
const baseRenderWorld = typeof renderWorld === 'function' ? renderWorld : null;
if(baseRenderWorld){
  // Assigning to the classic script's global function binding, so every
  // existing caller gets the new card without being edited.
  renderWorld = async function(s){
    await baseRenderWorld(s);
    let w; try{ w = await DATA.world(); }catch(e){ w = {}; }
    const N = (w && w.news) || {};
    const items = (N.items || []).slice();
    const col = document.querySelector('#world .wgrid > div:last-child');
    if(!col) return;
    col.classList.add('wnewscol');
    const has = items.some(x => x.region);
    const age = x => x.age_min == null ? 1e9 : x.age_min;
    const inView = f => f === 'india' ? items.filter(x => x.region === 'india')
      : f === 'global' ? items.filter(x => x.region !== 'india')
      : items.filter(x => x.impact === 'high');
    const draw = () => {
      let list = has ? inView(NEWSF) : items;
      const sources = [...new Set(list.map(x => x.source))].sort();
      if(NEWSSRC && !sources.includes(NEWSSRC)) NEWSSRC = '';
      if(NEWSSRC) list = list.filter(x => x.source === NEWSSRC);
      list.sort((a, b) => age(a) - age(b));
      const shown = NEWSALL ? list : list.slice(0, 12);
      const groups = [['Last hour', x => age(x) < 60], ['Last 12 hours', x => age(x) >= 60 && age(x) < 12 * 60],
                      ['Earlier', x => age(x) >= 12 * 60]];
      const row = x => {
        const r = (x.impact_rules || [])[0];
        const why = r ? `${esc2(r.label)}: “${esc2(r.matched)}”` : (x.tags || []).length
          ? esc2(x.tags.map(t => t.label).join(', ')) : '';
        const lvl = x.impact || 'none';
        return `<div class="wn">
          <span class="wdot ${lvl}" title="${lvl === 'high' ? 'High potential to move the Indian market' : lvl === 'medium' ? 'Relevant, rarely market-moving on its own' : 'No India-impact rule fired'}"></span>
          <div class="wnb"><a href="${esc2(safeUrl(x.link))}" target="_blank" rel="noopener">${esc2(x.title)}</a>
            <div class="wnm">${esc2(x.source)}${(x.also || []).length ? ` <span title="also: ${esc2(x.also.join(', '))}">+${x.also.length}</span>` : ''}
              · ${x.age_min != null ? ago(x.age_min) + ' ago' : '–'}${why ? ` · <span class="wwhy">${why}</span>` : ''}</div></div></div>`;
      };
      const body = groups.map(([lbl, f]) => {
        const g = shown.filter(f);
        return g.length ? `<div class="wgrp">${lbl}</div>` + g.map(row).join('') : '';
      }).join('') || `<div class="dimtxt" style="padding:10px 0">No ${NEWSF === 'high' ? 'high-impact Indian' : NEWSF === 'india' ? 'Indian' : 'global'} headlines${NEWSSRC ? ' from ' + esc2(NEWSSRC) : ''} in the last 36 hours.</div>`;
      const down = (N.feeds || []).filter(f => !f.ok), empty = (N.feeds || []).filter(f => f.ok && f.empty);
      const cnt = {india: inView('india').length, high: inView('high').length, global: inView('global').length};
      const upd = N.at ? `updated ${esc2(N.at.slice(11, 16))} IST` : '';
      col.innerHTML = `<h3>Headlines <span class="dimtxt" style="font-weight:400;text-transform:none;letter-spacing:0">${upd}</span></h3>
        ${has ? `<div class="wfilt">${[['india', 'India'], ['high', 'High impact'], ['global', 'Global']]
          .map(([k, l]) => `<button class="chip ${k === NEWSF ? 'on' : ''}" data-f="${k}" aria-pressed="${k === NEWSF}">${l} <small>${cnt[k]}</small></button>`).join('')}
          <select class="wsrc" aria-label="source"><option value="">All sources</option>${sources.map(x => `<option value="${esc2(x)}"${x === NEWSSRC ? ' selected' : ''}>${esc2(x)} (${(has ? inView(NEWSF) : items).filter(y => y.source === x).length})</option>`).join('')}</select></div>` : ''}
        <div class="wlist">${body}</div>
        ${list.length > 12 ? `<button class="btn wmore">${NEWSALL ? 'show fewer' : 'show all ' + list.length}</button>` : ''}
        <p class="note"><span class="wdot high"></span> high <span class="wdot medium"></span> medium — keyword rules on the headline, each showing the words
        that fired it. High: RBI and SEBI decisions, F&amp;O rules, budget and tax, macro prints, big foreign flows, results and big events at the largest
        NIFTY companies, border conflict, election results. A flag is not a prediction that NIFTY will move.${down.length ? ` Not responding: ${down.map(f => esc2(f.source)).join(', ')}.` : ''}${empty.length ? ` Nothing in 36h from: ${empty.map(f => esc2(f.source)).join(', ')}.` : ''}</p>`;
      col.querySelectorAll('.wfilt .chip').forEach(b => b.onclick = () => { NEWSF = b.dataset.f; NEWSALL = false; draw(); });
      const sel = col.querySelector('.wsrc'); if(sel) sel.onchange = e => { NEWSSRC = e.target.value; draw(); };
      const more = col.querySelector('.wmore'); if(more) more.onclick = () => { NEWSALL = !NEWSALL; draw(); };
    };
    draw();
  };
}

/* ================================================ compact mobile header */
// On a phone the three pickers fold behind one line that says what is picked;
// the chapter menu becomes a single scrolling row (CSS). Desktop is unchanged.
(function(){
  const h = document.querySelector('header');
  if(!h || h.querySelector('.hset')) return;
  const b = document.createElement('button');
  b.className = 'hset'; b.type = 'button'; b.setAttribute('aria-expanded', 'false');
  const paint = () => {
    const u = $('#undsel'), e = $('#expsel'), s = $('#sesssel');
    const txt = x => x && x.selectedOptions[0] ? x.selectedOptions[0].textContent : '–';
    const ex = e && e.value ? fmtExp(e.value) : '–';
    const se = s && s.selectedOptions[0] ? (s.value ? fmtExp(s.value) + ' replay' : 'live') : 'live';
    b.innerHTML = `<b>${esc2(txt(u).split(' ·')[0])}</b> · expiry <b>${esc2(ex)}</b> · ${esc2(se)}<span class="chev">▾</span>`;
  };
  b.onclick = () => { const o = h.classList.toggle('open'); b.setAttribute('aria-expanded', String(o)); };
  h.insertBefore(b, $('#undsel'));
  ['#undsel', '#expsel', '#sesssel'].forEach(id => { const x = $(id); if(x) x.addEventListener('change', () => { paint(); h.classList.remove('open'); }); });
  // the pickers are filled asynchronously; repaint when their options arrive
  // (observing the selects only -- observing the whole header would see
  // paint()'s own write and loop forever)
  const mo = new MutationObserver(paint);
  ['#undsel', '#expsel', '#sesssel'].forEach(id => { const x = $(id); if(x) mo.observe(x, {childList: true}); });
  paint();
  const ban = document.querySelector('.banner');
  if(ban) ban.addEventListener('click', () => ban.classList.toggle('open'));
})();

/* ============================================== how old is the chain? */
// The header says spot every five seconds, which made the whole page look
// live when the numbers built from the option chain were minutes behind.
// This is the honest clock for them: the age of the newest bar the chain was
// built from, ticking in the header.
(function(){
  const h = document.querySelector('header');
  if(!h || $('#cage')) return;
  const box = document.createElement('div');
  box.className = 'cage'; box.id = 'cage';
  box.title = 'Age of the option-chain snapshot these numbers are built from. '
    + 'The collector writes a one-minute bar, the publisher sends it on within 15 seconds. '
    + 'Spot and India VIX above are live every 5 seconds.';
  const before = $('#undsel') || h.querySelector('.spacer');
  h.insertBefore(box, before);
  const newest = () => {
    const out = [];
    try{
      const node = (base().chain || {})[UND] || {};
      for(const k in node) if(node[k] && node[k].as_of) out.push(node[k].as_of);
    }catch(e){}
    if(typeof CHAIN !== 'undefined' && CHAIN && CHAIN.as_of) out.push(CHAIN.as_of);
    if(typeof S !== 'undefined' && S && S.as_of) out.push(S.as_of);
    return out.sort().pop();
  };
  const tick = () => {
    const a = newest();
    if(!a){ box.innerHTML = ''; return; }
    // `as_of` is the bar's START, so a one-minute bar is already a minute old
    // the instant it closes. Age is measured from its CLOSE, which is when
    // the numbers could first exist.
    const secs = Math.max(0, Math.round((Date.now() - new Date(a).getTime()) / 1000) - 60);
    // Under an hour, the age is the useful number; beyond that (overnight,
    // a weekend) the time of the snapshot is, so it is shown instead.
    const txt = secs < 90 ? secs + 's old'
      : secs < 3600 ? Math.floor(secs / 60) + 'm ' + (secs % 60) + 's old'
      : String(a).slice(11, 16) + ' IST';
    // Out of hours the chain is SUPPOSED to be old, so it is not painted as
    // a fault -- only a stale chain during the session is a problem.
    const ist = new Date(Date.now() + (new Date().getTimezoneOffset() + 330) * 60000);
    const hm = ist.getHours() * 60 + ist.getMinutes();
    const open = ist.getDay() >= 1 && ist.getDay() <= 5 && hm >= 555 && hm <= 935;
    const cls = !open ? 'closed' : secs < 120 ? 'fresh' : secs < 360 ? 'warm' : 'stale';
    // A replayed session is not "old data", it is a different question.
    const replay = (typeof SESSION !== 'undefined' && SESSION) ? true : false;
    box.className = 'cage ' + (replay ? 'replay' : cls);
    box.innerHTML = replay ? '<span class="lbl">chain</span><b>replay</b>'
      : `<span class="lbl">chain</span><b>${txt}</b>`;
  };
  tick();
  setInterval(tick, 2000);
})();

})();

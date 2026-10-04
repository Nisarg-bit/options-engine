import json, math, numpy as np, calibrate as C, calibrate_wf as W
d, vix = W.load(); rows = C.build_rows(d, vix, json.load(open(W.GAP_2Y)))
S, L, STEP = 23100.0, 65, 50.0
rnd = lambda x: round(x / STEP) * STEP
def pnl(x, s, width=None):
    kc, kp = rnd(x.F + s * x.sigma), rnd(x.F - s * x.sigma)
    try: cr = x.ce[kc] + x.pe[kp]
    except KeyError: return None
    pay = max(0, x.ST - kc) + max(0, kp - x.ST)
    if width:
        try: cr -= x.ce[kc + width] + x.pe[kp - width]
        except KeyError: return None
        pay = min(max(0, x.ST - kc), width) + min(max(0, kp - x.ST), width)
    return (cr - pay) * S / x.spot * L
def show(label, sub):
    for nm, s, w in (("naked straddle", 0, None), ("naked 1σ strangle", 1.0, None), ("1σ condor 2k", 1.0, 100), ("1σ condor 6k", 1.0, 300)):
        v = np.array([p for p in (pnl(x, s, w) for _, x in sub.iterrows()) if p is not None])
        print(f"  {label:34s} {nm:18s} n {len(v):4d}  gross mean {v.mean():8,.0f}/lot  t {v.mean()/(v.std()/math.sqrt(len(v))):5.2f}")
r = rows.sort_values("day")
show("first entry per expiry (dte max)", r.groupby("expiry", as_index=False).first())
show("last entry per expiry (dte min)", r.groupby("expiry", as_index=False).last())
show("every day (overlapping)", r)
for dte in (1, 2, 3, 4, 6, 7):
    show(f"entries with dte == {dte}", r[r.dte == dte])

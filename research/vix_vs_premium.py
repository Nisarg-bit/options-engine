import json, math, numpy as np, calibrate as C, calibrate_wf as W
d, vix = W.load(); r = C.build_rows(d, vix, json.load(open(W.GAP_2Y)))
STEP=50; out=[]
for _, x in r.iterrows():
    atm = round(x.F/STEP)*STEP
    if atm in x.ce and atm in x.pe:
        out.append({"era": "2016-18" if x.day.year < 2019 else ("2019-22" if x.day.year < 2023 else "2023-26"),
                    "cs": (x.ce[atm]+x.pe[atm])/x.sigma, "abs_move": abs(x.ST-x.F)/x.sigma})
import pandas as pd
o = pd.DataFrame(out)
fair = math.sqrt(2/math.pi)   # E|X| for a normal = 0.798 sigma
g = o.groupby("era").agg(days=("cs","size"), straddle_over_vix_sigma=("cs","median"), realised_abs_move_over_vix_sigma=("abs_move","mean"))
g["vix_says_straddle_should_be"] = round(fair,3)
g["edge_pts_per_sigma"] = g.straddle_over_vix_sigma - g.realised_abs_move_over_vix_sigma
g["gate_pass_rate"] = o.groupby("era").cs.apply(lambda s: (s >= 0.9346).mean())
print(g.round(3).to_string())
print("\nall:", round(o.cs.median(),3), round(o.abs_move.mean(),3), "gate pass", round((o.cs>=0.9346).mean(),3))
hi = o[o.cs>=0.9346]; print("on gate days: straddle", round(hi.cs.median(),3), "realised", round(hi.abs_move.mean(),3), "n", len(hi))

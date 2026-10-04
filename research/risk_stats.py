import json, numpy as np, pandas as pd, calibrate as C, calibrate_wf as W
d, vix = W.load(); g = json.load(open(W.GAP_2Y)); r = C.build_rows(d, vix, g)
STEP=50; SPOT=23100; LOT=65
out=[]
for _, x in r.iterrows():
    F, sig, ST = x.F, x.sigma, x.ST
    atm = round(F/STEP)*STEP; ku, kd = round((F+sig)/STEP)*STEP, round((F-sig)/STEP)*STEP
    ce, pe = x.ce, x.pe
    row={"day":x.day,"expiry":x.expiry,"dte":x.dte,"spot":x.spot}
    try:
        cr=ce[atm]+pe[atm]; row["straddle"]=(cr-abs(ST-atm))/x.spot
    except KeyError: pass
    try:
        cr=ce[ku]+pe[kd]; pay=max(0,ST-ku)+max(0,kd-ST); row["strangle"]=(cr-pay)/x.spot
        w=2*STEP; crc=cr-ce[ku+w]-pe[kd-w]; payc=min(pay,w); row["condor"]=(crc-payc)/x.spot
        row["condor_maxloss"]=(w-crc)/x.spot
    except KeyError: pass
    out.append(row)
o=pd.DataFrame(out)
# one row per EXPIRY for independence: the signal nearest 3 dte-ish? use every day but also per-expiry worst
per=o.groupby("expiry")
res={}
for s in ("straddle","strangle","condor"):
    v=o[s].dropna()
    q={p:float(np.percentile(v,p)) for p in (1,5,10,50)}
    res[s]={"n_days":len(v),"n_exp":int(o.dropna(subset=[s]).expiry.nunique()),"mean_pct":v.mean()*100,
            "p1_pct":q[1]*100,"p5_pct":q[5]*100,"p10_pct":q[10]*100,"worst_pct":v.min()*100,
            "worst_day":str(o.loc[v.idxmin(),"day"]),"loss_freq":(v<0).mean()}
cm=o.condor_maxloss.dropna(); print("condor max loss (defined) median %.2f%% = %.0f/lot" % (cm.median()*100, cm.median()*SPOT*LOT))
# worst 5 straddle days
print(o.nsmallest(6,"straddle")[["day","expiry","dte","straddle"]].assign(rs=lambda t:t.straddle*SPOT*LOT).to_string(index=False))
# 6% stress move for naked straddle per lot
print("6pct move naked straddle ~ loss", round(0.06*SPOT*LOT), "per lot minus credit")
# losing streak: consecutive expiries where straddle (last signal day per expiry) lost
e=o.dropna(subset=["straddle"]).sort_values("day").groupby("expiry").last().straddle
streak=mx=0
for v in e:
    streak = streak+1 if v<0 else 0; mx=max(mx,streak)
print("longest run of losing expiries (straddle, last signal per expiry):", mx, "of", len(e))

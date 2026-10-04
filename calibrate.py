"""POP calibration on two years of NSE bhavcopy.

Question: when the site says "POP 75%", how often does the zone actually hold
at expiry -- and is there a better distribution than the normal one?

For every trading day and the FRONT weekly expiry (1-7 calendar days out):
    spot    index close (UndrlygPric)
    F       parity forward, median of K + C - P over the 7 liquid strikes
            nearest spot
    sigma   the site's own: spot * VIX/100 * sqrt(max(dte,1)/365)
    S_T     the index close on the expiry day (NIFTY settles on the close)
    z       (S_T - F) / sigma   -- one realised draw, in site-sigma units

Candidate models for POP, all fitted on YEAR 1 (Jul 2024 - Jul 2025) and
scored on YEAR 2 (Aug 2025 - Aug 2026), which the fit never sees:

    M0  normal, centred on spot, site sigma        (what the site does today)
    M1  normal, centred on F, sigma x k            (k fitted by max likelihood)
    M2  normal, centred on F, session-clock sigma x k  (weekend fix, see below)
    M3  empirical z distribution (session clock), smoothed -- the fat-tail model

Session clock: a trading day carries one unit of variance (66.26% in the
session, 33.74% overnight -- gap_share.json); an overnight gap spanning a
weekend carries the MEASURED multiple of a one-night gap (2.57x for three
nights), not three calendar days. Units are converted back to years so the
average matches the calendar clock over the sample.

Events scored on each test day, with breakevens from that day's real closing
premiums: ATM straddle, 1-sigma strangle, iron condor (1 sigma, +2 steps),
bull put spread, bear call spread, long call. Score: Brier score (lower is
better) and mean predicted minus realised hit rate (calibration gap).

    python calibrate.py            writes data/calib/report.json and
                                   data/calibration.json (the winner, refitted
                                   on ALL data -- chosen out of sample, then
                                   refitted, the standard way)
"""
import json, math
from bisect import bisect_left
from datetime import date

import numpy as np
import pandas as pd

OPTS = "data/calib/nifty_opts.parquet"
VIX = "data/vix_history.csv"
GAP = "data/gap_share.json"
SPLIT = date(2025, 8, 1)
STEP = 50.0


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ------------------------------------------------------------------ data

def load():
    d = pd.read_parquet(OPTS)
    d["TradDt"] = pd.to_datetime(d.TradDt).dt.date
    d["XpryDt"] = pd.to_datetime(d.XpryDt).dt.date
    vix = pd.read_csv(VIX)
    vix["date"] = pd.to_datetime(vix.date).dt.date
    return d, dict(zip(vix.date, vix.vix))


def session_units(day, expiry, tdays, share, gaps):
    """Variance units from `day` close to `expiry` close on the session clock."""
    i, j = bisect_left(tdays, day), bisect_left(tdays, expiry)
    if j >= len(tdays) or tdays[j] != expiry or tdays[i] != day:
        return None
    u = 0.0
    for k in range(i + 1, j + 1):
        nights = (tdays[k] - tdays[k - 1]).days
        mult = 1.0 if nights == 1 else gaps.get(str(nights), gaps.get("3", 2.57))
        u += share + (1 - share) * mult
    return u


def build_rows(d, vix, gap):
    share = gap["session_share"]
    gaps = {k: v["gap_var_vs_one_night"] for k, v in gap["longer_gaps"].items()}
    tdays = sorted(d.TradDt.unique())
    close_on = d.groupby("TradDt").UndrlygPric.median().to_dict()
    opts = d[d.FinInstrmTp == "IDO"]
    rows = []
    for day, g in opts.groupby("TradDt"):
        if day not in vix:
            continue
        exps = sorted(e for e in g.XpryDt.unique() if 1 <= (e - day).days <= 7)
        if not exps:
            continue
        e = exps[0]
        if e not in close_on:
            continue
        c = g[g.XpryDt == e]
        spot = float(c.UndrlygPric.median())
        ce = c[c.OptnTp == "CE"].set_index("StrkPric")
        pe = c[c.OptnTp == "PE"].set_index("StrkPric")
        ks = sorted(set(ce.index) & set(pe.index), key=lambda k: abs(k - spot))
        liq = [k for k in ks[:15] if ce.loc[k, "TtlTradgVol"] > 0 and pe.loc[k, "TtlTradgVol"] > 0][:7]
        if len(liq) < 3:
            continue
        F = float(np.median([k + ce.loc[k, "ClsPric"] - pe.loc[k, "ClsPric"] for k in liq]))
        dte = (e - day).days
        sig = spot * vix[day] / 100 * math.sqrt(max(dte, 1) / 365)
        units = session_units(day, e, tdays, share, gaps)
        rows.append({"day": day, "expiry": e, "dte": dte, "spot": spot, "F": F,
                     "vix": vix[day], "sigma": sig, "units": units,
                     "ST": float(close_on[e]),
                     "ce": {float(k): float(v) for k, v in ce.ClsPric.items()},
                     "pe": {float(k): float(v) for k, v in pe.ClsPric.items()}})
    r = pd.DataFrame(rows)
    # session-clock sigma, scaled so it matches the calendar clock on average
    # over the sample: same overall level, different split across days.
    ok = r.units.notna()
    cal_days = r.loc[ok, "dte"].clip(lower=1)
    scale = (cal_days.sum() / r.loc[ok, "units"].sum())
    r["sigma_sc"] = r.spot * r.vix / 100 * np.sqrt((r.units * scale).fillna(cal_days if False else r.dte.clip(lower=1)) / 365)
    r.attrs["unit_scale"] = scale
    r["z_spot"] = (r.ST - r.spot) / r.sigma
    r["z_F"] = (r.ST - r.F) / r.sigma
    r["z_sc"] = (r.ST - r.F) / r.sigma_sc
    return r


# ---------------------------------------------------------------- models

class Normal:
    def __init__(self, centre, sig_col, k=1.0, name=""):
        self.centre, self.sig_col, self.k, self.name = centre, sig_col, k, name

    def fit(self, tr):
        z = (tr.ST - tr[self.centre]) / tr[self.sig_col]
        self.k = float(np.sqrt(np.mean(z ** 2)))     # MLE scale, mean fixed at 0
        return self

    def cdf(self, row, x):
        return ncdf((x - row[self.centre]) / (self.k * row[self.sig_col]))


class Empirical:
    """Smoothed empirical CDF of z (kernel with bandwidth h), centred on F."""
    def __init__(self, sig_col, name=""):
        self.sig_col, self.name = sig_col, name

    def fit(self, tr):
        z = ((tr.ST - tr.F) / tr[self.sig_col]).to_numpy()
        self.z = np.sort(z)
        self.h = 1.06 * z.std() * len(z) ** -0.2      # Silverman
        return self

    def cdf_z(self, q):
        return float(np.mean([ncdf((q - zi) / self.h) for zi in self.z]))

    def cdf(self, row, x):
        return self.cdf_z((x - row.F) / row[self.sig_col])


def inside(m, row, lo, hi):
    return m.cdf(row, hi) - m.cdf(row, lo)


# ---------------------------------------------------------------- events

def events(row):
    """[(name, kind, lo, hi)] -- kind 'in' = P(lo < S_T < hi), 'above' = P(S_T > lo),
    'below' = P(S_T < hi). Breakevens from the day's real closing premiums."""
    ce, pe, F, sig = row.ce, row.pe, row.F, row.sigma
    atm = round(F / STEP) * STEP
    k1u, k1d = round((F + sig) / STEP) * STEP, round((F - sig) / STEP) * STEP
    out = []
    try:
        s = ce[atm] + pe[atm]
        out.append(("straddle", "in", atm - s, atm + s))
    except KeyError:
        pass
    try:
        s = ce[k1u] + pe[k1d]
        out.append(("strangle_1sd", "in", k1d - s, k1u + s))
        w = 2 * STEP
        c = s - ce[k1u + w] - pe[k1d - w]
        out.append(("iron_condor", "in", k1d - c, k1u + c))
    except KeyError:
        pass
    try:
        c = pe[atm] - pe[atm - 2 * STEP]
        out.append(("bull_put_spread", "above", atm - c, None))
        c = ce[atm] - ce[atm + 2 * STEP]
        out.append(("bear_call_spread", "below", None, atm + c))
        out.append(("long_call", "above", atm + ce[atm], None))
    except KeyError:
        pass
    return out


def prob(m, row, kind, lo, hi):
    if kind == "in":
        return inside(m, row, lo, hi)
    if kind == "above":
        return 1 - m.cdf(row, lo)
    return m.cdf(row, hi)


def hit(row, kind, lo, hi):
    return (lo < row.ST < hi) if kind == "in" else (row.ST > lo if kind == "above" else row.ST < hi)


def score(models, te):
    res = {m.name: {} for m in models}
    for _, row in te.iterrows():
        for name, kind, lo, hi in events(row):
            h = float(hit(row, kind, lo, hi))
            for m in models:
                p = min(max(prob(m, row, kind, lo, hi), 1e-4), 1 - 1e-4)
                d = res[m.name].setdefault(name, {"n": 0, "brier": 0.0, "p": 0.0, "h": 0.0, "ll": 0.0})
                d["n"] += 1; d["brier"] += (p - h) ** 2; d["p"] += p; d["h"] += h
                d["ll"] += -(h * math.log(p) + (1 - h) * math.log(1 - p))
    out = {}
    for mn, ev in res.items():
        tot = {"n": 0, "brier": 0.0, "ll": 0.0}
        out[mn] = {}
        for en, d in ev.items():
            out[mn][en] = {"n": d["n"], "brier": round(d["brier"] / d["n"], 4),
                           "pred": round(d["p"] / d["n"], 4), "real": round(d["h"] / d["n"], 4),
                           "gap": round((d["p"] - d["h"]) / d["n"], 4)}
            tot["n"] += d["n"]; tot["brier"] += d["brier"]; tot["ll"] += d["ll"]
        out[mn]["_all"] = {"n": tot["n"], "brier": round(tot["brier"] / tot["n"], 5),
                           "logloss": round(tot["ll"] / tot["n"], 5)}
    return out


def main():
    d, vix = load()
    gap = json.load(open(GAP))
    r = build_rows(d, vix, gap)
    tr, te = r[r.day < SPLIT], r[r.day >= SPLIT]
    print(f"days {len(r)} (train {len(tr)}, test {len(te)}), expiries "
          f"{r.expiry.nunique()}, unit scale {r.attrs['unit_scale']:.3f}")
    for col in ("z_spot", "z_F", "z_sc"):
        z = r[col]
        print(f"  {col}: mean {z.mean():+.3f} sd {z.std():.3f} "
              f"|z|>2 {np.mean(abs(z) > 2):.3f} (normal 0.046) "
              f"|z|>3 {np.mean(abs(z) > 3):.3f} (normal 0.003)")

    M0 = Normal("spot", "sigma", 1.0, "M0 normal, spot, site sigma")
    M1 = Normal("F", "sigma", name="M1 normal, forward, sigma x k").fit(tr)
    M2 = Normal("F", "sigma_sc", name="M2 normal, forward, session clock x k").fit(tr)
    M3 = Empirical("sigma_sc", name="M3 empirical z, session clock").fit(tr)
    models = [M0, M1, M2, M3]
    rep = {"train": [str(tr.day.min()), str(tr.day.max()), len(tr)],
           "test": [str(te.day.min()), str(te.day.max()), len(te)],
           "k": {"M1": round(M1.k, 4), "M2": round(M2.k, 4)},
           "test_scores": score(models, te),
           "train_scores": score(models, tr)}
    for mn, sc in rep["test_scores"].items():
        a = sc["_all"]
        print(f"\n{mn}: TEST brier {a['brier']} logloss {a['logloss']} (n {a['n']})")
        for en, v in sc.items():
            if en != "_all":
                print(f"   {en:18} n {v['n']:3}  predicted {v['pred']:.3f}  realised {v['real']:.3f}  gap {v['gap']:+.3f}  brier {v['brier']}")

    best = min(rep["test_scores"], key=lambda m: rep["test_scores"][m]["_all"]["brier"])
    rep["winner"] = best
    print("\nWINNER (lowest test Brier):", best)

    # refit the winner's family on ALL data for production
    if best.startswith("M3"):
        mfin = Empirical("sigma_sc").fit(r)
        prod = {"model": "empirical_z", "clock": "session", "bandwidth": round(mfin.h, 4),
                "z": [round(float(x), 4) for x in mfin.z]}
    elif best.startswith("M2"):
        mfin = Normal("F", "sigma_sc").fit(r)
        prod = {"model": "normal_scaled", "clock": "session", "k": round(mfin.k, 4)}
    elif best.startswith("M1"):
        mfin = Normal("F", "sigma").fit(r)
        prod = {"model": "normal_scaled", "clock": "calendar", "k": round(mfin.k, 4)}
    else:
        prod = {"model": "normal_scaled", "clock": "calendar", "k": 1.0}
    prod.update({"centre": "forward", "fitted_on": [str(r.day.min()), str(r.day.max())],
                 "n_days": int(len(r)), "session_share": gap["session_share"],
                 "gap_mult": {k: v["gap_var_vs_one_night"] for k, v in gap["longer_gaps"].items()},
                 "unit_scale": round(float(r.attrs["unit_scale"]), 5),
                 "chosen_by": "lowest Brier on held-out year " + rep["test"][0] + ".." + rep["test"][1]})
    # The out-of-sample evidence travels with the model, so the page can show
    # exactly what was tested -- and the reverse split (fit year 2, test
    # year 1) as a robustness check.
    rev_tr, rev_te = r[r.day >= SPLIT], r[r.day < SPLIT]
    rev_models = [Normal("spot", "sigma", 1.0, "M0"),
                  Empirical("sigma_sc", name="M3").fit(rev_tr)]
    rev = score(rev_models, rev_te)
    ts = rep["test_scores"]
    m0, m3 = [k for k in ts if k.startswith("M0")][0], [k for k in ts if k.startswith("M3")][0]
    prod["validation"] = {
        "train": rep["train"], "test": rep["test"],
        "brier_current": ts[m0]["_all"]["brier"], "brier_calibrated": ts[m3]["_all"]["brier"],
        "events": {e: {"n": ts[m0][e]["n"], "real": ts[m0][e]["real"],
                       "current": ts[m0][e]["pred"], "calibrated": ts[m3][e]["pred"]}
                   for e in ts[m0] if e != "_all"},
        "reverse": {"brier_current": rev["M0"]["_all"]["brier"],
                    "brier_calibrated": rev["M3"]["_all"]["brier"]},
        "z_sd": round(float(r.z_sc.std()), 3), "z_mean": round(float(r.z_sc.mean()), 3),
    }
    json.dump(rep, open("data/calib/report.json", "w"), indent=1, default=str)
    json.dump(prod, open("data/calibration.json", "w"), indent=1)
    print("wrote data/calib/report.json and data/calibration.json")


if __name__ == "__main__":
    main()

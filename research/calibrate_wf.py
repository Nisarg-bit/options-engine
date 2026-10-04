"""Step 9: walk-forward POP calibration on 2016-2026.

calibrate.py fitted on one year and tested on the next. With ten years the
honest test is walk-forward: for each year Y from 2019 to 2026, fit on every
expiry that SETTLED before 1 Jan Y, score on Y. Eight out-of-sample years,
never a peek.

It reuses calibrate.py's own pieces (build_rows, events, the models) so the
two cannot drift apart, and changes only what ten years force:

  * TRADED LEGS ONLY. 73% of option rows in the old data never traded that
    day, and their "close" is stale. Event breakevens are built only from
    strikes that traded; an event whose legs did not trade is skipped.
  * VECTORISED empirical CDF (identical maths, ~1000x faster).
  * Two clock parameter sets are compared, not assumed: the current 2-year
    session share / weekend multiple, and the 10-year one from gap_share10.
  * M3-recent: the empirical model fitted on only the trailing 2 years.
    M3-all vs M3-recent is the real question -- does old data help or hurt?
  * Uncertainty by EXPIRY, not by day: days in one week share one outcome,
    so the bootstrap resamples whole expiries (~430, not ~1,640 days).

Writes data/calib/report_2016.json and data/calibration_candidate.json.
data/calibration.json (production) is never touched.

    python calibrate_wf.py
"""
import json, math, sys
from datetime import date

import numpy as np
import pandas as pd

import calibrate as C

try:
    from scipy.special import ndtr as _ndtr
except ImportError:                                      # identical, slower
    _ndtr = np.vectorize(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))

OPTS = "data/calib/nifty_opts_2016.parquet"
VIX = "data/calib/vix_history_2016.csv"
GAP_2Y = "data/gap_share.json"
GAP_10Y = "data/calib/gap_share_2016.json"
REPORT = "data/calib/report_2016.json"
CANDIDATE = "data/calibration_candidate.json"
TEST_YEARS = range(2019, 2027)
WEEKLY_START = date(2019, 2, 11)
TUESDAY_START = date(2025, 9, 1)


class EmpiricalV(C.Empirical):
    """calibrate.Empirical with a vectorised CDF. Same bandwidth, same kernel."""
    def cdf_z(self, q):
        return float(_ndtr((q - self.z) / self.h).mean())


def gap_10y():
    """10-year clock: old-format share bias-corrected, weekend multiples pooled."""
    g = json.load(open(GAP_10Y))
    share = g["old_close_bias_corrected_share"]
    # pool the weekend/holiday multiples over both eras, weighted by count
    pooled = {}
    for k in ("2", "3", "4"):
        parts = [g[s]["longer_gaps"].get(k) for s in ("old_close", "udiff_last")]
        parts = [p for p in parts if p]
        n = sum(p["n"] for p in parts)
        pooled[k] = {"n": n, "gap_var_vs_one_night":
                     round(sum(p["n"] * p["gap_var_vs_one_night"] for p in parts) / n, 3)}
    return {"session_share": share, "longer_gaps": pooled}


def load(traded_only=True):
    d = pd.read_parquet(OPTS)
    d["TradDt"] = pd.to_datetime(d.TradDt).dt.date
    d["XpryDt"] = pd.to_datetime(d.XpryDt).dt.date
    if traded_only:
        d = d[(d.FinInstrmTp != "IDO") | (d.TtlTradgVol > 0)]
    v = pd.read_csv(VIX)
    v["date"] = pd.to_datetime(v.date).dt.date
    return d, dict(zip(v.date, v.vix))


def era(row):
    if row.day < WEEKLY_START:
        return "monthly_2016_18"
    return "tuesday_2025_26" if row.expiry >= TUESDAY_START and row.expiry.weekday() == 1 \
        else "thursday_2019_25"


def per_expiry_brier(models, te):
    """{model: {expiry: [sum_sq_err, n_events]}} -- for the bootstrap."""
    out = {m.name: {} for m in models}
    for _, row in te.iterrows():
        for name, kind, lo, hi in C.events(row):
            h = float(C.hit(row, kind, lo, hi))
            for m in models:
                p = min(max(C.prob(m, row, kind, lo, hi), 1e-4), 1 - 1e-4)
                a = out[m.name].setdefault(row.expiry, [0.0, 0])
                a[0] += (p - h) ** 2
                a[1] += 1
    return out


def bootstrap(pe, a, b, n=2000, seed=7):
    """Mean Brier(a) - Brier(b), resampling whole expiries. Negative = a better."""
    ex = sorted(set(pe[a]) & set(pe[b]))
    sa = np.array([pe[a][e][0] for e in ex]); sb = np.array([pe[b][e][0] for e in ex])
    cnt = np.array([pe[a][e][1] for e in ex])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(ex), size=(n, len(ex)))
    diffs = (sa[idx].sum(1) - sb[idx].sum(1)) / cnt[idx].sum(1)
    point = (sa.sum() - sb.sum()) / cnt.sum()
    return {"diff": round(float(point), 5), "ci95": [round(float(np.percentile(diffs, 2.5)), 5),
            round(float(np.percentile(diffs, 97.5)), 5)], "n_expiries": len(ex),
            "p_a_better": round(float((diffs < 0).mean()), 3)}


def models_for(tr, recent):
    ms = [C.Normal("spot", "sigma", 1.0, "M0 site today"),
          C.Normal("F", "sigma", name="M1 normal fwd xk").fit(tr),
          C.Normal("F", "sigma_sc", name="M2 normal session xk").fit(tr),
          EmpiricalV("sigma_sc", name="M3 empirical all").fit(tr)]
    if len(recent) >= 100:
        ms.append(EmpiricalV("sigma_sc", name="M3 empirical 2y").fit(recent))
    return ms


def run(rows, label):
    """Walk-forward over TEST_YEARS on one set of rows (one clock)."""
    years, pe_all = {}, {}
    for y in TEST_YEARS:
        cut = date(y, 1, 1)
        tr = rows[rows.expiry < cut]                    # settled before the test year
        recent = tr[tr.expiry >= date(y - 2, 1, 1)]
        te = rows[(rows.day >= cut) & (rows.day < date(y + 1, 1, 1))]
        if te.empty:
            continue
        ms = models_for(tr, recent)
        sc = C.score(ms, te)
        years[y] = {"train_days": len(tr), "test_days": len(te),
                    "test_expiries": int(te.expiry.nunique()),
                    "brier": {m: sc[m]["_all"]["brier"] for m in sc},
                    "events": {m: {e: {k: v[k] for k in ("n", "pred", "real", "gap")}
                                   for e, v in sc[m].items() if e != "_all"} for m in sc}}
        for m, per in per_expiry_brier(ms, te).items():
            pe_all.setdefault(m, {}).update(per)
        best = min(years[y]["brier"], key=years[y]["brier"].get)
        print(f"  [{label}] {y}: train {len(tr):4d}d test {len(te):3d}d "
              f"{te.expiry.nunique():2d}exp  " +
              "  ".join(f"{m.split()[0]}{'-2y' if '2y' in m else ''} {b:.4f}"
                        for m, b in years[y]["brier"].items()) + f"   best {best.split()[0]}",
              flush=True)
    names = list(pe_all)
    pooled = {m: round(sum(v[0] for v in pe_all[m].values()) /
                       sum(v[1] for v in pe_all[m].values()), 5) for m in names}
    m3 = "M3 empirical all"
    boot = {f"{m3} vs {o}": bootstrap(pe_all, m3, o) for o in names if o != m3}
    wins = {m: sum(1 for y in years.values() if min(y["brier"], key=y["brier"].get) == m)
            for m in names}
    return {"years": years, "pooled_brier": pooled, "bootstrap": boot, "years_won": wins,
            "_pe": pe_all}


def main():
    d, vix = load(traded_only=True)
    g2 = json.load(open(GAP_2Y))
    g10 = gap_10y()
    out = {"data": [str(d.TradDt.min()), str(d.TradDt.max())], "clocks": {
        "2y": {"session_share": g2["session_share"],
               "weekend_x": g2["longer_gaps"]["3"]["gap_var_vs_one_night"]},
        "10y": {"session_share": g10["session_share"],
                "weekend_x": g10["longer_gaps"]["3"]["gap_var_vs_one_night"]}}}
    print("clocks:", out["clocks"], flush=True)
    results = {}
    for label, g in (("2y", g2), ("10y", g10)):
        rows = C.build_rows(d, vix, g)
        rows["era"] = rows.apply(era, axis=1)
        if label == "2y":
            out["rows"] = {"days": len(rows), "expiries": int(rows.expiry.nunique()),
                           "by_era": rows.groupby("era").expiry.nunique().to_dict(),
                           "z_sc_sd_by_era": rows.groupby("era").z_sc.std().round(3).to_dict(),
                           "z_sc_mean_by_era": rows.groupby("era").z_sc.mean().round(3).to_dict()}
            print("rows:", out["rows"], flush=True)
        results[label] = run(rows, label)
        results[label]["_rows"] = rows
    for label in results:
        r = results[label]
        out[label] = {k: v for k, v in r.items() if not k.startswith("_")}
    # same model, two clocks -- which clock scores better out of sample?
    pe2, pe10 = results["2y"]["_pe"], results["10y"]["_pe"]
    m3 = "M3 empirical all"
    ex = sorted(set(pe2[m3]) & set(pe10[m3]))
    out["clock_compare_M3"] = bootstrap({"a": {e: pe10[m3][e] for e in ex},
                                         "b": {e: pe2[m3][e] for e in ex}}, "a", "b")
    json.dump(out, open(REPORT, "w"), indent=1, default=str)
    print("\nwrote", REPORT)


if __name__ == "__main__":
    main()

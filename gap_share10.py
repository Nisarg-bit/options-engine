"""Step 9a: the session/overnight split and weekend multiples, on ten years.

Same method as gap_share.py (near-month NIFTY future, same contract on
consecutive days, one-night pairs set the share, longer gaps reported as a
multiple of a one-night gap) -- over 2016-2026 instead of 2024-2026.

THE ONE DIFFERENCE, AND HOW IT IS HANDLED
The old-format bhavcopy (before July 2024) has OPEN and CLOSE but no last
traded price. The F&O CLOSE is the volume-weighted price of the last half
hour, so part of the final 30 minutes' move leaks into the overnight term and
the session share comes out LOW. gap_share.py avoids that with LastPric.
Rather than guess the size of that bias, it is measured: on the UDiFF years
both ClsPric and LastPric exist, so the share is computed both ways there
and the difference is reported next to the old-era numbers.

    python gap_share10.py        writes data/calib/fut_2016.csv (cache) and
                                 data/calib/gap_share_2016.json. Does NOT touch
                                 data/gap_share.json.
"""
import glob, json, os, zipfile
from datetime import date
from math import log, sqrt

import pandas as pd

RAW_OLD, RAW_NEW = "data/bhavcopy_old", "data/bhavcopy"
CACHE, OUT = "data/calib/fut_2016.csv", "data/calib/gap_share_2016.json"


def old_day(path):
    z = zipfile.ZipFile(path)
    df = pd.read_csv(z.open(z.namelist()[0]))
    df.columns = [c.strip() for c in df.columns]
    f = df[(df.SYMBOL.str.strip() == "NIFTY") & (df.INSTRUMENT.str.strip() == "FUTIDX")]
    f = f[(f.OPEN > 0) & (f.CLOSE > 0)]
    if f.empty:
        return None
    f = f.assign(x=pd.to_datetime(f.EXPIRY_DT.str.title(), format="%d-%b-%Y")).sort_values("x")
    r = f.iloc[0]
    return {"date": os.path.basename(path)[:10], "expiry": str(r.x.date()),
            "open": float(r.OPEN), "close": float(r.CLOSE), "last": None, "fmt": "old"}


def new_day(path):
    df = pd.read_csv(path, usecols=["TckrSymb", "FinInstrmTp", "XpryDt", "OpnPric",
                                    "LastPric", "ClsPric"])
    f = df[(df.TckrSymb == "NIFTY") & (df.FinInstrmTp == "IDF")]
    f = f[(f.OpnPric > 0) & (f.LastPric > 0)].sort_values("XpryDt")
    if f.empty:
        return None
    r = f.iloc[0]
    return {"date": os.path.basename(path)[:10], "expiry": str(r.XpryDt),
            "open": float(r.OpnPric), "close": float(r.ClsPric),
            "last": float(r.LastPric), "fmt": "udiff"}


def build_cache(budget_s=150):
    import time
    t0 = time.time()
    have = pd.read_csv(CACHE) if os.path.exists(CACHE) else pd.DataFrame(columns=["date"])
    done = set(have.date.astype(str))
    rows = []
    todo = [(p, old_day) for p in sorted(glob.glob(RAW_OLD + "/*.zip"))] + \
           [(p, new_day) for p in sorted(glob.glob(RAW_NEW + "/*.csv"))]
    todo = [(p, fn) for p, fn in todo if os.path.basename(p)[:10] not in done]
    for p, fn in todo:
        if time.time() - t0 > budget_s:
            break
        r = fn(p)
        if r:
            rows.append(r)
    out = pd.concat([have, pd.DataFrame(rows)], ignore_index=True) if rows else have
    out.sort_values("date").to_csv(CACHE, index=False)
    left = len(todo) - len(rows)
    print(f"cache: {len(out)} days, {left} still to read" + (" -- run again" if left > 0 else ""))
    return left == 0


def measure(days, px):
    """Same arithmetic as gap_share.measure; `px` = which column is the day's end."""
    p = []
    for a, b in zip(days[:-1], days[1:]):
        if a["expiry"] != b["expiry"] or pd.isna(a[px]) or pd.isna(b[px]):
            continue
        nights = (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days
        p.append((nights, log(b["open"] / a[px]), log(b[px] / b["open"])))
    one = [x for x in p if x[0] == 1]
    if len(one) < 30:
        return None
    ms = lambda xs: sum(x * x for x in xs) / len(xs)
    v_on, v_in = ms([x[1] for x in one]), ms([x[2] for x in one])
    longer = {}
    for n in (2, 3, 4):
        g = [x[1] for x in p if x[0] == n]
        if g:
            longer[str(n)] = {"n": len(g), "gap_var_vs_one_night": round(ms(g) / v_on, 3)}
    return {"n_one_night": len(one), "session_share": round(v_in / (v_on + v_in), 4),
            "overnight_rms_pct": round(sqrt(v_on) * 100, 4),
            "session_rms_pct": round(sqrt(v_in) * 100, 4), "longer_gaps": longer}


def main():
    if not build_cache():
        return
    d = pd.read_csv(CACHE).sort_values("date")
    recs = d.to_dict("records")
    res = {}
    # the bias: UDiFF years measured with LastPric (truth) and ClsPric (old-format proxy)
    new = [r for r in recs if r["fmt"] == "udiff"]
    res["udiff_last"] = measure(new, "last")
    res["udiff_close"] = measure(new, "close")
    bias = res["udiff_last"]["session_share"] - res["udiff_close"]["session_share"]
    res["close_bias_on_share"] = round(bias, 4)
    old = [r for r in recs if r["fmt"] == "old"]
    res["old_close"] = measure(old, "close")
    res["old_close_bias_corrected_share"] = round(res["old_close"]["session_share"] + bias, 4)
    res["by_year_close"] = {}
    for y in range(2016, 2027):
        yr = [r for r in recs if r["date"].startswith(str(y))]
        m = measure(yr, "close")
        if m:
            res["by_year_close"][y] = {"share": m["session_share"], "n": m["n_one_night"],
                                       "weekend_x": m["longer_gaps"].get("3", {}).get("gap_var_vs_one_night")}
    res["all_close"] = measure(recs, "close")
    json.dump(res, open(OUT, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

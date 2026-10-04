"""How a calendar day's variance splits between the session and the gap.

stop_touch.py spreads the site's sigma over the path a position actually
lives through: the stop can only fire while the market is open, and whatever
moves between the close and the next open arrives as one jump at 09:15. That
needs one number -- the share of a normal one-night day's variance that
happens INSIDE the session. This measures it rather than assuming it.

SOURCE

Near-month NIFTY futures from the local F&O bhavcopy (FinInstrmTp == IDF).
Spot's open is not in the bhavcopy; the future's is, and it is continuous
within one contract. Every pair is taken on the SAME contract on consecutive
trading days, so a roll never shows up as a gap.

    intraday   ln(last_t / open_t)
    overnight  ln(open_t / last_{t-1})

LastPric, not ClsPric: the F&O closing price is a volume-weighted average of
the last half hour, which would leak part of the final 30 minutes' move into
the overnight term.

Only one-night pairs (a trading day followed by the next calendar day) set
the share. Weekend and holiday gaps are reported beside it, as the ratio of
their variance to a one-night gap, because the site's calendar clock charges
a Friday->Monday gap three days of variance and this is where that claim can
be checked. They are reported, not used.

Usage:  python gap_share.py            print and write data/gap_share.json
"""

import glob
import json
import os
from datetime import date
from math import log, sqrt

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
BHAV = os.path.join(ROOT, "data", "bhavcopy")
OUT = os.path.join(ROOT, "data", "gap_share.json")


def near_month(path, symbol="NIFTY"):
    d = date.fromisoformat(os.path.basename(path)[:10])
    df = pd.read_csv(path, usecols=["TckrSymb", "FinInstrmTp", "XpryDt",
                                    "OpnPric", "LastPric", "TtlTradgVol"])
    f = df[(df["TckrSymb"] == symbol) & (df["FinInstrmTp"] == "IDF")]
    f = f[(f["OpnPric"] > 0) & (f["LastPric"] > 0)]
    if f.empty:
        return None
    r = f.sort_values("XpryDt").iloc[0]
    return {"date": d, "expiry": str(r["XpryDt"]),
            "open": float(r["OpnPric"]), "last": float(r["LastPric"])}


def rows(symbol="NIFTY"):
    out = []
    for p in sorted(glob.glob(os.path.join(BHAV, "*.csv"))):
        r = near_month(p, symbol)
        if r:
            out.append(r)
    return out


def pairs(days):
    """(nights, overnight_logret, intraday_logret) on one contract."""
    out = []
    for prev, cur in zip(days, days[1:]):
        if prev["expiry"] != cur["expiry"]:
            continue                      # rolled -- not the same instrument
        nights = (cur["date"] - prev["date"]).days
        out.append((nights, log(cur["open"] / prev["last"]),
                    log(cur["last"] / cur["open"])))
    return out


def ms(xs):
    return sum(x * x for x in xs) / len(xs) if xs else None


def measure(symbol="NIFTY"):
    days = rows(symbol)
    p = pairs(days)
    one = [x for x in p if x[0] == 1]
    if len(one) < 30:
        raise SystemExit(f"only {len(one)} one-night pairs -- not an estimate")
    v_on, v_in = ms([x[1] for x in one]), ms([x[2] for x in one])
    share = v_in / (v_on + v_in)

    longer = {}
    for n in sorted({x[0] for x in p if x[0] > 1}):
        g = [x[1] for x in p if x[0] == n]
        longer[str(n)] = {"n": len(g), "gap_var_vs_one_night": round(ms(g) / v_on, 3)}

    return {
        "symbol": symbol, "source": "bhavcopy near-month futures, same contract",
        "first": str(days[0]["date"]), "last": str(days[-1]["date"]),
        "n_one_night": len(one),
        "session_share": round(share, 4),
        "overnight_rms_pct": round(sqrt(v_on) * 100, 4),
        "session_rms_pct": round(sqrt(v_in) * 100, 4),
        "longer_gaps": longer,
    }


if __name__ == "__main__":
    r = measure()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(r, f, indent=2)
    print(json.dumps(r, indent=2))
    print(f"\nwrote {OUT}")

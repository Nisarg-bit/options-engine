"""Prove series.py against real collected parquet, before wiring the API.

WHY A PROBE FIRST

api.py is a running service behind the dashboard. Adding an endpoint means
restarting it, and an adapter bug would show up as a broken chart rather
than a stack trace anyone reads. series.py is tested against synthetic
sessions; what is NOT yet tested is the step in between -- turning collected
parquet into the {ts: {strike: {CE, PE}}} shape it expects.

That adapter is where the mistakes live: column names, timezone handling,
NaNs, whether `volume` is cumulative or per-bar. So it gets exercised here,
on real data, with output a human can check, before it goes near the API.

The adapter function below is the one the endpoint will import. If it works
here it works there, because it IS there.

Usage:
    python series_probe.py
    python series_probe.py --session 2026-09-15 --structure strangle --width 2
    python series_probe.py --anchor atm_rolling
    python series_probe.py --anchor 23200
"""

import glob
import os
import sys
from datetime import date

import pandas as pd

import series as S

BARS = "data/bars_1m"
UNDERLYING = "NIFTY"


def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p)[5:]))
        except ValueError:
            continue
    return out


def by_minute(day, underlying, expiry):
    """Collected parquet -> {ts: {strike: {"CE": {...}, "PE": {...}}}}.

    Notes on the columns, because each one has bitten something already:

      bid/ask   the mid is taken from the two-sided quote and the row is
                dropped when it is not two-sided. `close` is a last traded
                price that can be minutes stale on an illiquid strike.
      volume    per-bar, not cumulative -- the aggregator resets each minute.
      pv        price*volume accumulated within the bar, which is why VWAP
                does not multiply a bar's mid by its volume afterwards.
      ts        stored UTC; rendered IST here because that is the only
                clock a reader of this output thinks in.
    """
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={underlying}", "*.parquet")))
    if not files:
        return {}, f"no parquet under {BARS}/date={day}/underlying={underlying}"

    cols = ["ts", "expiry", "strike", "opt_type",
            "bid", "ask", "close", "volume", "pv", "oi"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception as e:
            return {}, f"{os.path.basename(f)}: {type(e).__name__}: {e}"
    df = pd.concat(frames, ignore_index=True)

    df = df[df["expiry"].astype(str).str.slice(0, 10) == expiry.isoformat()]
    if df.empty:
        return {}, f"no rows for expiry {expiry}"

    df = df[(df["bid"] > 0) & (df["ask"] >= df["bid"])].copy()
    if df.empty:
        return {}, "no two-sided quotes in this session"
    df["mid"] = 0.5 * (df["bid"] + df["ask"])

    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["hm"] = ts.dt.strftime("%H:%M")

    out = {}
    for r in df.itertuples():
        out.setdefault(r.hm, {}).setdefault(float(r.strike), {})[
            str(r.opt_type)] = {
                "mid": float(r.mid),
                "volume": float(r.volume or 0),
                "pv": float(r.pv or 0),
                "oi": float(r.oi or 0)}
    return out, None


def expiries_on(day, underlying):
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={underlying}", "*.parquet")))
    out = set()
    for f in files:
        try:
            out.update(pd.read_parquet(f, columns=["expiry"])["expiry"]
                       .astype(str).str.slice(0, 10).unique().tolist())
        except Exception:
            continue
    return sorted(date.fromisoformat(e) for e in out)


def main():
    args = {"--session": None, "--expiry": None, "--structure": "straddle",
            "--anchor": "atm_open", "--width": "2", "--every": "15"}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]

    days = sessions()
    if not days:
        print(f"no sessions under {BARS}")
        return 1
    day = date.fromisoformat(args["--session"]) if args["--session"] else days[-1]

    exps = [e for e in expiries_on(day, UNDERLYING) if e >= day]
    if not exps:
        print(f"no expiries on {day}")
        return 1
    expiry = date.fromisoformat(args["--expiry"]) if args["--expiry"] else exps[0]

    print(f"SERIES PROBE   {day}   expiry {expiry}   "
          f"({(expiry - day).days} DTE)")
    print(f"structure {args['--structure']}   anchor {args['--anchor']}   "
          f"width {args['--width']}\n")

    bm, err = by_minute(day, UNDERLYING, expiry)
    if err:
        print(f"adapter failed: {err}")
        return 1
    print(f"{len(bm)} minutes, "
          f"{len({k for m in bm.values() for k in m})} strikes\n")

    anchor = args["--anchor"]
    if anchor not in ("atm_open", "atm_rolling"):
        anchor = float(anchor)

    out = S.build(bm, structure=args["--structure"], anchor=anchor,
                  width=int(args["--width"]))
    if not out["rows"]:
        for n in out["notes"]:
            print(f"  {n}")
        return 1

    print(f"strike step {out['strike_step']:.0f}   "
          f"{out['points']} points   {out['strike_rolls']} strike rolls\n")

    step = max(1, int(args["--every"]))
    hdr = (f"{'time':<7}{'price':>9}{'vwap':>9}{'ma':>9}{'ema':>9}"
           f"{'rsi':>7}   {'legs':<24}{'oi':>12}")
    print(hdr)
    print("-" * len(hdr))
    rows = out["rows"]
    for r in rows[::step] + ([rows[-1]] if len(rows) % step else []):
        legs = " ".join(f"{g['strike']:.0f}{g['opt_type']}"
                        for g in r["legs"])
        def f(v, w=9, p=2):
            return f"{v:>{w}.{p}f}" if v is not None else f"{'-':>{w}}"
        print(f"{r['ts']:<7}{f(r['price'])}{f(r['vwap'])}{f(r['ma'])}"
              f"{f(r['ema'])}{f(r['rsi'], 7, 1)}   {legs:<24}"
              f"{r['oi']:>12,.0f}")

    print()
    for n in out["notes"]:
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

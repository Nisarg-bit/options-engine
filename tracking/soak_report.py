"""What the ten-session soak actually produced.

Phase 1 asked one question: does this thing collect a clean session, every
session, without anyone watching? This answers it from the files on disk
rather than from anyone's recollection -- per session, per underlying, with
the gaps named rather than averaged away.

A 'gap' here is a minute between 09:15 and 15:29 with no bar for an
underlying at all. That is the failure that matters: a websocket drop loses
minutes silently, and a summary that reports only totals would hide it. One
missing minute in 3,750 is a rounding error in a count and a hole in a
backtest.

Reads one session at a time and only the two columns it needs, because this
runs on a 2 GB box that is also running the collector.

Usage:  python soak_report.py
"""

import glob
import os
from datetime import date, datetime, time, timedelta

import pandas as pd

BARS = "data/bars_1m"
JOURNAL = "data/journal/signals.csv"
OPEN_T, CLOSE_T = time(9, 15), time(15, 29)


def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return out


def expected_minutes(d):
    t, out = datetime.combine(d, OPEN_T), []
    end = datetime.combine(d, CLOSE_T)
    while t <= end:
        out.append(t.time().replace(second=0, microsecond=0))
        t += timedelta(minutes=1)
    return out


def scan(d, und):
    files = glob.glob(os.path.join(BARS, f"date={d}", f"underlying={und}",
                                   "*.parquet"))
    if not files:
        return None
    rows, tokens, mins, size = 0, set(), set(), 0
    for f in files:
        size += os.path.getsize(f)
        df = pd.read_parquet(f, columns=["ts", "instrument_token"])
        rows += len(df)
        tokens.update(df["instrument_token"].unique().tolist())
        ts = pd.to_datetime(df["ts"])
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert("Asia/Kolkata")
        mins.update(ts.dt.floor("min").dt.time.unique().tolist())
    exp = expected_minutes(d)
    missing = [m for m in exp if m not in mins]
    return {"rows": rows, "contracts": len(tokens), "minutes": len(mins),
            "expected": len(exp), "missing": missing, "parts": len(files),
            "mb": size / 1e6}


def main():
    days = sessions()
    if not days:
        print("no sessions on disk")
        return

    unds = sorted({os.path.basename(p).split("=")[1]
                   for d in days
                   for p in glob.glob(os.path.join(BARS, f"date={d}",
                                                   "underlying=*"))})
    print(f"SOAK REPORT   {days[0]} -> {days[-1]}   "
          f"{len(days)} sessions   {', '.join(unds)}\n")

    hdr = (f"{'session':<12}{'und':<11}{'contracts':>10}{'minutes':>9}"
           f"{'gaps':>6}{'bars':>10}{'parts':>7}{'MB':>8}")
    print(hdr)
    print("-" * len(hdr))

    tot = {"rows": 0, "mb": 0.0, "missing": 0}
    per_day = {}
    for d in days:
        for u in unds:
            s = scan(d, u)
            if s is None:
                print(f"{str(d):<12}{u:<11}{'NO DATA':>10}")
                continue
            tot["rows"] += s["rows"]
            tot["mb"] += s["mb"]
            tot["missing"] += len(s["missing"])
            per_day.setdefault(d, []).append((u, s))
            flag = "" if not s["missing"] else f"  <- {len(s['missing'])} missing"
            print(f"{str(d):<12}{u:<11}{s['contracts']:>10,}{s['minutes']:>9}"
                  f"{len(s['missing']):>6}{s['rows']:>10,}{s['parts']:>7}"
                  f"{s['mb']:>8.1f}{flag}")

    print("-" * len(hdr))
    print(f"{'TOTAL':<23}{'':>10}{'':>9}{tot['missing']:>6}"
          f"{tot['rows']:>10,}{'':>7}{tot['mb']:>8.1f}")

    # Name every gap. A count is not enough to act on.
    holes = [(d, u, s["missing"]) for d in per_day for u, s in per_day[d]
             if s["missing"]]
    print()
    if not holes:
        print("NO GAPS. Every minute of every session, for every underlying,")
        print("has at least one bar. That is what the soak was testing.")
    else:
        print("GAPS -- minutes with no bar at all:")
        for d, u, miss in holes:
            shown = ", ".join(m.strftime("%H:%M") for m in miss[:12])
            more = f"  (+{len(miss)-12} more)" if len(miss) > 12 else ""
            print(f"  {d}  {u:<11} {len(miss):>3}:  {shown}{more}")

    # ------------------------------------------------------------- signals
    print()
    if not os.path.exists(JOURNAL):
        print("no journal yet")
        return
    j = pd.read_csv(JOURNAL)
    rec = j[j["recommended"].astype(str).str.lower() == "true"].copy()
    scored_days = j.loc[j["pnl_rs"].notna(), "signal_date"].nunique()
    print(f"SIGNALS   {j['signal_date'].nunique()} days logged, "
          f"{scored_days} scored\n")
    h2 = (f"{'signal':<12}{'expiry':<12}{'dte':>4}{'spot':>10}{'c/sigma':>9}"
          f"{'gate':>6}  {'structure':<18}{'net EV':>9}{'realised':>10}")
    print(h2)
    print("-" * len(h2))
    for _, r in rec.sort_values("signal_date").iterrows():
        gate = "open" if str(r["gate_passed"]).lower() == "true" else "shut"
        real = ("" if pd.isna(r["pnl_rs"]) else f"{r['pnl_rs']:>10,.0f}")
        print(f"{r['signal_date']:<12}{r['expiry']:<12}{int(r['dte']):>4}"
              f"{r['spot']:>10,.0f}{r['c_over_sigma']:>9.3f}{gate:>6}"
              f"  {r['structure']:<18}{r['net_ev_rs']:>9,.0f}{real}")

    done = j[j["pnl_rs"].notna()]
    if len(done):
        print(f"\n{done['signal_date'].nunique()} scored: "
              f"realised {done[done['recommended'].astype(str).str.lower()=='true']['pnl_rs'].sum():,.0f} "
              f"on the recommended trades, "
              f"{done.groupby('structure')['pnl_rs'].sum().max():,.0f} "
              f"on the best structure in hindsight.")


if __name__ == "__main__":
    main()

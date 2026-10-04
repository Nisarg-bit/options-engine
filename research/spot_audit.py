"""Is the spot the signal job recorded the real index level?

The signal runs at 09:00 IST, before the open. It builds the chain from
YESTERDAY's collected bars, then asks Kite for a live quote:

    q = kite.ltp(["NSE:NIFTY 50", "NSE:INDIA VIX"])

and uses that one number to pick the ATM strike, size sigma, and compute POP,
EV and the gate. Every downstream figure hangs off it. A wrong spot does not
announce itself -- it produces a confident, rich-looking recommendation built
on the wrong strike.

Three references, none of which needs Kite or the internet:

  1. NIFTY 50's own close on the session the chain came from. At 09:00 the
     index is not trading, so a healthy ltp() returns exactly this.

     NOTE the underlying=INDEX partition holds THREE instruments -- NIFTY 50
     (256265), NIFTY BANK (260105) and INDIA VIX (264969). The first version
     of this script read the partition without filtering and silently
     compared NIFTY against BANKNIFTY. Hence the explicit filter below.

  2. The forward implied by put-call parity across that same chain, which the
     signal job already computed and journalled as `forward`. Parity needs no
     external price: F = K + C - P.

  3. WHERE ELSE the recorded number appears. For every spot that disagrees
     with the chain, this searches every collected minute of every session
     for the nearest matching index level. A stale quote will match some
     specific minute of some specific day, and that tells us what is being
     handed back. Matching nothing anywhere means it is not an index level
     at all, which is a different and worse bug.

Usage:  python spot_audit.py
"""

import glob
import os
from datetime import date

import pandas as pd

BARS = "data/bars_1m"
JOURNAL = "data/journal/signals.csv"
INDEX = "INDEX"

NIFTY_TOKEN = 256265          # Kite's instrument token for NIFTY 50
NIFTY_SYMBOL = "NIFTY50"      # tradingsymbol with spaces/underscores stripped

TOL = 25.0                    # points; beyond this a quote disagrees


# ------------------------------------------------------------------ loading

def _read(path):
    want = ["ts", "instrument_token", "close"]
    try:
        df = pd.read_parquet(path, columns=want + ["tradingsymbol"])
    except (OSError, ValueError, KeyError):
        try:
            df = pd.read_parquet(path, columns=want)
            df["tradingsymbol"] = ""
        except (OSError, ValueError, KeyError):
            return None
    ts = pd.to_datetime(df["ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["ts"] = ts
    return df


def _norm(s):
    return str(s).upper().replace(" ", "").replace("_", "").replace("-", "")


def index_partition(d):
    """Every index instrument's bars for date d, or None."""
    files = sorted(glob.glob(os.path.join(BARS, f"date={d}",
                                          f"underlying={INDEX}", "*.parquet")))
    if not files:
        return None
    parts = [x for x in (_read(f) for f in files) if x is not None]
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).sort_values("ts")


def nifty_only(df):
    """The NIFTY 50 rows. By tradingsymbol, else by token."""
    if df is None or df.empty:
        return None
    by_sym = df[df["tradingsymbol"].map(_norm) == NIFTY_SYMBOL]
    if not by_sym.empty:
        return by_sym
    by_tok = df[df["instrument_token"] == NIFTY_TOKEN]
    return by_tok if not by_tok.empty else None


def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return out


def f(x, w=10, p=2):
    return " " * w if x is None else f"{x:>{w},.{p}f}"


# --------------------------------------------------------------------- main

def main():
    if not os.path.exists(JOURNAL):
        print("no journal yet")
        return

    days = sessions()
    if not days:
        print("no sessions on disk")
        return

    # ------------------------------------------------- what is in the index
    latest = index_partition(days[-1])
    print(f"INDEX PARTITION on {days[-1]} -- what the collector stores there\n")
    if latest is None:
        print("  (nothing)")
    else:
        g = (latest.groupby(["instrument_token", "tradingsymbol"])["close"]
             .agg(["count", "last"]).reset_index())
        print(f"  {'token':>10}  {'symbol':<16}{'bars':>7}{'last close':>13}")
        for _, r in g.iterrows():
            mark = "  <- used as spot reference" if (
                _norm(r["tradingsymbol"]) == NIFTY_SYMBOL
                or r["instrument_token"] == NIFTY_TOKEN) else ""
            print(f"  {int(r['instrument_token']):>10}  "
                  f"{str(r['tradingsymbol']):<16}{int(r['count']):>7}"
                  f"{r['last']:>13,.2f}{mark}")
    print()

    # --------------------------------------------- one long NIFTY 50 series
    hist = []
    closes_by_day = {}
    for d in days:
        n = nifty_only(index_partition(d))
        if n is None or n.empty:
            continue
        closes_by_day[d] = float(n["close"].iloc[-1])
        for t, c in zip(n["ts"], n["close"]):
            hist.append((d, t.time(), float(c)))
    if not hist:
        print("could not identify NIFTY 50 in any session -- check the token")
        return

    # ------------------------------------------------------- signal journal
    j = pd.read_csv(JOURNAL)
    keep = [c for c in ["signal_date", "session", "expiry", "dte", "spot",
                        "forward", "vix", "c_over_sigma"] if c in j.columns]
    g = (j[keep].drop_duplicates(subset=["signal_date"])
         .sort_values("signal_date"))

    print("SPOT AUDIT -- the recorded quote against NIFTY 50 and the chain\n")
    hdr = (f"{'signal':<12}{'chain from':<12}{'spot':>11}{'NIFTY close':>13}"
           f"{'spot-close':>12}{'forward':>11}{'spot-fwd':>10}   {'note'}")
    print(hdr)
    print("-" * (len(hdr) + 12))

    suspects = []
    for _, r in g.iterrows():
        sd = date.fromisoformat(str(r["signal_date"])[:10])
        cd = (date.fromisoformat(str(r["session"])[:10])
              if "session" in r and pd.notna(r["session"]) else None)
        spot = float(r["spot"]) if pd.notna(r["spot"]) else None
        fwd = (float(r["forward"])
               if "forward" in r and pd.notna(r["forward"]) else None)
        pc = closes_by_day.get(cd)

        d_close = None if (spot is None or pc is None) else spot - pc
        d_fwd = None if (spot is None or fwd is None) else spot - fwd

        notes = []
        if sd not in closes_by_day:
            notes.append("NO SESSION on this date -- market was shut")
        # Flag on spot vs the index's own close. That is the crisp test: at
        # 09:00 the index is not trading, so the two must be equal. spot-fwd
        # is corroboration only -- the forward legitimately sits tens of
        # points above spot from carry, so it cannot carry the threshold.
        bad = (d_close is not None and abs(d_close) > TOL)
        if bad or (d_close is None and d_fwd is not None and abs(d_fwd) > 60):
            notes.append("QUOTE DISAGREES WITH THE INDEX")
            suspects.append((sd, cd, spot, pc, fwd))

        print(f"{str(sd):<12}{str(cd):<12}{f(spot,11)}{f(pc,13)}"
              f"{f(d_close,12,1)}{f(fwd,11)}{f(d_fwd,10,1)}   "
              f"{'; '.join(notes)}")

    # --------------------------------------------- where does the number live
    print()
    if not suspects:
        print("Every recorded spot agrees with the chain. Inputs are sound.")
        return

    print("WHERE ELSE DOES THAT NUMBER APPEAR?")
    print("For each disagreeing quote, the closest NIFTY 50 level in every")
    print("minute of every collected session:\n")
    for sd, cd, spot, pc, fwd in suspects:
        best = min(hist, key=lambda h: abs(h[2] - spot))
        bd, bt, bc = best
        where = ("the chain's own session" if bd == cd else
                 "the signal date itself" if bd == sd else
                 f"a different session ({bd})")
        print(f"  signal {sd}   recorded {spot:,.2f}")
        print(f"    nearest anywhere: {bc:,.2f}  on {bd} at "
              f"{bt.strftime('%H:%M')}   off by {abs(bc-spot):,.2f}")
        print(f"    that is {where}")
        if pc is not None:
            print(f"    for reference, {cd} closed at {pc:,.2f} "
                  f"(quote was {spot-pc:+,.1f})")
        print()

    print("READING IT")
    print("  If the nearest match is within a point or two and lands on a")
    print("  specific minute, the quote is a stale reading from that moment")
    print("  and we chase why ltp() returned it.")
    print("  If the nearest match is tens of points away, the number is not")
    print("  an index level the collector ever saw -- which points at the")
    print("  quote call itself rather than at staleness.")
    print()
    print("  Either way nothing downstream of a bad spot can be trusted:")
    print("  ATM strike, sigma, POP, net EV and the gate all take it as")
    print("  input. Settled P&L is unaffected -- score() gets settlement")
    print("  from parity on expiry-day bars, not from this quote.")


if __name__ == "__main__":
    main()

"""Are the prices near the close real, or are they wide-spread mids?

WHY THIS EXISTS

The last two live rows of 15 September read, at 15:10 with five minutes to
the flat-by time:

    straddle 23150, spot 23174.45, credit 102.80,
    POP terminal 1.0000, no-touch 1.0000, net EV Rs 4,210

An ATM straddle twenty minutes from expiry is worth its intrinsic value plus
whatever the remaining twenty minutes of movement are worth. Intrinsic here
is |23174.45 - 23150| = 24.45. So the model believes it is being paid about
78 points of TIME VALUE for twenty minutes on an index that, by its own
sigma, moves 18.9 points in five. Those two statements cannot both be true.

Either the quote is real and the option is extraordinarily rich, or the
number being used is not a price anyone would trade at. The difference
matters enormously: the second case is a system that generates fantasy
signals at the end of every single session, and it would look like a superb
strategy in any backtest that trusted the same mids.

WHAT IT CHECKS

For each minute in the tail of a session, for the strikes nearest the money:

  spread      ask - bid, and that as a fraction of the mid. A mid drawn from
              a spread wider than the mid itself is not a tradeable price.
  time value  credit - intrinsic, against 0.7979 * sigma, the fair Bachelier
              straddle for the minutes actually remaining. Time value far
              above that is either genuine richness or a bad quote.
  mid vs last the mid the code uses against the last traded price. When
              these diverge sharply the depth is stale, not the trade.

It reads only what the collector already wrote. Nothing is inferred that
the parquet does not contain, and columns the collector did not record are
reported as missing rather than guessed at.

Usage:
    python close_audit.py                    today, nearest expiry, last 30 min
    python close_audit.py 2026-09-15         that session
    python close_audit.py 2026-09-15 45      that session, last 45 minutes
"""

import glob
import os
import sys
from datetime import date, datetime, time as dtime

import pandas as pd

import intraday as I
import vol_time as V

# Taken from intraday.py rather than restated here. Writing "data/bars"
# by hand once already cost a round trip -- the collector's path is
# data/bars_1m, and a constant copied by hand is a constant that drifts.
BARS = I.BARS
UNDERLYING = I.UNDERLYING
CLOSE = dtime(15, 30)
STRIKE_STEP = 50.0
NEAR = 2            # strikes each side of the money to show

# A mid is only meaningful if the spread around it is narrow. These are
# judgment thresholds for flagging, not fitted quantities.
SPREAD_WARN = 0.25   # ask-bid more than a quarter of the mid
SPREAD_BAD = 0.60    # ... or more than 60% of it: not a price


def load(day, expiry):
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={UNDERLYING}", "*.parquet")))
    if not files:
        return None, f"no option bars under {BARS}/date={day}"
    frames, missing = [], set()
    want = ["ts", "expiry", "strike", "opt_type", "close", "bid", "ask"]
    for f in files:
        try:
            have = set(pd.read_parquet(f).columns)
        except (OSError, ValueError):
            continue
        cols = [c for c in want if c in have]
        missing |= set(want) - have
        try:
            df = pd.read_parquet(f, columns=cols)
        except (OSError, ValueError, KeyError):
            continue
        for c in set(want) - set(cols):
            df[c] = None
        frames.append(df)
    if not frames:
        return None, "option bars exist but none could be read"
    df = pd.concat(frames, ignore_index=True)
    df = df[df["expiry"].astype(str).str.slice(0, 10) == expiry.isoformat()]
    if df.empty:
        return None, f"no rows for expiry {expiry}"
    if missing:
        print(f"  [columns absent from the collector's schema: "
              f"{', '.join(sorted(missing))}]")
    df["ts"] = pd.to_datetime(df["ts"])
    if getattr(df["ts"].dt, "tz", None) is not None:
        df["ts"] = df["ts"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df, None


def num(v):
    try:
        f = float(v)
        return f if f == f else None        # NaN check without importing math
    except (TypeError, ValueError):
        return None


def nifty_series(day):
    """[(minute, close)] for NIFTY 50, ascending.

    Delegated to intraday.index_frames rather than re-read here. The INDEX
    partition holds three instruments and the token column is named
    `instrument_token` -- both facts have already cost a debugging round,
    and there is no reason for this file to know either of them.
    """
    frames = I.index_frames(day)
    series = frames.get(I.NIFTY_TOKEN) or {}
    return sorted(series.items())


def close_at(series, minute):
    """The last NIFTY close at or before `minute`, else None."""
    best = None
    for ts, val in series:
        if pd.Timestamp(ts) <= pd.Timestamp(minute):
            best = val
        else:
            break
    return num(best)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    day = date.fromisoformat(args[0]) if args else date.today()
    tail_min = int(args[1]) if len(args) > 1 else 30

    exps = set()
    for f in sorted(glob.glob(os.path.join(
            BARS, f"date={day}", f"underlying={UNDERLYING}", "*.parquet"))):
        try:
            exps.update(pd.read_parquet(f, columns=["expiry"])["expiry"]
                        .astype(str).str.slice(0, 10).unique().tolist())
        except (OSError, ValueError, KeyError):
            continue
    if not exps:
        print(f"no option bars for {day}")
        return 1
    expiry = min(date.fromisoformat(e) for e in exps)

    df, err = load(day, expiry)
    if err:
        print(err)
        return 1

    cutoff = pd.Timestamp(datetime.combine(day, CLOSE)) - pd.Timedelta(
        minutes=tail_min)
    df = df[df["ts"] >= cutoff]
    if df.empty:
        print(f"no bars in the last {tail_min} minutes of {day}")
        return 1

    print(f"CLOSE AUDIT  {day}   expiry {expiry}   "
          f"last {tail_min} minutes")
    print("Is the price the engine used a price anyone would trade at?\n")

    series = nifty_series(day)
    if not series:
        print("the INDEX partition has no NIFTY 50 rows for this day -- "
              "cannot locate the money without a spot")
        return 1

    flagged = 0
    skipped = 0
    examined = 0
    minutes_done = 0
    for minute in sorted(df["ts"].unique()):
        at = df[df["ts"] == minute]
        spot = close_at(series, minute)
        if spot is None:
            skipped += 1
            continue
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
        mins_left = V.minutes_between(pd.Timestamp(minute).time(), CLOSE)
        minutes_done += 1
        print(f"{pd.Timestamp(minute).strftime('%H:%M')}   "
              f"spot {spot:,.2f}   atm {atm:,.0f}   "
              f"{mins_left:.0f} min to {CLOSE}")

        for k in [atm + i * STRIKE_STEP for i in range(-NEAR, NEAR + 1)]:
            leg = {}
            for ot in ("CE", "PE"):
                r = at[(at["strike"].astype(float) == k)
                       & (at["opt_type"].astype(str) == ot)]
                if r.empty:
                    continue
                b, a, c = (num(r["bid"].iloc[0]), num(r["ask"].iloc[0]),
                           num(r["close"].iloc[0]))
                mid = ((a + b) / 2.0 if (b and a and a >= b and b > 0)
                       else c)
                leg[ot] = (b, a, c, mid)
            if len(leg) < 2:
                continue

            credit = sum(v[3] for v in leg.values() if v[3])
            if not credit:
                continue
            intrinsic = abs(spot - k)
            tv = credit - intrinsic

            bits = []
            for ot in ("CE", "PE"):
                b, a, c, mid = leg[ot]
                if b and a and mid:
                    sp = (a - b) / mid
                    tag = ("  !!" if sp >= SPREAD_BAD
                           else "  !" if sp >= SPREAD_WARN else "")
                    bits.append(f"{ot} {b:>7.2f}/{a:>7.2f} mid {mid:>7.2f} "
                                f"sprd {sp:>5.0%}{tag}")
                    examined += 1
                    if sp >= SPREAD_WARN:
                        flagged += 1
                else:
                    bits.append(f"{ot} bid/ask missing, last {c}")
            mark = " <-- ATM" if k == atm else ""
            print(f"   {k:,.0f}{mark}")
            for bit in bits:
                print(f"      {bit}")
            print(f"      credit {credit:>7.2f}   intrinsic {intrinsic:>7.2f}"
                  f"   time value {tv:>7.2f}")
        print()

    # Report what was EXAMINED before reporting what was found. The first
    # run of this script printed "0 legs flagged" after examining nothing at
    # all, because a wrong column name made every spot lookup fail and the
    # loop skipped silently. A clean verdict and an empty pass looked
    # identical. They must never look identical again.
    print(f"examined {examined} legs across {minutes_done} minutes"
          + (f"; skipped {skipped} minutes with no index quote"
             if skipped else ""))
    if not examined:
        print("NOTHING WAS CHECKED -- this is not a clean result. "
              "The data did not line up; do not read it as a pass.")
        return 1
    print(f"{flagged} of them quoted with a spread of {SPREAD_WARN:.0%} or "
          f"more of their own mid.")
    if flagged:
        print("A mid taken from a spread that wide is an average of two "
              "prices nobody traded at.")
    else:
        print("Spreads were tight. The prices the engine used were real, "
              "so the rich end-of-day premium needs another explanation.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

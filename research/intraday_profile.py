"""Is volatility spread evenly across the session? The model assumes it is.

THE ASSUMPTION UNDER TEST

vol_time scales a session's volatility to a partial window by the square root
of elapsed minutes:

    sigma(window) = sigma_day * k_session * sqrt(minutes / 375)

That is correct if and only if every minute of the session carries the same
variance. If variance is U-shaped -- heavy at the open, quiet through the
middle, heavy into the close, which is what index futures usually do -- then
this understates any window containing an open or a close and overstates a
midday one.

WHY IT MATTERS HERE, SPECIFICALLY

Bucketing the backtest by days to expiry showed:

    0 DTE      predicted 78.2%   actual 57.9%   +20.3 points overconfident
    1-2 DTE    predicted 94.7%   actual 100.0%
    3-5 DTE    predicted 99.0%   actual 100.0%
    6+ DTE     predicted 99.6%   actual 100.0%

Every horizon is conservative except the one the gate actually opens at. On
expiry day EVERY horizon is a partial window -- there is no whole day left to
run -- so a bad partial-window rule shows up at 0 DTE and essentially nowhere
else. That is the shape of the error, and it is why this is the first thing
worth measuring.

If the profile is flat, this hypothesis is dead and the 0 DTE problem is
something else -- gamma near the pin, or the normal model's tails, or the
sample being three sessions of one market's weather.

WHAT IT MEASURES

One-minute NIFTY candles over as much history as Kite will serve. For each
time-of-day bucket, the mean squared log return, expressed as a share of the
whole session's variance. A flat session puts 1/N of the variance in each of
N equal buckets; the ratio of measured share to flat share is the correction
factor the model is currently missing.

Then, for the windows that actually matter, it reports how far off
sqrt(minutes/span) is.

A WARNING ABOUT THE LAST BUCKET

The first run of this put 27.1% of the entire session's variance in
15:15-15:30 -- 6.78 times flat. Before believing that, remember what was
found the same morning: the NIFTY index bar freezes near the close and then
catches up in one jump. On 15 September it read 23,172.35 at both 15:27 and
15:28, then 23,118.60 at 15:29. That is a 53.75-point move that never
happened as a move.

One such jump per session carries roughly thirty normal minutes of variance,
and 41 sessions of them land in exactly that bucket. Real closing volatility
and a catch-up artifact are indistinguishable here -- they occupy the same
minutes.

So the horizon table is printed for more than one endpoint. The engine
flattens at 15:15, so sigma_hold never contains the suspect bucket at all;
only sigma_expiry on expiry day does. Where the two tables disagree, the
difference IS the contamination, and only the 15:15 numbers can be acted on.

Nothing is written and nothing is changed. This is a measurement.

Usage:
    python intraday_profile.py --both       BOTH endpoints -- use this one
    python intraday_profile.py --end 15:15  only the hold horizon
    python intraday_profile.py --days 90
    python intraday_profile.py --bucket 30
"""

import json
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from math import log, sqrt

CACHE = "data/cache"
NIFTY_TOKEN = 256265
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)
SESSION_MINUTES = 375.0
CHUNK_DAYS = 55            # Kite caps minute history per request
DEFAULT_DAYS = 60
DEFAULT_BUCKET = 15


def minutes_from_open(t):
    return (t.hour * 60 + t.minute) - (OPEN.hour * 60 + OPEN.minute)


def cache_path(token, start, end, interval):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, f"{token}_{start}_{end}_{interval}.json")


def fetch_minutes(kite, token, start, end, refresh=False):
    """One-minute candles, cached and chunked. Same shape as vol_calibrate."""
    path = cache_path(token, start, end, "minute")
    if os.path.exists(path) and not refresh:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass

    out, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            chunk = kite.historical_data(token, cur, stop, "minute")
        except Exception as e:
            print(f"  fetch failed {cur} -> {stop}: {type(e).__name__}: {e}")
            if not out:
                return []
            break
        out.extend({"date": str(r["date"]), "close": r["close"]}
                   for r in chunk)
        print(f"  {cur} -> {stop}   {len(chunk)} minute candles")
        cur = stop + timedelta(days=1)
        time.sleep(0.4)

    try:
        with open(path, "w") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    return out


def by_session(rows):
    """{date: [(time, close)]} in ascending time."""
    out = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["date"])[:19])
            px = float(r["close"])
        except (TypeError, ValueError):
            continue
        if px <= 0 or not (OPEN <= ts.time() <= CLOSE):
            continue
        out.setdefault(ts.date(), []).append((ts.time(), px))
    for d in out:
        out[d].sort()
    return out


def main():
    days = DEFAULT_DAYS
    bucket = DEFAULT_BUCKET
    ends = [CLOSE]
    for i, a in enumerate(sys.argv):
        if a == "--days":
            days = int(sys.argv[i + 1])
        if a == "--bucket":
            bucket = int(sys.argv[i + 1])
        if a == "--end":
            hh, mm = sys.argv[i + 1].split(":")
            ends = [dtime(int(hh), int(mm))]
        if a == "--both":
            ends = [CLOSE, dtime(15, 15)]

    import session as kite_session
    try:
        kite = kite_session.get_kite()
    except Exception as e:
        print(f"kite session failed: {type(e).__name__}: {e}")
        return 1

    end = date.today()
    start = end - timedelta(days=days)
    print(f"NIFTY 1-minute candles  {start} -> {end}\n")
    rows = fetch_minutes(kite, NIFTY_TOKEN, start, end)
    if not rows:
        print("no candles returned")
        return 1

    sessions = by_session(rows)
    # Only whole sessions. A half-session would bias whichever buckets it
    # happens to contain, and there is no way to tell a holiday from a
    # collection gap after the fact.
    full = {d: v for d, v in sessions.items() if len(v) >= 360}
    print(f"{len(sessions)} sessions returned, {len(full)} with a full day "
          f"of candles\n")
    if len(full) < 10:
        print("too few full sessions to say anything")
        return 1

    nb = int(SESSION_MINUTES // bucket) + 1
    sq = [0.0] * nb          # summed squared returns per bucket
    n = [0] * nb

    for d, series in sorted(full.items()):
        for (t0, p0), (t1, p1) in zip(series, series[1:]):
            if p0 <= 0 or p1 <= 0:
                continue
            m = minutes_from_open(t1)
            if not (0 <= m <= SESSION_MINUTES):
                continue
            b = min(int(m // bucket), nb - 1)
            r = log(p1 / p0)
            sq[b] += r * r
            n[b] += 1

    total = sum(sq)
    if total <= 0:
        print("no usable returns")
        return 1

    flat_share = 1.0 / sum(1 for c in n if c)
    print(f"VARIANCE BY TIME OF DAY   ({bucket}-minute buckets, "
          f"{len(full)} sessions)")
    print("A flat session puts an equal share in every bucket. "
          "'x flat' is the\nfactor the current model is missing.\n")
    hdr = (f"{'window':<16}{'minutes':>9}{'share':>9}{'x flat':>9}  "
           f"{'':<24}")
    print(hdr)
    print("-" * len(hdr))

    om = OPEN.hour * 60 + OPEN.minute
    for b in range(nb):
        if not n[b]:
            continue
        s0 = om + b * bucket
        s1 = min(om + (b + 1) * bucket, CLOSE.hour * 60 + CLOSE.minute)
        share = sq[b] / total
        ratio = share / flat_share
        bar = "#" * min(24, int(round(ratio * 8)))
        print(f"{s0//60:02d}:{s0%60:02d}-{s1//60:02d}:{s1%60:02d}    "
              f"{n[b]:>9}{share:>8.1%}{ratio:>9.2f}  {bar}")

    # --- what this does to the windows the engine actually prices
    #
    # Reported to more than one endpoint on purpose. The engine flattens at
    # 15:15, so sigma_hold never contains the 15:15-15:30 bucket -- and that
    # bucket is the one we cannot trust, because a frozen-then-catching-up
    # index feed produces one large artificial return per session in exactly
    # those minutes. Measuring to 15:15 excludes it; measuring to 15:30
    # includes it. If the two tables disagree, the difference IS the
    # contamination, and only the 15:15 column can be acted on.
    for end in ends:
        end_min = end.hour * 60 + end.minute
        keep = [b for b in range(nb)
                if n[b] and (om + b * bucket) < end_min]
        sub_total = sum(sq[b] for b in keep)
        if sub_total <= 0:
            continue
        print()
        print(f"EFFECT ON A WINDOW ENDING AT {end.strftime('%H:%M')}")
        if end == CLOSE:
            print("  INCLUDES the 15:15-15:30 bucket -- treat with suspicion.")
        else:
            print("  Excludes the suspect closing bucket. This is the one the")
            print("  hold horizon actually uses.")
        print()
        span = end_min - om
        hdr2 = (f"{'from':<8}{'mins':>8}{'model':>10}{'measured':>11}"
                f"{'ratio':>9}")
        print(hdr2)
        print("-" * len(hdr2))
        for b in keep:
            s0 = om + b * bucket
            mins_left = end_min - s0
            if mins_left <= 0:
                continue
            model = sqrt(mins_left / span)
            measured = sqrt(sum(sq[c] for c in keep if c >= b) / sub_total)
            print(f"{s0//60:02d}:{s0%60:02d}   {mins_left:>8.0f}"
                  f"{model:>10.3f}{measured:>11.3f}{measured/model:>9.2f}")

    print()
    print("  ratio > 1  the model UNDERSTATES the risk of that window")
    print("  ratio < 1  it overstates it")
    print()
    print(f"  {len(full)} sessions is roughly {len(full)//5} weeks of one")
    print("  market's weather. A U-shape that survives this is worth acting")
    print("  on; a small wobble is not.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

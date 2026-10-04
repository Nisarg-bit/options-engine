"""Is the open-interest field trustworthy at the session open?

THE OBSERVATION THAT PROMPTED THIS

chain_buildup.py, anchored on the session's first minute, reported this on
2026-09-16 against the 6 DTE expiry:

    21,350 PE    open interest    0  ->  1,961,570

That cannot be true. Open interest counts contracts OUTSTANDING, and
outstanding positions are carried overnight -- yesterday's close already
held those contracts. A 1,900-point out-of-the-money put does not go from
literally zero to two million in one morning. Something about the 09:15
reading is wrong, not something about the market.

And it was not one row. Across the chain, 108 of 142 contracts came back
"building" and OI roughly trebled board-wide. An indicator that says the
same thing about three quarters of the board is not reading positioning;
it is reading its own baseline.

WHAT THIS MEASURES

Whether OI is still arriving during the first minutes of the session. The
test is deliberately not "does 09:15 look odd" -- odd is a judgement. It is:

    take each contract's OI late in the session as the settled value,
    and watch what fraction of that value each early minute reports.

If the feed is warm at 09:15, that fraction is already near 1 and stays
there: OI drifts during a session, it does not multiply. If the feed is
cold, the early minutes read far below the settled value and climb toward
it -- and the minute where the climb stops is the earliest honest anchor.

WHY THE SETTLED VALUE IS TAKEN FROM MIDDAY, NOT THE LAST MINUTE

Real positioning does accumulate over a session, so the last minute is a
moving target that conflates warm-up with the day's actual trading. A
reference taken around 11:00 is late enough to be warm and early enough
that it has not absorbed the whole day. The ratio is then "how much of an
already-settled number was visible at 09:15" rather than "how much of the
day had happened".

WHAT WOULD FALSIFY THE WARM-UP STORY

If early OI is LOW but climbs smoothly all day rather than jumping to a
plateau, that is ordinary accumulation and the anchor is fine -- the
board-wide "building" verdict would then be true, if uninformative. The
plateau is the thing to look for: a discontinuity followed by flatness is
a feed filling in, not a market trading.

Usage:
    python oi_warmup.py
    python oi_warmup.py --session 2026-09-16 --expiry 2026-09-22
    python oi_warmup.py --minutes 30 --ref 11:00
"""

import sys
from datetime import date

import series_probe as SP

REF = "11:00"          # settled reference; see docstring
EARLY = 20             # how many opening minutes to print
MIN_OI = 100_000       # only contracts with enough OI for the ratio to mean
SHOW = 8               # contracts in the detail table


def _oi(by_minute, ts, strike, ot):
    m = by_minute.get(ts, {}).get(strike, {}).get(ot)
    return None if m is None else m.get("oi")


def contracts(by_minute, ref_ts, min_oi=MIN_OI):
    """(strike, opt_type) pairs that are liquid enough to judge."""
    out = []
    for k, legs in by_minute.get(ref_ts, {}).items():
        for ot, v in legs.items():
            oi = v.get("oi")
            if oi is not None and oi >= min_oi:
                out.append((k, ot, float(oi)))
    return sorted(out, key=lambda r: -r[2])


def coverage(by_minute, times, ref_ts, cons, upto):
    """For each early minute: median (OI at that minute / OI at ref).

    Median rather than mean, because a handful of contracts reading zero
    would drag a mean to a number no individual contract shows.
    """
    rows = []
    for ts in times[:upto]:
        fracs, zeros, missing = [], 0, 0
        for k, ot, ref in cons:
            v = _oi(by_minute, ts, k, ot)
            if v is None:
                missing += 1
                continue
            if v == 0:
                zeros += 1
            fracs.append(v / ref if ref else 0.0)
        if not fracs:
            rows.append((ts, None, zeros, missing, 0))
            continue
        fracs.sort()
        n = len(fracs)
        med = (fracs[n // 2] if n % 2 else 0.5 * (fracs[n // 2 - 1] + fracs[n // 2]))
        rows.append((ts, med, zeros, missing, n))
    return rows


def main():
    args = {"--session": None, "--expiry": None,
            "--ref": REF, "--minutes": str(EARLY), "--min-oi": str(MIN_OI)}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]

    days = SP.sessions()
    if not days:
        print(f"no sessions under {SP.BARS}")
        return 1
    day = date.fromisoformat(args["--session"]) if args["--session"] else days[-1]

    exps = [e for e in SP.expiries_on(day, SP.UNDERLYING) if e >= day]
    if not exps:
        print(f"no expiries on {day}")
        return 1
    expiry = date.fromisoformat(args["--expiry"]) if args["--expiry"] else exps[0]

    bm, err = SP.by_minute(day, SP.UNDERLYING, expiry)
    if err:
        print(f"adapter failed: {err}")
        return 1
    times = sorted(bm)

    ref_ts = args["--ref"]
    if ref_ts not in bm:
        later = [t for t in times if t >= ref_ts]
        if not later:
            print(f"session ends at {times[-1]}, before the {ref_ts} reference.")
            print("Nothing can be judged: rerun after the reference time, or")
            print(f"pass --ref {times[-1]}.")
            return 1
        ref_ts = later[0]

    cons = contracts(bm, ref_ts, float(args["--min-oi"]))
    if not cons:
        print(f"no contract at {ref_ts} carries {float(args['--min-oi']):,.0f} OI")
        return 1

    print(f"OI WARM-UP   {day}   expiry {expiry}   "
          f"({(expiry - day).days} DTE)")
    print(f"session {times[0]} -> {times[-1]} ({len(times)} minutes)")
    print(f"reference {ref_ts}, {len(cons)} contracts at or above "
          f"{float(args['--min-oi']):,.0f} OI\n")

    upto = int(args["--minutes"])
    rows = coverage(bm, times, ref_ts, cons, upto)

    print(f"SHARE OF THE {ref_ts} OI VISIBLE AT EACH EARLY MINUTE\n")
    hdr = f"{'time':<7}{'median share':>14}{'reading 0':>11}{'absent':>8}{'seen':>7}"
    print(hdr)
    print("-" * len(hdr))
    for ts, med, zeros, missing, n in rows:
        m = f"{med:>13.1%}" if med is not None else f"{'-':>13}"
        print(f"{ts:<7}{m} {zeros:>10} {missing:>7} {n:>6}")

    # The plateau test. Warm-up is a step followed by flatness; ordinary
    # accumulation is a slope that keeps going. Comparing the early climb
    # against the rest of the session separates them.
    print()
    tail_ts = [t for t in times if t >= ref_ts]
    if len(tail_ts) >= 2:
        tail = coverage(bm, tail_ts, ref_ts, cons, len(tail_ts))
        first_med = rows[0][1]
        settle = [r[1] for r in rows if r[1] is not None]
        drift = [r[1] for r in tail if r[1] is not None]
        if settle and drift:
            print("HOW IT BEHAVES AFTER THE REFERENCE")
            print(f"  first minute ({rows[0][0]}):        "
                  f"{first_med:.1%} of the {ref_ts} value"
                  if first_med is not None else "  first minute: no reading")
            print(f"  {ref_ts} onward, median share ranges "
                  f"{min(drift):.1%} to {max(drift):.1%}")
            print()
            print("  A feed filling in looks like a low first minute and a")
            print("  narrow range afterwards. Genuine accumulation keeps")
            print("  climbing past the reference instead of flattening.")

    print()
    print(f"THE {SHOW} LARGEST CONTRACTS, MINUTE BY MINUTE\n")
    head = times[:min(upto, 12)]
    hdr = f"{'strike':>10}  " + "".join(f"{t[3:]:>7}" for t in head) + f"{'ref':>11}"
    print(hdr)
    print("-" * len(hdr))
    for k, ot, ref in cons[:SHOW]:
        cells = ""
        for ts in head:
            v = _oi(bm, ts, k, ot)
            cells += f"{'-':>7}" if v is None else f"{v / 1e6:>7.2f}"
        print(f"{k:>7,.0f} {ot}  {cells}{ref / 1e6:>11.2f}")
    print(f"\n  millions of contracts; column headings are minutes past "
          f"{head[0][:2]}:00")

    print()
    print("READ THIS AS A MEASUREMENT, NOT A VERDICT")
    print("  If the early shares are near 100%, the 09:15 anchor is sound and")
    print("  the board-wide 'building' reading is real. If they are far below")
    print("  and then flatten, the anchor is reading a feed that had not")
    print("  finished arriving, and chain_buildup.py needs the same treatment")
    print("  series.py already got: skip the opening minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

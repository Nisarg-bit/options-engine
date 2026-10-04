"""Why does the spot guard refuse 71% of minutes, and does DTE explain it?

THE QUESTION

Adding models.spot_is_believable to the intraday backtest cut 233 scored
recommendations to 68 and made terminal calibration go from 3.5 points out
to 21.7. That is the opposite of what a data-quality filter should do. Two
explanations fit, and they have opposite consequences:

  A. THE GUARD IS BROKEN. A 71% refusal rate is not a filter, it is a
     rejection of the normal case. Either the carry allowance is far too
     small for the real basis at higher DTE, or the dispersion term is
     firing on stale option LEGS -- a different fault than the stale index
     it was built for.

  B. THE GUARD IS RIGHT AND THE SAMPLE WAS NEVER HOMOGENEOUS. The guard
     drops the far-dated sessions and keeps the near-dated ones. If
     calibration is much worse near expiry, then "3.5 points out" over 233
     mixed predictions was an average across easy and catastrophic days, and
     reporting it as one number was hiding the problem rather than measuring
     it.

WHAT SEPARATES THEM

Two sections, deliberately independent.

  PART 1  audits the guard. Per session, per decision minute: the basis, the
          dispersion, the tolerance, and which test failed. If refusals at
          5-6 DTE are basis gaps growing smoothly with time to expiry, the
          carry allowance is simply too tight and A holds. If they are
          dispersion blowouts, it is stale legs.

  PART 2  ignores the guard entirely and buckets the UNGUARDED advisory
          ledger by days to expiry. If calibration error rises as expiry
          approaches with the guard switched off, B holds and it is a real
          structural finding about the model, not an artefact of filtering.

Part 2 is the one that matters. Part 1 can only tell us whether a tool I
built yesterday works; part 2 tells us something about the market.

Usage:
    python basis_report.py              all sessions
    python basis_report.py --newvol     price with the fitted calibration
"""

import sys
from datetime import date, datetime, time as dtime

import intraday as I
import models as M
import vol_time as V
from position import CE, PE

# Buckets chosen so 0 DTE stands alone -- expiry day is a different animal
# and averaging it with anything hides exactly what we are looking for.
BUCKETS = [(0, 0, "0 DTE (expiry day)"), (1, 2, "1-2 DTE"),
           (3, 5, "3-5 DTE"), (6, 99, "6+ DTE")]


def bucket_of(dte):
    for lo, hi, name in BUCKETS:
        if lo <= dte <= hi:
            return name
    return "?"


def pct(x):
    return f"{x:>7.1%}"


def quantiles(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[0], s[n // 4], s[n // 2], s[(3 * n) // 4], s[-1]


# ------------------------------------------------------- part 1: the guard

def audit_guard():
    print("=" * 72)
    print("PART 1 -- WHAT THE GUARD IS ACTUALLY REFUSING")
    print("=" * 72)
    print("For every decision minute: how far the index sits from the "
          "chain's\nown forward, how much the strikes disagree among "
          "themselves, and\nwhich test failed.\n")

    hdr = (f"{'session':<12}{'dte':>4}{'mins':>6}{'refused':>9}"
           f"{'basis med':>11}{'basis max':>11}{'disp med':>10}"
           f"{'disp max':>10}{'tol med':>9}")
    print(hdr)
    print("-" * len(hdr))

    totals = {"basis": 0, "incoherent": 0, "thin": 0, "ok": 0}
    for day in I.sessions():
        exps = [e for e in I.expiries_on(day) if e >= day]
        if not exps:
            continue
        expiry = exps[0]
        book = I.option_book(day, expiry)
        if not book:
            continue
        idx = I.index_frames(day)
        nifty = idx.get(I.NIFTY_TOKEN, {})
        if not nifty:
            continue
        dte = (expiry - day).days

        bases, disps, tols, refused = [], [], [], 0
        for t in sorted(book):
            if not (I.FIRST_ENTRY <= t.time() <= I.LAST_ENTRY):
                continue
            spot = nifty.get(t)
            if spot is None:
                continue
            prices = book[t]
            rows = []
            for k in sorted({k for k, _ in prices}):
                c, p = prices.get((k, CE)), prices.get((k, PE))
                if c and p:
                    rows.append({"strike": k, "ce": c, "pe": p})
            years = max(0.0, dte + V.minutes_between(t.time(), dtime(15, 30))
                        / V.SESSION_MINUTES) / 365.0
            ok, fwd, disp, tol, why = M.spot_is_believable(rows, spot, years)
            if fwd is None:
                totals["thin"] += 1
                refused += 1
                continue
            bases.append(fwd - spot)
            disps.append(disp)
            tols.append(tol)
            if ok:
                totals["ok"] += 1
            else:
                refused += 1
                totals["incoherent" if disp > M.CHAIN_INCOHERENT
                       else "basis"] += 1

        if not bases:
            continue
        bq, dq, tq = quantiles(bases), quantiles(disps), quantiles(tols)
        print(f"{str(day):<12}{dte:>4}{len(bases):>6}{refused:>9}"
              f"{bq[2]:>11.1f}{max(abs(bq[0]), abs(bq[4])):>11.1f}"
              f"{dq[2]:>10.1f}{dq[4]:>10.1f}{tq[2]:>9.1f}")

    print()
    n = sum(totals.values())
    if n:
        print(f"  passed                      {totals['ok']:>6}  "
              f"({totals['ok']/n:.0%})")
        print(f"  refused: basis too wide     {totals['basis']:>6}  "
              f"({totals['basis']/n:.0%})")
        print(f"  refused: chain incoherent   {totals['incoherent']:>6}  "
              f"({totals['incoherent']/n:.0%})")
        print(f"  refused: too few strikes    {totals['thin']:>6}  "
              f"({totals['thin']/n:.0%})")
    print()
    print("  READ IT THIS WAY. If 'basis med' grows smoothly with dte and")
    print("  the refusals are nearly all 'basis too wide', the carry")
    print("  allowance is too tight and the guard is miscalibrated -- the")
    print("  basis is real and I underestimated it. If the refusals are")
    print("  'chain incoherent', individual option legs are stale and the")
    print("  guard is catching a different fault than it was built for.")


# --------------------------------------------- part 2: calibration by DTE

def calibration_by_dte(cal):
    print()
    print("=" * 72)
    print("PART 2 -- CALIBRATION BY DAYS TO EXPIRY, GUARD OFF")
    print("=" * 72)
    print("The guard plays no part here. Every session is scored exactly as")
    print("it was in the 233-prediction run; the rows are only SORTED by")
    print("time to expiry. If error rises as expiry approaches, the single")
    print("headline number was averaging unlike things.\n")

    rows = []
    for day in I.sessions():
        r = I.run_session(day, 15, 0.85, I.CS_GATE, I.TWO_LEG,
                          calibrate=True, cal=cal, guard=False)
        if r is None:
            continue
        dte = (r["expiry"] - day).days
        for a in r["advisory"]:
            if a.get("exit"):
                rows.append((dte, a))

    if not rows:
        print("  no scored recommendations -- nothing to bucket")
        return

    hdr = (f"{'bucket':<20}{'n':>5}{'sessions':>10}{'pred POP':>10}"
           f"{'actual':>9}{'error':>8}{'pred NT':>9}{'actual':>9}"
           f"{'error':>8}")
    print(hdr)
    print("-" * len(hdr))

    for _, _, name in BUCKETS:
        sub = [a for d, a in rows if bucket_of(d) == name]
        if not sub:
            continue
        n = len(sub)
        sess = len({a["ts"].date() for a in sub})
        pt = sum(a["cand"]["pop_terminal"] for a in sub) / n
        inside = sum(1 for a in sub
                     if a["final"] is not None
                     and a["cand"]["lower"] < a["final"] < a["cand"]["upper"])
        pn = sum(a["cand"]["pop_no_touch"] for a in sub) / n
        untouched = sum(1 for a in sub if not a["touched"])
        print(f"{name:<20}{n:>5}{sess:>10}{pct(pt)}{pct(inside/n)}"
              f"{(pt - inside/n)*100:>+7.1f}"
              f"{pct(pn)}{pct(untouched/n)}{(pn - untouched/n)*100:>+7.1f}")

    print()
    print("  'error' is predicted minus actual, in percentage points.")
    print("  Positive means the model claimed more safety than it got.")
    print()
    print("  A single session count of 1 or 2 in any bucket means that row")
    print("  is one market's weather, not a measurement. Read the session")
    print("  column before the error column.")


def main():
    cal = I.load_calibration() if "--newvol" in sys.argv else None
    if "--newvol" in sys.argv and cal is None:
        print(f"--newvol asked for but {I.CALIBRATION} is missing; "
              f"using the old sigma\n")
    print(f"SIGMA: {'fitted calibration' if cal else 'old flat sqrt(t)'}\n")
    audit_guard()
    calibration_by_dte(cal)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

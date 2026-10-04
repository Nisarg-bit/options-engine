"""Old decay model vs new, on the SAME rows. Settles it with numbers.

Written because I predicted how the capture column would move and then could
not check the prediction: the session count had changed from 13 to 14 between
runs, and I had quoted 0.57 from the journal's realised/implied ratio, which
is a different metric from capture entirely. Two numbers from memory, neither
re-derived.

This computes both models on identical sessions and identical structures, so
there is nothing to confound.
"""
import sys

import intraday_vrp as V

UND = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
WIDTHS = [0, 2, 4]


def old_theo(day, expiry, ei, xi, credit):
    """intraday_vrp.theoretical_decay * credit -- the model being replaced."""
    return V.theoretical_decay(day, expiry, ei, xi) * credit


def main():
    days = V.sessions(UND)
    if not days:
        print("no sessions")
        return 1

    print(f"\n{UND}   {len(days)} sessions   {days[0]} -> {days[-1]}\n")
    hdr = (f"  {'session':<12}{'w':>3}{'credit':>9}{'old theo':>10}"
           f"{'new theo':>10}{'ratio':>8}{'old cap':>9}{'new cap':>9}"
           f"  {'model':<22}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    by_w = {w: {"old": [], "new": [], "ocap": [], "ncap": []} for w in WIDTHS}
    fallbacks = 0

    for d in days:
        bm, exp, err = V.load(d, UND)
        if err:
            continue
        for w in WIDTHS:
            r = V.simulate(bm, d, exp, width=w)
            if not r:
                continue
            all_times = sorted(bm)
            ei = all_times.index(r["entry"])
            xi = (all_times.index(r["exit"]) if r["exit"] in bm
                  else len(all_times) - 1)
            ot = old_theo(d, exp, ei, xi, r["credit"])
            nt = r["theo_decay_pts"]
            if r.get("theo_model", "").startswith("time-fraction"):
                fallbacks += 1
            ocap = r["pnl_pts"] / ot if ot else None
            ncap = r["capture"]
            by_w[w]["old"].append(ot)
            by_w[w]["new"].append(nt)
            if ocap is not None:
                by_w[w]["ocap"].append(ocap)
            if ncap is not None:
                by_w[w]["ncap"].append(ncap)
            print(f"  {str(d):<12}{w:>3}{r['credit']:>9.2f}{ot:>10.2f}"
                  f"{nt:>10.2f}{nt / ot if ot else 0:>8.2f}"
                  f"{ocap if ocap is not None else 0:>9.2f}"
                  f"{ncap if ncap is not None else 0:>9.2f}"
                  f"  {r.get('theo_model', '?'):<22}")

    print(f"\n  fallbacks to the old formula: {fallbacks}"
          f"   (should be 0 on complete sessions)\n")

    print("  MEDIANS BY WIDTH\n")
    print(f"  {'width':>6}{'old theo':>11}{'new theo':>11}{'theo x':>9}"
          f"{'old cap':>10}{'new cap':>10}{'cap x':>8}")
    print("  " + "-" * 65)
    for w in WIDTHS:
        o, n = V.med(by_w[w]["old"]), V.med(by_w[w]["new"])
        oc, nc = V.med(by_w[w]["ocap"]), V.med(by_w[w]["ncap"])
        if not o:
            continue
        print(f"  {w:>6}{o:>11.2f}{n:>11.2f}{n / o:>9.2f}"
              f"{oc:>10.3f}{nc:>10.3f}{(nc / oc) if oc else 0:>8.2f}")

    print("\n  WHAT THIS SHOULD SHOW IF THE CHANGE IS CORRECT\n")
    print("    width 0 : theo x = 1.00 to within the bid/mid half-spread,")
    print("              because the two models are algebraically identical")
    print("              on a straddle. Anything else means the identity is")
    print("              broken and every ATM number on the site moved.")
    print("    width 2 : theo x well ABOVE 1 -- the old model understated it")
    print("    width 4 : theo x higher still")
    print("    capture moves INVERSELY: same P&L, larger denominator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

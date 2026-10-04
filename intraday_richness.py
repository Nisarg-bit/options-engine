"""Does the 09:20 option price know anything about the day that follows?

THE QUESTION, AND WHY IT IS THE ONLY ONE LEFT

intraday_vrp.py established that P&L is the forward move and essentially
nothing else: corr(|forward move|, net) = -0.968 across 12 sessions. That is
not a finding, it is the definition -- selling a straddle IS a bet that the
move will be small, so "profitable when the move is small" is circular and
cannot be traded on.

The non-circular question is whether the PRICE at entry carries information
about the move that follows. Two separable parts:

  IS THERE A PREMIUM?   Compare what the 09:20 straddle implies for the
                        session against what the session realises. If implied
                        sits systematically above realised, a seller is being
                        paid for something.

  IS THERE A SIGNAL?    Does a richer-than-usual morning predict a quieter
                        day? If corr(implied, realised) is around zero, the
                        price knows nothing, richness is not tradeable, and
                        there is nothing here to build on.

The first can be true while the second is false. A constant premium is
harvestable without any forecasting; a premium that is zero on average but
predictable would be tradeable with a filter. Both false means stop.

HOW REALISED IS MEASURED, AND WHY IT MATTERS

Not close-minus-open. That is a single draw with enormous variance -- on 12
sessions it would tell you almost nothing, and the earlier study leaned on it
only because it is what a trade actually pays out on.

Here realised volatility comes from the MINUTE PATH: the square root of the
summed squared minute-to-minute changes in the parity forward. That is ~370
observations per session instead of one, which is what makes a 12-session
sample able to say anything at all.

The cost of that precision is microstructure noise. The forward is derived
from bid/ask midpoints, so consecutive minutes carry quote jitter that is not
volatility, and summing squared 1-minute changes overstates the truth. So it
is computed at 1, 5 and 15 minute sampling and all three are reported. The
gap between them IS the noise, and if the 1-minute figure is wildly above the
15-minute one, that is the estimator talking rather than the market.

Nothing here uses VIX. Implied comes from the chain's own price.

Usage:
    python intraday_richness.py
    python intraday_richness.py --entry 09:20 --exit 15:15 --min-dte 1
"""

import sys
from math import pi, sqrt

import intraday_vrp as V

# E|X| = sigma * sqrt(2/pi) for a zero-mean normal, so a Bachelier straddle is
# worth 2 * sigma * phi(0) = 0.7979 * sigma. Inverting that is how a straddle
# price becomes an implied absolute move.
BACHELIER = sqrt(2.0 / pi)          # 0.79788


def implied_session_sigma(straddle_mid, sessions_left):
    """The 09:20 straddle, expressed as an implied ONE-SESSION sigma.

    The straddle prices movement to expiry, not to this afternoon, so it is
    de-scaled by sqrt(sessions remaining). Everything observable at 09:20.
    """
    if sessions_left <= 0 or straddle_mid <= 0:
        return None
    return straddle_mid / BACHELIER / sqrt(sessions_left)


def forward_path(bm, times):
    out = []
    for t in times:
        f = V.forward(bm[t])
        if f is not None:
            out.append((t, f))
    return out


def realised_sigma(path, every=1):
    """Root sum of squared changes in the forward, sampled every `every` minutes.

    Sampling coarser trades statistical precision for immunity to quote
    jitter. Both matter here, so the caller asks for several.
    """
    pts = path[::every]
    if len(pts) < 3:
        return None
    d = [b[1] - a[1] for a, b in zip(pts, pts[1:])]
    return sqrt(sum(x * x for x in d))


def corr(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    sx = sqrt(sum((x - mx) ** 2 for x, _ in pairs))
    sy = sqrt(sum((y - my) ** 2 for _, y in pairs))
    return cov / (sx * sy) if sx and sy else None


def med(xs):
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def study(day, bm, expiry, entry, exit_at):
    times = sorted(t for t in bm if entry <= t <= exit_at)
    if len(times) < 30:
        return None
    t0 = times[0]

    atm = V.atm_by_parity(bm[t0])
    if atm is None:
        return None
    legs = V.legs_for(atm, V.infer_step([k for m in bm.values() for k in m]), 0)
    # MID here, not bid: this measures what the market thinks, not what a
    # trade would fetch. Costs belong in the P&L study, not in the forecast.
    sell, cover = V.sell_price(bm[t0], legs), V.cover_price(bm[t0], legs)
    if sell is None or cover is None:
        return None
    straddle = 0.5 * (sell + cover)

    all_times = sorted(bm)
    n_full = V.trading_days(day, expiry)
    s_left = n_full + (V.SESSION_MINUTES - all_times.index(t0)) / V.SESSION_MINUTES
    imp = implied_session_sigma(straddle, s_left)
    if imp is None:
        return None

    path = forward_path(bm, times)
    if len(path) < 30:
        return None
    return {
        "date": day, "dte": n_full, "straddle": straddle,
        "sessions_left": s_left, "implied": imp,
        "rv1": realised_sigma(path, 1),
        "rv5": realised_sigma(path, 5),
        "rv15": realised_sigma(path, 15),
        "net_move": abs(path[-1][1] - path[0][1]),
        "minutes": len(path),
    }


def main():
    args = {"--underlying": "NIFTY", "--entry": "09:20", "--exit": "15:15",
            "--min-dte": "0"}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]
    und, entry, exit_at = args["--underlying"], args["--entry"], args["--exit"]
    min_dte = int(args["--min-dte"])

    days = V.sessions(und)
    if not days:
        print(f"no sessions under {V.BARS}")
        return 1
    print(f"INTRADAY RICHNESS   {und}   {len(days)} sessions   "
          f"{days[0]} to {days[-1]}")
    print(f"entry {entry}  exit {exit_at}  min DTE {min_dte}  no VIX\n")

    rows = []
    for d in days:
        bm, exp, err = V.load(d, und)
        if err:
            print(f"  SKIPPED {d}: {err}")
            continue
        if max(bm) < exit_at:
            print(f"  SKIPPED {d}: data ends {max(bm)}, before {exit_at}")
            continue
        if V.trading_days(d, exp) < min_dte:
            print(f"  SKIPPED {d}: below {min_dte} DTE")
            continue
        r = study(d, bm, exp, entry, exit_at)
        if r:
            rows.append(r)

    if len(rows) < 4:
        print(f"\n  only {len(rows)} usable sessions — not enough to say anything")
        return 1

    hdr = (f"{'date':<12}{'dte':>4}{'straddle':>10}{'implied s':>11}"
           f"{'rv 1min':>9}{'rv 5min':>9}{'rv 15min':>10}"
           f"{'net move':>10}{'rv5/imp':>9}")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{str(r['date']):<12}{r['dte']:>4}{r['straddle']:>10.2f}"
              f"{r['implied']:>11.1f}{r['rv1']:>9.1f}{r['rv5']:>9.1f}"
              f"{r['rv15']:>10.1f}{r['net_move']:>10.1f}"
              f"{r['rv5'] / r['implied']:>9.2f}")
    print("-" * len(hdr))

    imp = [r["implied"] for r in rows]
    print(f"\n  MICROSTRUCTURE CHECK — the same volatility, sampled three ways")
    for k, lbl in (("rv1", "1-minute"), ("rv5", "5-minute"), ("rv15", "15-minute")):
        print(f"    median realised, {lbl:<10} {med([r[k] for r in rows]):>7.1f} pts")
    r1, r15 = med([r["rv1"] for r in rows]), med([r["rv15"] for r in rows])
    print(f"    1-min sits {100 * (r1 / r15 - 1):+.0f}% against 15-min. A large "
          f"positive gap is quote\n    jitter being counted as volatility, not "
          f"the market moving.")

    print(f"\n  IS THERE A PREMIUM?   implied at 09:20 against what the session did")
    for k, lbl in (("rv1", "1-min"), ("rv5", "5-min"), ("rv15", "15-min")):
        ratios = [r[k] / r["implied"] for r in rows]
        print(f"    realised({lbl:<6}) / implied    median {med(ratios):>5.2f}   "
              f"mean {sum(ratios) / len(ratios):>5.2f}   "
              f"{sum(1 for x in ratios if x < 1)}/{len(ratios)} below 1.0")
    print("    Below 1.0 means the morning price was above what the day "
          "delivered —\n    a seller was paid for movement that did not arrive.")

    print(f"\n  IS THERE A SIGNAL?   does the morning price predict the day?")
    for k, lbl in (("rv1", "1-min"), ("rv5", "5-min"), ("rv15", "15-min")):
        c = corr(imp, [r[k] for r in rows])
        print(f"    corr(implied, realised {lbl:<6})  {c:+.3f}"
              if c is not None else f"    corr(implied, realised {lbl}) n/a")
    c = corr(imp, [r["net_move"] for r in rows])
    print(f"    corr(implied, |net move|)     {c:+.3f}" if c is not None else "")
    print(f"    corr(DTE, implied)            {corr([r['dte'] for r in rows], imp):+.3f}"
          f"   <- if this is large, 'richness' is mostly just DTE")

    print(f"\n  RICH DAYS AGAINST CHEAP ONES   split at the median implied sigma")
    cut = med(imp)
    for name, sel in (("rich (implied >= median)", lambda r: r["implied"] >= cut),
                      ("cheap (implied <  median)", lambda r: r["implied"] < cut)):
        g = [r for r in rows if sel(r)]
        if not g:
            continue
        ratios = [r["rv5"] / r["implied"] for r in g]
        print(f"    {name:<26} n={len(g):<3} median realised/implied "
              f"{med(ratios):>5.2f}   median net move {med([r['net_move'] for r in g]):>6.1f}")
    print("    A seller wants the rich group to show a LOWER ratio: paid more, "
          "and the\n    movement still did not come. Same ratio in both means "
          "richness is not a signal.")

    print(f"\n  {len(rows)} sessions. The ratio is measured on ~370 minute "
          f"observations per\n  session and is the sturdier number; the "
          f"correlation is 12 points and is not.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

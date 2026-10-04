"""Is near-dated implied volatility above India VIX, or below it?

THE CLAIM UNDER TEST

The dashboard says the weekly strategy's 0-DTE failure -- 78.2% safety claimed,
57.9% delivered -- happened because India VIX is a THIRTY-DAY implied vol being
applied to a same-day horizon, "where the market prices volatility about a
third higher". That predicts near-dated ATM IV runs ABOVE VIX, and increasingly
so as DTE falls.

/api/term measured the opposite on one session: every expiry from 3 to 30 DTE
priced 6-10% BELOW VIX, near-flat. But the nearest reading there was 3 DTE, and
a single session is a single session.

TWO EXPLANATIONS, AND THEY PREDICT DIFFERENT THINGS

  term structure     Near expiries genuinely price higher vol. Then the ratio
                     ATM_IV / VIX should RISE as DTE falls, and cross above 1
                     somewhere near expiry. This is what the dashboard claims.

  methodology gap    India VIX is computed variance-swap style across the whole
                     smile, including the dearer out-of-the-money wings, so it
                     sits structurally above any single at-the-money strike. A
                     methodology gap is roughly CONSTANT in DTE -- it would show
                     as a flat ratio below 1 at every horizon, including zero.

The 0 DTE bucket separates them. Nothing else does.

WHY IT DOES NOT USE api.market_ref

market_ref reads spot and VIX from LIVE Kite quotes. Pointed at a session from
three weeks ago it would paint today's index level onto that day's chain and
produce confident nonsense. Spot and VIX are read here from each session's own
INDEX partition instead -- which is exactly the rewiring the replay picker will
need, so this doubles as its prototype.
"""

import sys
from collections import defaultdict

import api as API
import intraday_vrp as V
import models as M

UND = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"

# India VIX trades near 11-14; an index trades in the thousands. Detecting the
# volatility series by magnitude rather than by token id keeps this working if
# the instrument list ever changes, and it is self-checking in a way a
# hard-coded 264969 is not -- NIFTY_TOKEN is copy-pasted into seven files here
# and none of them would notice if it went stale.
VIX_MAX_LEVEL = 100.0


def index_and_vix(day):
    """(spot, vix) from this session's own bars, or (None, None)."""
    df = API.read_parts(day, "INDEX")
    if df is None or df.empty or "instrument_token" not in df.columns:
        return None, None
    med = df.groupby("instrument_token")["close"].median()
    med = med[med > 0]
    if med.empty:
        return None, None

    vix_toks = med[med < VIX_MAX_LEVEL]
    idx_toks = med[med >= VIX_MAX_LEVEL]
    if vix_toks.empty or idx_toks.empty:
        return None, None

    # The index of interest is the one whose level the chain's strikes sit
    # around. With NIFTY and BANKNIFTY both in the partition, take the lower --
    # BANKNIFTY trades near 57,000 against NIFTY's 23,000.
    tok = idx_toks.idxmin() if UND == "NIFTY" else idx_toks.idxmax()
    last = lambda t: float(df[df["instrument_token"] == t]
                           .sort_values("ts")["close"].iloc[-1])
    return last(tok), last(vix_toks.idxmin())


def main():
    days = API.sessions()
    if not days:
        print("no sessions")
        return 1

    print(f"\nATM IV vs INDIA VIX   {UND}   {len(days)} sessions\n")
    hdr = (f"  {'session':<12}{'expiry':<12}{'cal':>5}{'trd':>5}{'spot':>10}"
           f"{'vix':>8}{'atm IV':>9}{'IV/VIX':>9}{'adjusted':>10}{'strikes':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    buckets = defaultdict(list)
    for d in days:
        spot, vix = index_and_vix(d)
        if not spot or not vix:
            print(f"  {str(d):<12}  no index/vix bars")
            continue
        bars = API.last_bars(d, UND)
        if bars is None:
            continue
        # dte relative to THE SESSION, not to now. Getting this wrong is the
        # single easiest way to produce plausible, wrong history.
        exps = sorted(e for e in bars["expiry"].unique() if (e - d).days >= 0)
        for exp in exps[:3]:
            dte = (exp - d).days
            try:
                chain, _dr, _t, _ts = API.build_chain(d, UND, exp)
            except Exception:
                continue
            if not chain or len(chain) < 8:
                continue
            flat = API.priced(chain)
            iv = M.atm_iv(flat, spot, max(dte, 1) / 365.0)
            if not iv:
                continue
            iv *= 100.0
            # TRADING days, not calendar. sigma = spot * vol * sqrt(dte/365)
            # counts Saturday and Sunday as volatility-bearing time, so an IV
            # solved over a window containing a weekend comes out understated
            # by sqrt(trading/calendar). NIFTY weeklies expire on a Tuesday,
            # so a Friday reading spans two dead days out of four and is
            # understated by ~29%. That artifact is large enough to invert the
            # sign of the thing this script is measuring.
            trd = V.trading_days(d, exp)
            adj = (trd / dte) ** 0.5 if dte > 0 else 1.0
            ratio = iv / vix
            # An expiry-day close has no time value left to imply a vol from:
            # 27 strikes survive and the solve returns ~1%. That is the
            # measurement failing, not the market.
            usable = dte > 0 and iv > 1.0
            if usable:
                buckets[min(dte, 9)].append((ratio, ratio / adj if adj else None))
            print(f"  {str(d):<12}{str(exp):<12}{dte:>5}{trd:>5}{spot:>10,.1f}"
                  f"{vix:>8.2f}{iv:>9.2f}{ratio:>9.4f}"
                  f"{(ratio / adj if adj else 0):>10.4f}"
                  f"{len(chain):>9}{'' if usable else '   no time value left'}")

    if not buckets:
        print("\n  nothing priced")
        return 1

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        return (xs[len(xs) // 2] if len(xs) % 2
                else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)

    print("\n  BY DAYS TO EXPIRY  (expiry-day closes excluded: no time value)\n")
    print(f"  {'dte':>5}{'n':>5}{'raw IV/VIX':>13}{'trading-day adj':>18}")
    print("  " + "-" * 43)
    adjs = []
    for k in sorted(buckets):
        raw = med([a for a, _b in buckets[k]])
        adj = med([b for _a, b in buckets[k]])
        adjs += [b for _a, b in buckets[k] if b is not None]
        label = f"{k}" if k < 9 else "9+"
        print(f"  {label:>5}{len(buckets[k]):>5}{raw:>13.4f}{adj:>18.4f}")

    print("\n  VERDICT\n")
    overall = med(adjs)
    if overall is None:
        print("    nothing usable")
        return 1
    print(f"    trading-day adjusted IV/VIX, all horizons: {overall:.4f}"
          f"   (n={len(adjs)})")
    spread = max(adjs) - min(adjs)
    print(f"    range {min(adjs):.4f} to {max(adjs):.4f}   spread {spread:.4f}")
    print()
    if overall > 1.03:
        print("    ATM IV runs ABOVE India VIX once the weekend is taken out.")
        print("    The dashboard's direction is right: a thirty-day VIX")
        print("    UNDERSTATES near-dated volatility. Note the raw column")
        print("    says the opposite -- that gap is the calendar-day bug, not")
        print("    the market, and it is large enough to invert the sign.")
    elif overall < 0.97:
        print("    ATM IV runs BELOW VIX even adjusted. That is the")
        print("    smile-weighting methodology gap, and the dashboard's stated")
        print("    reason for the 0-DTE failure does not hold.")
    else:
        print("    ATM IV and VIX agree once the weekend is taken out.")
        print("    Neither explanation is supported; the 0-DTE failure needs")
        print("    another cause.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

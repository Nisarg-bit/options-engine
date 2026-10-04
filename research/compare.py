"""Phase 2.3 -- the same entry rule, seven different structures, 26 months.

The question this exists to answer: at the moments the credit/sigma filter
says to sell, does a DEFINED-risk structure keep enough of the naked
straddle's edge to be worth its capped tail?

That matters because the tail is the one thing the backtest cannot measure.
July 2024 - August 2026 contained no gap-down of the kind that ruins a short
straddle. Every average below describes a market that behaved. A condor's
loss is capped by construction, so it does not depend on the sample having
been kind.

Mechanics, matching the workbook's own convention:
  signal day D   strikes chosen from D's close (spot, VIX, chain)
  entry day D+1  filled at that morning's OPEN
  expiry         cash-settled at intrinsic against the final settlement price

Usage:  python compare.py 2024-07-08 2026-08-28 [credit_sigma_threshold]
"""

import sys
from datetime import date

import pandas as pd

import backtest as bt
import pricing as P
import strategies as S
import vix as vixmod

MIN_DTE, MAX_DTE = 1, 12
LOTS = 1
WING_WIDTH = 200.0
SIGMA_MULT = 1.5
MARGIN_PER_LOT = 120000.0

MIN_CREDIT_PTS = 5.0
MIN_CREDIT_FRACTION = 0.10

# For DEFINED-risk structures the margin is exact: max loss IS the margin,
# and both it and the P&L scale with contract size, so return-on-margin is
# size-invariant.
#
# For UNDEFINED risk there is no such identity. The workbook uses a flat
# Rs 120,000 per lot doubled -- a constant, while its P&L scales with lot
# size, which silently flattered the straddle in the lot-75 era and punished
# it in the lot-25 era. Approximating SPAN+exposure as a share of notional
# at least scales with both contract size and index level, so the two
# structures can be compared on the same footing.
#
# 10% of notional is a reasonable stand-in for a short NIFTY straddle with
# the leg offset SPAN grants. The exact figure is available from Kite's
# basket-margin endpoint; worth wiring in before any of this becomes a
# position-sizing decision.
MARGIN_PCT_NOTIONAL = 0.10

# MEASURED half-spreads, in points, by |strike/spot - 1|, from five sessions
# of the collector's own level-1 quotes at the open -- which is when this
# backtest enters. Replaces the flat 1.0 point per leg that every earlier run
# charged, and which turned out to be 10-17x too pessimistic.
#
# Two caveats that belong with the numbers, not in a footnote:
#   * level 1 only. Where the touch is shallower than your order, the true
#     cost is higher than this by an amount the data cannot show.
#   * five calm sessions, VIX ~10.7. Spreads widen in stress, which is
#     exactly when you would want out.
MEASURED_HALF_SPREAD = [
    (0.0025, 0.11),
    (0.0100, 0.12),
    (0.0200, 0.16),
    (0.0400, 0.33),
    (9.9999, 0.96),
]


def half_spread(strike, spot):
    """Points to cross, one leg, at that strike's moneyness."""
    m = abs(strike / spot - 1.0) if spot else 0.0
    for hi, cost in MEASURED_HALF_SPREAD:
        if m < hi:
            return cost
    return MEASURED_HALF_SPREAD[-1][1]

COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there

ORDER = ["short_straddle", "intraday_strangle", "expiry_strangle",
         "maxev_strangle", "iron_condor", "iron_butterfly", "iron_condor_wide"]
DEFINED = {"iron_condor", "iron_butterfly", "iron_condor_wide"}


# ---------------------------------------------------------------- helpers

def chain_rows(day_df, expiry):
    """Signal-day closing chain for one expiry: strike -> CE/PE close."""
    ch = day_df[day_df["XpryDt"] == expiry]
    ce = ch[ch["OptnTp"] == "CE"].set_index("StrkPric")["ClsPric"]
    pe = ch[ch["OptnTp"] == "PE"].set_index("StrkPric")["ClsPric"]
    common = ce.index.intersection(pe.index)
    return [{"strike": float(k), "ce": float(ce[k]), "pe": float(pe[k])}
            for k in sorted(common)]


def open_price(day_df, expiry, strike, typ):
    """Entry-day open for one leg. None when the contract did not trade."""
    m = day_df[(day_df["XpryDt"] == expiry) & (day_df["StrkPric"] == strike)
               & (day_df["OptnTp"] == typ)]
    if m.empty:
        return None
    v = float(m["OpnPric"].iloc[0])
    return v if v > 0 else None


def intrinsic(final_spot, strike, typ):
    return (max(0.0, final_spot - strike) if typ == "CE"
            else max(0.0, strike - final_spot))


def episodes(rows):
    """Non-overlapping trades -- the real sample size."""
    if not rows:
        return 0
    n, busy = 0, None
    for entry, exp in sorted((r["entry_day"], r["expiry"]) for r in rows):
        if busy is None or entry > busy:
            n += 1
            busy = exp
    return n


# ------------------------------------------------------------- one session

def run_day(signal_day, entry_day, by_day, vix, threshold):
    """Build every structure for one session. Returns a list of trade dicts."""
    sig, ent = by_day[signal_day], by_day[entry_day]

    v = vix.get(signal_day)
    if v is None or v != v:
        return []
    spot = sig["UndrlygPric"].dropna()
    if spot.empty:
        return []
    spot = float(spot.mode().iloc[0])

    exps = sorted(e for e in ent["XpryDt"].unique()
                  if MIN_DTE <= (e - entry_day).days <= MAX_DTE)
    if not exps:
        return []
    expiry = exps[0]
    if expiry not in set(by_day):
        return []

    rows = chain_rows(sig, expiry)
    if len(rows) < 20:
        return []

    lot_ser = ent[ent["XpryDt"] == expiry]["NewBrdLotQty"]
    if lot_ser.empty:
        return []
    lot = int(lot_ser.mode().iloc[0])

    dte = max((expiry - entry_day).days, 1)
    built = S.build_all(rows, spot, v / 100.0, dte,
                        step=50.0, lot_size=lot, lots=LOTS,
                        wing_width=WING_WIDTH, sigma_mult=SIGMA_MULT,
                        margin_per_lot=MARGIN_PER_LOT, costs=COSTS)

    # entry filter, measured on the ATM straddle exactly as wiki_gate does
    sigma = P.Volatility(spot, v / 100.0, dte).expiry_stdev
    c_over_sigma = built["short_straddle"].credit_pts / sigma
    if c_over_sigma < threshold:
        return []

    xrows = by_day[expiry]
    xr = xrows[xrows["XpryDt"] == expiry]["UndrlygPric"].dropna()
    if xr.empty:
        return []
    final_spot = float(xr.mode().iloc[0])

    out = []
    for name in ORDER:
        s = built[name]
        legs = [(s.ce_strike, "CE", +1), (s.pe_strike, "PE", +1)]
        if s.defined_risk:
            legs += [(s.long_ce_strike, "CE", -1), (s.long_pe_strike, "PE", -1)]

        # Every leg must have actually traded at the open. Far-OTM wings often
        # have not -- so condors are simply not enterable on some days, and the
        # trade count per structure is itself a finding.
        fills = {}
        ok = True
        for k, typ, _ in legs:
            px = open_price(ent, expiry, k, typ)
            if px is None:
                ok = False
                break
            fills[(k, typ)] = px
        if not ok:
            continue

        credit = sum(sign * fills[(k, typ)] for k, typ, sign in legs)

        # Charge each leg its own measured half-spread rather than a flat
        # guess. A condor's wings sit further out than its shorts and cost
        # more to cross; a flat rate hid that in both directions.
        slip_pts = sum(half_spread(k, spot) for k, _typ, _sign in legs)
        net_credit = credit - slip_pts

        # A credit smaller than the cost of getting in and out is a
        # guaranteed loss with no upside.
        if net_credit < MIN_CREDIT_PTS:
            continue
        if s.defined_risk and net_credit < MIN_CREDIT_FRACTION * WING_WIDTH:
            continue

        settle = sum(sign * intrinsic(final_spot, k, typ) for k, typ, sign in legs)
        gross = (net_credit - settle) * lot * LOTS
        fees = P.transaction_costs(net_credit, lot, LOTS, s.defined_risk, COSTS)
        pnl = gross - fees

        capped = (WING_WIDTH - credit) * lot * LOTS if s.defined_risk else None
        margin = (s.margin if s.defined_risk
                  else spot * lot * LOTS * MARGIN_PCT_NOTIONAL)

        out.append({
            "structure": name, "signal_day": signal_day, "entry_day": entry_day,
            "expiry": expiry, "dte": dte, "spot": spot, "final_spot": final_spot,
            "c_over_sigma": c_over_sigma, "lot": lot, "legs": len(legs),
            "credit": round(credit, 2), "settle": round(settle, 2),
            "slippage_pts": round(slip_pts, 3), "margin": round(margin, 0),
            "max_loss_modelled": capped, "pnl": round(pnl, 2),
            # Size-neutral. Contract size ran 25 -> 75 -> 65 across this
            # sample; every rupee figure is part P&L and part lot size, and
            # comparing halves in rupees compares different contracts.
            "pnl_per_unit": round(pnl / (lot * LOTS), 4),
            "roi": round(pnl / margin, 6) if margin else 0.0,
        })
    return out


# ---------------------------------------------------------------- reporting

def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python compare.py 2024-07-08 2026-08-28 [threshold]")
    start, end = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 1.00

    raw = bt.load(start, end)
    by_day = {d: g for d, g in raw.groupby("date")}
    days = sorted(by_day)
    vix = vixmod.load().to_dict()

    print(f"\nentry filter: credit/sigma >= {threshold:.2f}   "
          f"lots={LOTS}  wings={WING_WIDTH:.0f} pts")
    print(f"slippage: MEASURED half-spreads by moneyness "
          f"({MEASURED_HALF_SPREAD[0][1]:.2f} pts ATM to "
          f"{MEASURED_HALF_SPREAD[-1][1]:.2f} deep), not the old flat 1.00/leg")
    print(f"exchange charge {COSTS.exchange_per_side:.4%} "
          f"(NSE current, not the workbook's stale 0.035%)\n")

    rows = []
    for i in range(len(days) - 1):
        rows.extend(run_day(days[i], days[i + 1], by_day, vix, threshold))
    if not rows:
        sys.exit("no trades passed the filter")

    t = pd.DataFrame(rows).sort_values("entry_day").reset_index(drop=True)
    t.to_csv("compare_trades.csv", index=False)

    entries = sorted(t["entry_day"].unique())
    cut = entries[len(entries) // 2]
    straddle_n = (t["structure"] == "short_straddle").sum()

    print("all P&L figures are POINTS PER UNIT of contract, not rupees --")
    print("contract size ran 25 -> 75 -> 65 and does not align with the halves\n")
    hdr = (f"{'structure':<20}{'n':>5}{'fill%':>7}{'ep':>5}{'avg':>8}{'med':>8}"
           f"{'win%':>7}{'worst':>9}{'IS':>8}{'OOS':>8}{'stable':>8}")
    print(hdr)
    print("-" * len(hdr))
    for name in ORDER:
        sub = t[t["structure"] == name]
        if sub.empty:
            print(f"{name:<20}    0   never fillable")
            continue
        p = sub["pnl_per_unit"]
        is_ = sub[sub["entry_day"] < cut]["pnl_per_unit"]
        oos = sub[sub["entry_day"] >= cut]["pnl_per_unit"]
        stable = ("yes" if len(is_) >= 8 and len(oos) >= 8
                  and is_.mean() > 0 and oos.mean() > 0 else "no")
        print(f"{name:<20}{len(sub):>5}{len(sub)/max(straddle_n,1)*100:>6.0f}%"
              f"{episodes(sub.to_dict('records')):>5}{p.mean():>8.1f}"
              f"{p.median():>8.1f}{(p>0).mean()*100:>7.1f}{p.min():>9.1f}"
              f"{is_.mean():>8.1f}{oos.mean():>8.1f}{stable:>8}")

    print("\nreturn on margin, per trade -- median, so one outlier cannot carry it")
    print(f"{'structure':<20}{'margin Rs':>11}{'median ROI':>12}"
          f"{'IS':>9}{'OOS':>9}{'risk':>11}")
    print("-" * 72)
    for name in ORDER:
        sub = t[t["structure"] == name]
        if sub.empty:
            continue
        r = sub["roi"] * 100
        r_is = sub[sub["entry_day"] < cut]["roi"] * 100
        r_oos = sub[sub["entry_day"] >= cut]["roi"] * 100
        risk = "DEFINED" if name in DEFINED else "unlimited"
        print(f"{name:<20}{sub['margin'].median():>11,.0f}{r.median():>11.2f}%"
              f"{r_is.median():>8.2f}%{r_oos.median():>8.2f}%{risk:>11}")

    print("""
HOW TO READ THIS
  fill%   share of the straddle's trade count this structure could actually
          be entered on. A condor needs four fills; far-OTM wings often did
          not trade at the open. A low fill% is a real constraint, not a
          rounding detail.
  worst   the largest single loss. For DEFINED-risk structures this is the
          number to look at -- it is capped by construction, so it does not
          depend on this sample having contained no crash. For the straddle
          it is capped by nothing.
  ep      non-overlapping trades. Under ~15, treat the row as anecdote.

Slippage is charged per leg, so a condor pays double the straddle's. On thin
wings that is still optimistic -- the real cost of a four-leg fill is wider
than one point a leg, and this backtest cannot see the spread.""")
    print("\nper-trade detail written to compare_trades.csv")


if __name__ == "__main__":
    main()

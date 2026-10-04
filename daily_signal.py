"""Daily signal, generated at 09:00 IST from the previous session's close.

Replaces the workbook's GENERATE DAILY SIGNAL macro. Same seven structures,
same gate, same cost model -- but the chain comes from bars this machine
collected itself rather than a scrape, and the prices come from the quoted
market rather than the last trade.

That last distinction matters more than it sounds. A far-OTM strike that did
not trade in the closing minute carries a stale `close`; the collector's own
data shows contracts printing a close of 745.15 while the market was 3.30
bid / 94.55 offered. Pricing a structure off `close` on strikes like that
produces confident nonsense. So every leg is priced at the MID of a quote
whose spread is sane, and strikes that fail that test are dropped and counted
rather than guessed at.

The spot comes from the collector's own INDEX bars, not from a quote. This
job runs at 09:00 IST -- the first second of the pre-open call auction, when
the index is computed from a mostly empty basket -- and four of the first six
signals were built on a number returned at that moment. The previous
session's close is on disk, exact, and settled fifteen hours earlier. As a
result this job now makes no network call at all: it reads bars, and that is
the whole of its input.

Usage:
    python signal.py            build and send
    python signal.py --dry      build and print, send nothing
"""

import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import journal
import market
import models as M
import notify
import pricing as P
import strategies as S

IST = ZoneInfo("Asia/Kolkata")

UNDERLYING   = "NIFTY"
STRIKE_STEP  = 50.0
WING_WIDTH   = 200.0
SIGMA_MULT   = 1.5
MARGIN_LOT   = 120000.0
DEFAULT_LOT  = 65

MIN_DTE, MAX_DTE = 1, 12

# The index instruments the collector already stores under underlying=INDEX.
# Reading spot from here rather than asking for a quote is the fix for the
# 09:00 pre-open problem: the previous session's close was flushed to disk
# fifteen hours before this job runs, it is exact, and it cannot be affected
# by what the exchange is or is not doing at the moment we ask.
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969

# The chain's put-call parity forward and the index must agree to within
# carry. Across the nine collected sessions the observed basis ran from
# -29.9 to +98.4 points, mean +39, sd 46 -- so 150 never fires on a normal
# day while catching anything of the size that corrupted the first six
# signals, where spot and the chain were up to 540 points apart.
BASIS_MAX = 150.0

# Entry gate. 0.65 is the workbook's Daily_Signal!H17 and two years of
# bhavcopy said to leave it alone: out of sample the curve is flat from
# credit/sigma 0.90 upward, so tightening buys nothing and costs sample.
POP_MIN = 0.65

# A quote is usable when both sides are live and the spread is not absurd.
# Relative alone is too harsh on cheap options (a 1-point spread on a 3-point
# option is 33% and perfectly normal); absolute alone is too loose on
# expensive ones. Take the more generous of the two.
SPREAD_ABS = 2.0
SPREAD_REL = 0.25

COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there


# ------------------------------------------------------------------ inputs

def previous_session():
    """The most recent date we actually collected, before today.

    Driven by what is on disk rather than a calendar, so a holiday or a
    missed session self-corrects instead of pointing at an empty folder.
    """
    days = []
    for p in glob.glob("data/bars_1m/date=*"):
        try:
            days.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    today = datetime.now(timezone.utc).date()
    past = sorted(d for d in days if d < today)
    return past[-1] if past else None


def closing_bars(session_day):
    """Last bar per instrument for one session."""
    files = glob.glob(f"data/bars_1m/date={session_day}/"
                      f"underlying={UNDERLYING}/*.parquet")
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.sort_values("ts").groupby("instrument_token", as_index=False).last()
    return df


def lot_size(session_day):
    """From the instrument master, falling back to the current NIFTY lot."""
    for d in (session_day, datetime.now(timezone.utc).date()):
        path = f"data/instruments/{d}.json"
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
            rows = data if isinstance(data, list) else data.get("instruments", [])
            for r in rows:
                if r.get("name") == UNDERLYING or r.get("underlying") == UNDERLYING:
                    v = r.get("lot_size") or r.get("lotsize")
                    if v:
                        return int(v), f"instrument master {d}"
        except Exception:
            pass
    return DEFAULT_LOT, "fallback default"


def index_close(session_day, token):
    """Closing value of one index instrument, from our own collected bars.

    This replaces `kite.ltp()`. The job runs at 09:00 IST, which is the first
    second of the pre-open call auction -- the index is then computed from a
    mostly empty basket and the number that comes back is real, live, and
    meaningless. Four of the first six signals were built on one.

    The previous session's close has none of that fragility. It is a settled
    number, already on disk, and it is what a signal built from the previous
    session's chain should be measured against anyway.
    """
    files = sorted(glob.glob(f"data/bars_1m/date={session_day}/"
                             f"underlying=INDEX/*.parquet"))
    if not files:
        return None
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(
                f, columns=["ts", "instrument_token", "close"]))
        except (OSError, ValueError, KeyError):
            continue
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df[df["instrument_token"] == token]
    if df.empty:
        return None
    return float(df.sort_values("ts")["close"].iloc[-1])


def quote_price(row):
    """Mid of a sane two-sided quote, else None. Returns (price, spread)."""
    bid, ask = float(row["bid"]), float(row["ask"])
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    mid = 0.5 * (bid + ask)
    spread = ask - bid
    if spread > max(SPREAD_ABS, SPREAD_REL * mid):
        return None, spread
    return mid, spread


def build_chain(bars, expiry):
    """[{strike, ce, pe}] priced at mid, plus a coverage report."""
    sub = bars[bars["expiry"] == expiry]
    legs, dropped = {}, {"one_sided": 0, "wide": 0}
    for _, row in sub.iterrows():
        px, spread = quote_price(row)
        if px is None:
            dropped["wide" if spread is not None else "one_sided"] += 1
            continue
        legs.setdefault(float(row["strike"]), {})[row["opt_type"]] = px

    chain = [{"strike": k, "ce": v["CE"], "pe": v["PE"]}
             for k, v in sorted(legs.items()) if "CE" in v and "PE" in v]
    return chain, dropped, len(sub)


# ----------------------------------------------------------------- report

def fmt_inr(x):
    return f"{x:,.0f}"


def build_message(ctx, structures, best):
    L = []
    L.append(f"<b>{UNDERLYING} DAILY SIGNAL</b>  {ctx['today']:%a %d %b}")
    L.append(f"from {ctx['session']:%d %b} close &middot; "
             f"expiry {ctx['expiry']:%d %b} ({ctx['dte']}d)")
    L.append("")
    L.append(f"spot {ctx['spot']:,.2f}   VIX {ctx['vix']:.2f}   "
             f"fwd {ctx['forward'] - ctx['spot']:+.0f}")
    L.append(f"ATM straddle {ctx['straddle']:.1f}   "
             f"fair {ctx['fair']:.1f}   ratio {ctx['ratio']:.3f}")
    L.append(f"credit/sigma {ctx['c_over_sigma']:.3f}   "
             f"(gate needs {ctx['cs_gate']:.3f})")
    L.append("")

    if best is None:
        L.append("<b>NO TRADE</b>")
        L.append(ctx["reason"])
    else:
        L.append(f"<b>{best.label}</b>")
        if best.defined_risk:
            L.append(f"sell {best.ce_strike:.0f} CE / {best.pe_strike:.0f} PE")
            L.append(f"buy  {best.long_ce_strike:.0f} CE / "
                     f"{best.long_pe_strike:.0f} PE")
            L.append(f"max loss Rs {fmt_inr(best.max_loss_pts * best.lot_size)}")
        else:
            L.append(f"sell {best.ce_strike:.0f} CE / {best.pe_strike:.0f} PE")
            L.append("risk UNDEFINED")
        L.append(f"credit {best.credit_pts:.1f} pts = Rs {fmt_inr(best.credit_rs)}")
        L.append(f"POP {best.pop*100:.1f}%   net EV Rs {fmt_inr(best.net_ev_rs)}")
        L.append(f"breakevens {best.lower_be:,.0f} - {best.upper_be:,.0f}")
        L.append(f"margin Rs {fmt_inr(best.margin)}")

    L.append("")
    L.append("<b>all structures</b> (net EV, Rs)")
    for s in sorted(structures.values(), key=lambda x: -x.net_ev_rs):
        mark = "*" if best is not None and s.name == best.name else " "
        L.append(f"{mark}{s.label:<20} {fmt_inr(s.net_ev_rs):>8}  "
                 f"POP {s.pop*100:4.1f}%")

    L.append("")
    L.append(f"chain {ctx['usable']} strikes usable, "
             f"{ctx['dropped_wide']} wide, {ctx['dropped_one']} one-sided")
    L.append(f"lot {ctx['lot']} ({ctx['lot_src']})")
    return "\n".join(L)


# -------------------------------------------------------------------- main

def main():
    dry = "--dry" in sys.argv
    today = datetime.now(timezone.utc).date()

    # A signal is a trade you could have entered. On 14 Sep 2026 this job
    # woke on a market holiday, found Friday's chain exactly where it had
    # left it, asked for a quote, got Friday's close back -- every number
    # internally consistent -- and logged seven structures into a session
    # that never happened. Nothing failed, which is why nothing caught it.
    #
    # The date is taken in IST deliberately: `today` above is the UTC date,
    # which is the same thing at 03:30 UTC but not at every hour a human
    # might run this by hand.
    ist_today = datetime.now(timezone.utc).astimezone(IST).date()
    if not market.is_trading_day(ist_today):
        return fail(f"{ist_today} is not an NSE trading day -- "
                    f"no signal, nothing journalled", dry)

    session_day = previous_session()
    if session_day is None:
        return fail("no collected sessions on disk", dry)

    bars = closing_bars(session_day)
    if bars is None or bars.empty:
        return fail(f"no {UNDERLYING} bars for {session_day}", dry)

    expiries = sorted({e for e in bars["expiry"].unique()
                       if MIN_DTE <= (e - today).days <= MAX_DTE})
    if not expiries:
        return fail(f"no expiry between {MIN_DTE} and {MAX_DTE} days out", dry)
    expiry = expiries[0]
    dte = (expiry - today).days

    chain, dropped, total = build_chain(bars, expiry)
    if len(chain) < 15:
        return fail(f"only {len(chain)} usable strikes for {expiry} "
                    f"({total} contracts, {dropped['wide']} wide quotes) "
                    f"-- refusing to signal on a chain this thin", dry)

    spot = index_close(session_day, NIFTY_TOKEN)
    vix = index_close(session_day, VIX_TOKEN)
    if spot is None or vix is None:
        missing = "NIFTY 50" if spot is None else "INDIA VIX"
        return fail(f"no collected {missing} close for {session_day} -- "
                    f"refusing to signal without a level", dry)

    # The chain and the index have to agree about where the market is. Both
    # are now read from the same session's own data, so a large disagreement
    # means one of them is wrong and there is no way to tell which. Picking a
    # strike anyway is exactly what produced six confident, unfollowable
    # signals -- so stop instead. A refused run is recoverable; a plausible
    # wrong one is not, because it never announces itself.
    forward = M.forward_from_parity(chain, spot)
    basis = forward - spot
    if abs(basis) > BASIS_MAX:
        return fail(f"chain and index disagree: parity forward "
                    f"{forward:,.1f} vs spot {spot:,.1f} "
                    f"({basis:+,.1f} pts, limit {BASIS_MAX:.0f}) -- no signal",
                    dry)

    lot, lot_src = lot_size(session_day)
    structures = S.build_all(chain, spot, vix / 100.0, dte,
                             step=STRIKE_STEP, lot_size=lot, lots=1,
                             wing_width=WING_WIDTH, sigma_mult=SIGMA_MULT,
                             margin_per_lot=MARGIN_LOT, costs=COSTS)

    vol = P.Volatility(spot, vix / 100.0, dte)
    straddle = structures["short_straddle"].credit_pts
    cs = straddle / vol.expiry_stdev if vol.expiry_stdev else 0.0

    # POP >= 0.65 on the ATM straddle is credit/sigma >= 0.9346 -- see the
    # algebra in wiki_gate.py. Expressed on that axis so one number governs.
    cs_gate = 0.9346

    best = S.best(structures)
    reason = ""
    if best is not None and cs < cs_gate:
        best, reason = None, (f"premium too thin: credit/sigma {cs:.3f} "
                              f"below {cs_gate:.3f}")
    elif best is None:
        reason = "nothing clears positive net EV once costs are paid"

    ctx = {
        "today": today, "session": session_day, "expiry": expiry, "dte": dte,
        "spot": spot, "vix": vix, "forward": forward,
        "sigma": vol.expiry_stdev, "straddle": straddle,
        "fair": P.fair_straddle(vol.expiry_stdev),
        "ratio": P.rich_cheap_ratio(straddle, vol.expiry_stdev),
        "c_over_sigma": cs, "cs_gate": cs_gate,
        "usable": len(chain), "dropped_wide": dropped["wide"],
        "dropped_one": dropped["one_sided"],
        "lot": lot, "lot_src": lot_src, "reason": reason,
    }

    msg = build_message(ctx, structures, best)
    print(msg.replace("<b>", "").replace("</b>", "").replace("&middot;", "-"))

    if not dry:
        # Journal FIRST. A Telegram outage must not cost us the record --
        # the message is a convenience, the row is the evidence.
        try:
            journal.log(ctx, structures, best)
        except Exception as e:
            print("journal failed:", e)
        notify.send(msg)

    # Feature store LAST and on its own: the journal row and the message have
    # already gone out, so nothing here can cost either. A --dry run computes
    # and prints the row without writing it. See features.py.
    try:
        import features   # imported here so even an import error stays contained
        features.record("daily", ctx, closing_bars=bars, write=not dry)
    except Exception as e:
        print("features failed:", e)

    # Shadow books (shadow.py): the live rule sized to the risk rules, and two
    # candidates, tracked forward. Paper only, and on its own like features.
    try:
        import shadow
        shadow.record(ctx, structures, write=not dry)
    except Exception as e:
        print("shadow failed:", e)


def fail(why, dry):
    msg = f"<b>{UNDERLYING} SIGNAL FAILED</b>\n{why}"
    print(msg.replace("<b>", "").replace("</b>", ""))
    if not dry:
        notify.send(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)

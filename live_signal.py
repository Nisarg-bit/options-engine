"""Live prediction, during the session, from live quotes.

WHAT THIS IS

The weekly job speaks once, at 09:00, from yesterday's close. This one speaks
all day. Every cycle it re-reads the market, re-prices every structure and
emits a fresh view -- spot, sigma, best structure, both POPs, EV, gate -- with
the probability computed FOR THAT MOMENT, not inherited from the morning.

The market moves; the prediction moves with it. That is the whole point.

WHERE THE DATA COMES FROM

Kite's quote() accepts up to 500 instruments in one call, and a NIFTY chain
for a single expiry is about 180. So one request gets the entire chain, live,
seconds old -- rather than reading collected parquet, which lags by up to the
collector's five-minute flush.

Asking Kite for a quote is safe HERE in a way it was not at 09:00. That
failure was specific to the pre-open auction, when the index is computed from
a nearly empty basket; the first print on 15 September read 853 points high.
Between 09:20 and 15:15 the index is computed from live trades. The loop
refuses to run outside that window, and the parity cross-check guards it
anyway.

SHARED SCORING, DELIBERATELY

The candidate construction and scoring are imported from intraday.py, the
module that was backtested over ten sessions and 211 recommendations. The
live loop is a DATA ADAPTER around validated code, not a second
implementation of it. If these diverged, the backtest would stop being
evidence about the thing that actually runs.

WHICH SIGMA

--newvol prices with the fitted calibration in data/vol_calibration.json;
without it the old flat-clock sigma is used. Both are available on purpose:
the old one is what every recorded row so far was priced with, and changing
it silently would make the log a mixture of two models with no way to tell
them apart. Every row now carries a sigma_model column saying which produced
it, so the two populations stay separable however the flag is set.

THE GATE HAS HYSTERESIS

A bare threshold sitting on the current market toggles. On the first live day
credit/sigma read 0.934 at 11:52 and 0.944 at 11:54 -- the gate closed, then
opened, on a move of one hundredth. Acted on, that is a round trip's costs
paid for noise.

So the gate is a state, not a comparison. It opens only after CONFIRM
consecutive cycles at or above GATE_OPEN, and once open it stays open until
CONFIRM consecutive cycles below GATE_CLOSE, which sits a band lower. Both
thresholds are quoted as ATM-straddle POP (0.650 to open, 0.620 to close) so
the band means something rather than being a round number in credit/sigma
units. The band width is a judgment call, not a fitted quantity, and it is
written here rather than buried so it can be argued with.

The raw comparison is still recorded in gate_raw. That is what lets us go
back later and measure how much chatter the hysteresis actually absorbed.

SHADOW MODE IS THE DEFAULT

Every row is written with tradeable=false and nothing is sent anywhere. The
predictions are being RECORDED, not offered, until the sigma rebuild has been
validated forward rather than only in replay. Recording them now means that
when it is, there are already weeks of live, forward-looking predictions to
check it against, which is far better evidence than anything replayed.

Pass --live to clear the flag. Do that when sigma is validated, not before.

Usage:
    python live_signal.py               one cycle, shadow, prints and records
    python live_signal.py --dry         one cycle, prints, records nothing
    python live_signal.py --newvol      price with the fitted calibration
    python live_signal.py --live        clears the shadow flag
"""

import csv
import json
import os
import sys
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import intraday as I
import market
import models as M
import vol_time as V
from position import CE, PE

IST = ZoneInfo("Asia/Kolkata")
OUT = "data/journal/live_signals.csv"
GATE_STATE = "data/journal/live_gate_state.json"

UNDERLYING = "NIFTY"
INDEX_SYMBOL = "NSE:NIFTY 50"
VIX_SYMBOL = "NSE:INDIA VIX"

FIRST = dtime(9, 20)          # after the opening auction has settled
LAST = dtime(15, 15)          # the intraday book's own flat-by time
FLAT_BY = dtime(15, 15)

STRIKE_WINDOW = 0.04          # keep strikes within +-4% of spot
MAX_INSTRUMENTS = 400         # quote() caps at 500; leave headroom
MIN_DTE, MAX_DTE = 0, 12
# The staleness guard lives in models.py so intraday.py can use the same
# one -- this module imports intraday, so the dependency cannot run the
# other way, and two copies of a guard is how the two drift apart.
# See models.spot_is_believable for what it checks and why.

# Gate hysteresis. Quoted as ATM-straddle POP, converted once at import so
# the numbers in the source mean what they say.
#   GATE_OPEN   POP 0.650 -> credit/sigma 0.9346   (== intraday.CS_GATE)
#   GATE_CLOSE  POP 0.620 -> credit/sigma 0.8779
GATE_OPEN = 0.9346
GATE_CLOSE = 0.8779
CONFIRM = 2                   # consecutive cycles before the state flips

FIELDS = [
    "ts", "underlying", "expiry", "dte", "spot", "vix", "forward", "basis",
    "fwd_dispersion", "basis_tol", "spot_suspect",
    "sigma_model", "sigma_hold", "sigma_expiry", "minutes_left",
    "structure", "ce_strike", "pe_strike", "credit_pts",
    "lower_be", "upper_be", "pop_terminal", "pop_no_touch", "pop_gap",
    "net_ev_pts", "net_ev_rs", "cs",
    "gate_raw", "gate_state", "confirm_n", "gate_passed", "lot",
    # The alternate model's own pick -- its own strikes. Two systems.
    "alt_model", "alt_sigma_hold", "alt_sigma_expiry",
    "alt_structure", "alt_ce_strike", "alt_pe_strike", "alt_credit_pts",
    "alt_cs", "alt_pop_terminal", "alt_pop_no_touch", "alt_net_ev_rs",
    "alt_gate_raw",
    # The acting model's OWN structure, re-priced under the alternate
    # sigma. Strikes held fixed, probability model varied. One trade, two
    # forecasts -- this is the like-for-like column set.
    "paired_cs", "paired_pop_terminal", "paired_pop_no_touch",
    "paired_net_ev_rs",
    "tradeable", "status",
]


# ------------------------------------------------------------------ helpers

def instruments_for(expiry, spot, day):
    """(symbol -> (strike, opt_type)) for the chain near the money."""
    for d in (day, date.today()):
        path = f"data/instruments/{d}.json"
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            continue
        rows = data if isinstance(data, list) else data.get("instruments", [])
        out, lot = {}, None
        lo, hi = spot * (1 - STRIKE_WINDOW), spot * (1 + STRIKE_WINDOW)
        for r in rows:
            if r.get("name") != UNDERLYING:
                continue
            it = str(r.get("instrument_type") or r.get("opt_type") or "")
            if it not in (CE, PE):
                continue
            if str(r.get("expiry"))[:10] != expiry.isoformat():
                continue
            try:
                k = float(r.get("strike") or 0)
            except (TypeError, ValueError):
                continue
            if not (lo <= k <= hi):
                continue
            sym = r.get("tradingsymbol")
            if not sym:
                continue
            out[f"NFO:{sym}"] = (k, it)
            lot = lot or r.get("lot_size") or r.get("lotsize")
        if out:
            return out, int(lot or I.DEFAULT_LOT), d
    return {}, I.DEFAULT_LOT, None


def expiries_from_master(day):
    for d in (day, date.today()):
        path = f"data/instruments/{d}.json"
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            continue
        rows = data if isinstance(data, list) else data.get("instruments", [])
        out = set()
        for r in rows:
            if r.get("name") != UNDERLYING:
                continue
            e = str(r.get("expiry"))[:10]
            try:
                out.add(date.fromisoformat(e))
            except ValueError:
                continue
        if out:
            return sorted(out)
    return []


def mid_from_quote(q):
    """Mid of the best two-sided quote, else last traded price."""
    depth = (q or {}).get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    try:
        b = float(buy[0]["price"])
        a = float(sell[0]["price"])
        if b > 0 and a > 0 and a >= b:
            return (a + b) / 2.0
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    try:
        v = float((q or {}).get("last_price"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def write_row(row, dry):
    """Append one row, refusing to append a new schema to an old file.

    DictWriter writes a header only when the file is new, so adding a column
    and appending would lay 31 values under a 27-column header -- every field
    after the insertion point silently shifted by one, and no error anywhere.
    That is the kind of corruption you find three weeks later while wondering
    why the gate column contains volatilities.

    So the existing header is checked. If it no longer matches, the old file
    is set aside under a dated name and a fresh one started. Nothing is lost
    and the two schemas stay separable.
    """
    if dry:
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.exists(OUT)
    if not new:
        try:
            with open(OUT, newline="") as fh:
                have = next(csv.reader(fh), [])
        except OSError:
            have = []
        if have != FIELDS:
            stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
            kept = f"{OUT.rsplit('.', 1)[0]}.pre-{stamp}.csv"
            os.replace(OUT, kept)
            print(f"  [schema changed -- previous log kept as {kept}]")
            new = True
    with open(OUT, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


# ------------------------------------------------------------- gate state

def load_gate(day, expiry):
    """The gate's state, or a fresh closed one.

    Keyed on (day, expiry): a new session starts closed, and so does a roll
    to a new expiry, because a confirmation earned on last week's contract
    says nothing about this week's.
    """
    fresh = {"day": day.isoformat(), "expiry": expiry.isoformat(),
             "state": "closed", "pending": None, "count": 0}
    try:
        s = json.load(open(GATE_STATE))
    except (OSError, ValueError):
        return fresh
    if (s.get("day") != fresh["day"]
            or s.get("expiry") != fresh["expiry"]):
        return fresh
    for k in fresh:
        s.setdefault(k, fresh[k])
    return s


def step_gate(state, cs, dry):
    """Advance the gate one cycle and return (open?, state).

    Open requires CONFIRM consecutive cycles at or above GATE_OPEN. Close
    requires CONFIRM consecutive cycles strictly below GATE_CLOSE. Anything
    between the thresholds, or any single cycle that breaks a run, clears
    the pending count -- so a flicker costs nothing and a real move still
    gets through in CONFIRM cycles.
    """
    want = None
    if state["state"] == "closed" and cs >= GATE_OPEN:
        want = "open"
    elif state["state"] == "open" and cs < GATE_CLOSE:
        want = "closed"

    if want is None:
        state["pending"], state["count"] = None, 0
    elif state["pending"] == want:
        state["count"] += 1
    else:
        state["pending"], state["count"] = want, 1

    if want is not None and state["count"] >= CONFIRM:
        state["state"] = want
        state["pending"], state["count"] = None, 0

    if not dry:
        os.makedirs(os.path.dirname(GATE_STATE), exist_ok=True)
        tmp = GATE_STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, GATE_STATE)
    return state["state"] == "open", state


def bail(why, row, dry, record=True, normal=False):
    """Stop, say why, and usually keep the row.

    A failed quote or a tripped guard IS a finding and belongs in the record.
    Firing outside market hours is not -- the timer is deliberately broader
    than the window so it never misses the edges, and logging every one of
    those would bury the interesting refusals.

    `normal` decides the EXIT CODE, which is a different question from
    whether to record. The timer fires from 03:00 to 09:55 UTC while the
    window is 03:50 to 09:45, so several firings a day land outside it by
    design. Those returned 1, systemd marked the unit failed, and
    options-live-signal.service sat red for days while working perfectly.

    A status light that is always on is a status light nobody reads. So an
    expected refusal exits 0 and only a real fault -- a dead session, a
    failed quote, an incoherent chain -- exits 1.
    """
    row["status"] = why
    if record:
        write_row(row, dry)
    print(f"{row['ts']}  NO PREDICTION -- {why}")
    return 0 if normal else 1


# --------------------------------------------------------------------- main

def main():
    dry = "--dry" in sys.argv
    shadow = "--live" not in sys.argv
    newvol = "--newvol" in sys.argv

    # The calibration is loaded whether or not --newvol was passed, because
    # BOTH models are priced every cycle. --newvol only decides which one
    # acts; the other is recorded beside it.
    cal = I.load_calibration()
    if newvol and cal is None:
        print(f"--newvol asked for, but {I.CALIBRATION} is missing or "
              f"unreadable -- using the old sigma", file=sys.stderr)
    acting = "fitted" if (newvol and cal is not None) else "flat"

    now = datetime.now(IST)
    row = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "underlying": UNDERLYING,
           "sigma_model": acting,
           "tradeable": "false" if shadow else "true"}

    if not market.is_trading_day(now.date()):
        return bail("not an NSE trading day", row, dry, record=False,
                    normal=True)
    if not (FIRST <= now.time() <= LAST):
        return bail(f"outside {FIRST}-{LAST} -- the pre-open auction and the "
                    f"closing minutes are not priced reliably",
                    row, dry, record=False, normal=True)

    import session as kite_session
    try:
        kite = kite_session.get_kite()
    except Exception as e:
        return bail(f"kite session failed: {type(e).__name__}", row, dry)

    # --- spot and VIX, live. Safe mid-session; see the module docstring.
    try:
        q = kite.ltp([INDEX_SYMBOL, VIX_SYMBOL])
        spot = float(q[INDEX_SYMBOL]["last_price"])
        vix = float(q[VIX_SYMBOL]["last_price"])
    except Exception as e:
        return bail(f"index quote failed: {type(e).__name__}", row, dry)
    if spot <= 0 or vix <= 0:
        return bail(f"nonsense index quote: spot {spot}, vix {vix}", row, dry)
    row.update(spot=round(spot, 2), vix=round(vix, 2))

    # --- pick the expiry
    today = now.date()
    exps = [e for e in expiries_from_master(today)
            if MIN_DTE <= (e - today).days <= MAX_DTE]
    if not exps:
        return bail("no expiry in range in the instrument master", row, dry)
    expiry = exps[0]
    dte = (expiry - today).days
    row.update(expiry=expiry.isoformat(), dte=dte)

    # --- the whole chain in one call
    syms, lot, master_day = instruments_for(expiry, spot, today)
    if not syms:
        return bail("no chain instruments found in the master", row, dry)
    keys = list(syms)[:MAX_INSTRUMENTS]
    try:
        quotes = kite.quote(keys)
    except Exception as e:
        return bail(f"chain quote failed ({len(keys)} instruments): "
                    f"{type(e).__name__}", row, dry)

    prices, chain = {}, {}
    for sym, (k, ot) in syms.items():
        px = mid_from_quote(quotes.get(sym))
        if px is None or px <= 0:
            continue
        prices[(k, ot)] = px
        chain.setdefault(k, {})[ot] = px
    rows = [{"strike": k, "ce": v[CE], "pe": v[PE]}
            for k, v in sorted(chain.items()) if CE in v and PE in v]
    row["lot"] = lot
    if len(rows) < 8:
        return bail(f"only {len(rows)} two-sided strikes live", row, dry)

    # --- two horizons. Risk to the exit; value to expiry.
    flat_dt = datetime.combine(today, FLAT_BY)
    hold_days = V.horizon_days(now.replace(tzinfo=None), flat_dt)
    exp_days = max(dte, V.horizon_days(now.replace(tzinfo=None),
                                       datetime.combine(today, dtime(15, 30))))
    if hold_days <= 0:
        return bail("no session left to hold", row, dry, normal=True)

    # --- do the index and the chain agree about what the underlying is?
    mins_to_close = V.minutes_between(now.time(), dtime(15, 30))
    years = max(0.0, dte + mins_to_close / V.SESSION_MINUTES) / 365.0
    forward, dispersion = M.forward_and_spread(rows, spot)

    # --- REFUSE only on faults we understand.
    #
    # Two different things were being tested here and only one of them is
    # understood well enough to act on.
    #
    # A chain that disagrees WITH ITSELF is broken data: parity is
    # model-free, so five near-the-money strikes implying five different
    # underlyings means the legs are not contemporaneous with each other and
    # nothing priced off them can be trusted. Refuse.
    #
    # A chain that agrees with itself but sits away from the index is NOT
    # understood. Over eleven sessions the gap ran ~50 points on every
    # non-expiry day and collapsed to ~5 on expiry day, at every DTE from 1
    # to 6 -- a constant offset in POINTS, not a rate. Explaining it as carry
    # needs 14% a year at 6 DTE and 87% at 1 DTE, which is not one rate and
    # therefore not carry. Refusing on it threw away 71% of all minutes.
    #
    # So it is RECORDED and not acted on. A guard built from three minutes of
    # one session does not get to veto eleven sessions of data on a quantity
    # nobody has explained yet.
    if forward is None:
        return bail("too few two-sided strikes to imply a forward", row, dry)
    tol = M.basis_tolerance(spot, years, dispersion)
    basis = forward - spot
    suspect = abs(basis) > tol
    row.update(forward=round(forward, 2), basis=round(basis, 1),
               fwd_dispersion=round(dispersion, 2), basis_tol=round(tol, 1),
               spot_suspect="true" if suspect else "false")
    if dispersion > M.CHAIN_INCOHERENT:
        return bail(f"the chain disagrees with itself: near strikes imply "
                    f"underlyings {dispersion:,.0f} pts apart -- nothing "
                    f"priced off it can be trusted", row, dry)

    # --- BOTH models, every cycle.
    #
    # The flag decides which one acts. It does not decide which one is
    # recorded -- both are, against the same chain at the same instant,
    # because the only thing that will ever settle which sigma is right is
    # forward data, and a cycle recorded under one model is a cycle of
    # evidence about the other permanently lost. Priced together they cost
    # one extra pass over ~180 quotes already in memory.
    def sigmas(model):
        if model == "flat":
            return (V.Volatility(spot, vix / 100.0, hold_days).stdev,
                    V.Volatility(spot, vix / 100.0, exp_days).stdev)
        # Holding horizon is flat by 15:15 and crosses no night, so only
        # k_session applies. The horizon to expiry crosses one gap per
        # trading day, each priced by its own class.
        return (cal.intraday(spot, vix / 100.0,
                             hold_days * V.SESSION_MINUTES),
                cal.sigma(spot, vix / 100.0,
                          lead_minutes=V.minutes_between(now.time(),
                                                         dtime(15, 30)),
                          day_classes=V.day_classes_between(today, expiry)))

    scored = {}
    for model in ("flat", "fitted"):
        if model == "fitted" and cal is None:
            continue
        sh, se = sigmas(model)
        if sh <= 0 or se <= 0:
            if model == acting:
                return bail(f"nonsense {model} sigma: hold {sh:.1f}, "
                            f"expiry {se:.1f}", row, dry)
            continue
        cs_ = [I.score_candidate(c, spot, sh, se, lot)
               for c in I.candidates(prices, spot, sh, I.TWO_LEG)]
        if not cs_:
            continue
        scored[model] = {"sigma_hold": sh, "sigma_expiry": se, "cands": cs_,
                         "pick": max(cs_, key=lambda c: c["net_ev_rs"])}

    if acting not in scored:
        return bail("no structure could be built from the live chain",
                    row, dry)

    primary = scored[acting]
    sigma_hold, sigma_exp = primary["sigma_hold"], primary["sigma_expiry"]
    cands, pick = primary["cands"], primary["pick"]
    row.update(sigma_hold=round(sigma_hold, 1),
               sigma_expiry=round(sigma_exp, 1),
               minutes_left=round(hold_days * V.SESSION_MINUTES))

    # The alternate model's own view, and -- separately -- the acting
    # model's EXACT structure re-priced under the alternate sigma.
    #
    # Those are two different questions and conflating them is what made the
    # 211-prediction backtest comparison unreadable. Strangle strikes are
    # placed at multiples of sigma_hold, so a narrower sigma picks different
    # strikes: alt_* is "what would the other model have traded", which is a
    # comparison of two SYSTEMS. paired_* holds the strikes fixed and varies
    # only the probability model, which is a comparison of two FORECASTS of
    # one trade. Only the second is like-for-like. For an ATM straddle they
    # coincide, because its strikes do not depend on sigma at all.
    other = next((m for m in scored if m != acting), None)
    if other:
        alt = scored[other]
        ap = alt["pick"]
        # score_candidate mutates its argument, so the acting pick is copied
        # before being re-priced -- otherwise the re-score would overwrite
        # the numbers the gate just acted on.
        paired = I.score_candidate(dict(pick), spot, alt["sigma_hold"],
                                   alt["sigma_expiry"], lot)
        row.update(
            alt_model=other,
            alt_sigma_hold=round(alt["sigma_hold"], 1),
            alt_sigma_expiry=round(alt["sigma_expiry"], 1),
            alt_structure=ap["name"], alt_ce_strike=ap["ce"],
            alt_pe_strike=ap["pe"], alt_credit_pts=round(ap["credit"], 2),
            alt_cs=round(ap["cs"], 4),
            alt_pop_terminal=round(ap["pop_terminal"], 4),
            alt_pop_no_touch=round(ap["pop_no_touch"], 4),
            alt_net_ev_rs=round(ap["net_ev_rs"], 0),
            alt_gate_raw=("true" if (ap["cs"] >= GATE_OPEN
                                     and ap["net_ev_rs"] > 0) else "false"),
            paired_cs=round(paired["cs"], 4),
            paired_pop_terminal=round(paired["pop_terminal"], 4),
            paired_pop_no_touch=round(paired["pop_no_touch"], 4),
            paired_net_ev_rs=round(paired["net_ev_rs"], 0),
        )
    else:
        alt = paired = None

    # The raw comparison -- what the old code would have said -- then the
    # state machine. EV is not part of the hysteresis: a negative-EV trade is
    # refused outright rather than being confirmed away over two cycles.
    raw = pick["cs"] >= GATE_OPEN and pick["net_ev_rs"] > 0
    gate_open, gstate = step_gate(load_gate(today, expiry), pick["cs"], dry)
    passed = gate_open and pick["net_ev_rs"] > 0

    row.update(gate_raw="true" if raw else "false",
               gate_state=gstate["state"],
               confirm_n=(f"{gstate['pending']}:{gstate['count']}"
                          if gstate["pending"] else ""))
    row.update(structure=pick["name"], ce_strike=pick["ce"],
               pe_strike=pick["pe"], credit_pts=round(pick["credit"], 2),
               lower_be=round(pick["lower"], 1), upper_be=round(pick["upper"], 1),
               pop_terminal=round(pick["pop_terminal"], 4),
               pop_no_touch=round(pick["pop_no_touch"], 4),
               pop_gap=round(pick["pop_gap"], 4),
               net_ev_pts=round(pick["net_ev_pts"], 2),
               net_ev_rs=round(pick["net_ev_rs"], 0),
               cs=round(pick["cs"], 4),
               gate_passed="true" if passed else "false",
               status="ok")
    write_row(row, dry)

    mark = "SHADOW" if shadow else "LIVE"
    print(f"{row['ts']}  {mark}   spot {spot:,.2f}  VIX {vix:.2f}  "
          f"fwd {basis:+.0f}")
    print(f"  {int(row['minutes_left'])} min to {FLAT_BY}   "
          f"sigma hold {sigma_hold:.1f}  to expiry {sigma_exp:.1f}  "
          f"[{row['sigma_model']}]   (expiry {expiry}, {dte}d)")
    print(f"  {pick['name']}  sell {pick['ce']:.0f} CE / {pick['pe']:.0f} PE  "
          f"credit {pick['credit']:.1f}")
    print(f"  zone {pick['lower']:,.0f} - {pick['upper']:,.0f}")
    print(f"  POP terminal {pick['pop_terminal']:.1%}   "
          f"no-touch {pick['pop_no_touch']:.1%}   "
          f"gap {pick['pop_gap']:.1%}")
    print(f"  credit/sigma {pick['cs']:.3f} "
          f"(open {GATE_OPEN:.3f} / close {GATE_CLOSE:.3f})   "
          f"net EV Rs {pick['net_ev_rs']:,.0f}")
    pend = (f"   [{gstate['count']}/{CONFIRM} toward {gstate['pending']}]"
            if gstate["pending"] else "")
    disagree = ""
    if raw != passed:
        disagree = (f"   (bare threshold says {'take' if raw else 'skip'} "
                    f"-- hysteresis absorbed it)")
    if row.get("spot_suspect") == "true":
        print(f"  [spot suspect: chain implies {forward:,.2f}, index says "
              f"{spot:,.2f}, gap {basis:+,.1f} vs tol {tol:,.1f} "
              f"-- recorded, not refused]")
    print(f"  gate {gstate['state'].upper()}{pend}{disagree}")
    print(f"  -> {'GATE OPEN' if passed else 'no trade'}"
          f"{'   [shadow: recorded, not offered]' if shadow else ''}")
    for c in sorted(cands, key=lambda x: -x["net_ev_rs"]):
        if c is pick:
            continue
        print(f"     {c['name']:<14} EV {c['net_ev_rs']:>8,.0f}  "
              f"POP {c['pop_terminal']:.1%}  c/s {c['cs']:.3f}")

    if alt is not None:
        print(f"  --- also recorded: {row['alt_model']} sigma "
              f"(hold {alt['sigma_hold']:.1f} / exp "
              f"{alt['sigma_expiry']:.1f}) ---")
        print(f"     same trade, re-priced:  POP "
              f"{paired['pop_terminal']:.1%}  no-touch "
              f"{paired['pop_no_touch']:.1%}  c/s {paired['cs']:.3f}  "
              f"EV Rs {paired['net_ev_rs']:,.0f}")
        ap = alt["pick"]
        print(f"     its own pick:           {ap['name']} "
              f"{ap['ce']:.0f}/{ap['pe']:.0f}  POP "
              f"{ap['pop_terminal']:.1%}  c/s {ap['cs']:.3f}  "
              f"EV Rs {ap['net_ev_rs']:,.0f}"
              f"{'  [would gate OPEN]' if row['alt_gate_raw'] == 'true' else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

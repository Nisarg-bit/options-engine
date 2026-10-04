"""The intraday book, built as a backtest before it is ever run live.

WHY THIS EXISTS

The weekly book has produced two independent expiries in ten sessions. The
twenty-observation floor is therefore about five months away, and until it
arrives nothing about the model can be confirmed or refuted. That is not a
patience problem, it is a design problem: a strategy that generates one
testable observation a week cannot be validated inside a student project.

Twenty-five decision points a session changes the arithmetic. The same ten
sessions already on disk yield a few hundred scored recommendations, each
with a prediction made before the outcome and an outcome measured from real
minute bars.

TWO LEDGERS, AS AGREED

  advisory   every recommendation the system issues, scored. Measures the
             MODEL. A recommendation at 11:00 is scored even though the book
             was already in a position and could not have taken it.

  book       what a single-position account actually did. Measures the
             ACCOUNT. One position at a time, entered at the first
             qualifying signal, exited by rule.

They diverge deliberately. Collapsing them is how a record starts flattering
itself: the model looks good because you only counted the trades you took,
or the account looks good because you counted trades it never held.

WHAT IS DIFFERENT FROM THE DAILY BOOK

  sigma      from vol_time on a trading-minute clock. `pricing.Volatility`
             floors the horizon at one day, which makes every intraday
             reading identical and is why this could not be built before.

  POP        reported twice -- terminal (inside the zone at 15:15) and
             no-touch (never reached a breakeven all session). The gap
             between them is the drawdown the position has to survive, and
             it is invisible in terminal POP alone.

  structures two-leg only by default. Four legs pay four lots of brokerage
             twice on a 375-minute horizon, and the daily book's EV filter
             already rejected seven of nine sessions on costs alone. The
             set is a flag, not a constant -- Nisarg has not settled it yet.

  costs      a full round trip. The weekly book sells and lets expiry
             settle it; this one buys back, every time.

Usage:
    python intraday.py                      all sessions, defaults
    python intraday.py --every 15           decision cadence in minutes
    python intraday.py --capture 0.85       decay exit threshold
    python intraday.py --gate 0.9346        credit/sigma entry gate
    python intraday.py --structures two-leg straddle + strangles (default;
                                            four-leg not yet ported)
    python intraday.py --session 2026-09-08 one session
    python intraday.py --calibrate          score every candidate, trade none
    python intraday.py --newvol             price with the fitted calibration
"""

import glob
import json
import os
import sys
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

import models as M
import position as X
import pricing as P
import vol_time as V
from position import CE, PE, ExitRule, Leg, Position

BARS = "data/bars_1m"
UNDERLYING = "NIFTY"
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969

FIRST_ENTRY = dtime(9, 20)      # after the opening auction has settled
LAST_ENTRY = dtime(14, 45)      # nothing opened in the last half hour
FLAT_BY = dtime(15, 15)         # square off, no exceptions

STRIKE_STEP = 50.0
DEFAULT_LOT = 75
CS_GATE = 0.9346                # POP >= 0.65 on the ATM straddle
COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there

TWO_LEG = ("straddle", "strangle_1.0", "strangle_1.5")

CALIBRATION = "data/vol_calibration.json"


def load_calibration():
    """The fitted model, or None. Absent is not an error -- the old sigma
    stays the default until someone asks for the new one."""
    try:
        return V.Calibration.load(CALIBRATION)
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------- data

def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return out


def _norm_ts(df):
    ts = pd.to_datetime(df["ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return ts.dt.floor("min")


def index_frames(day):
    """{token: {minute: value}} for NIFTY 50 and INDIA VIX."""
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", "underlying=INDEX", "*.parquet")))
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(
                f, columns=["ts", "instrument_token", "close"]))
        except (OSError, ValueError, KeyError):
            continue
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = _norm_ts(df)
    out = {}
    for tok in (NIFTY_TOKEN, VIX_TOKEN):
        sub = df[df["instrument_token"] == tok].sort_values("ts")
        out[tok] = dict(zip(sub["ts"], sub["close"].astype(float)))
    return out


def mid(bid, ask, close):
    try:
        b, a = float(bid), float(ask)
        if b > 0 and a > 0 and a >= b:
            return (a + b) / 2.0
    except (TypeError, ValueError):
        pass
    try:
        return float(close)
    except (TypeError, ValueError):
        return None


def option_book(day, expiry):
    """{minute: {(strike, opt_type): price}} for one expiry, one session."""
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={UNDERLYING}", "*.parquet")))
    cols = ["ts", "expiry", "strike", "opt_type", "close", "bid", "ask"]
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=cols)
        except (OSError, ValueError, KeyError):
            continue
        df = df[df["expiry"].astype(str).str.slice(0, 10) == expiry.isoformat()]
        if not df.empty:
            frames.append(df)
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = _norm_ts(df)
    book = {}
    for t, px, k, o in zip(df["ts"],
                           [mid(b, a, c) for b, a, c in
                            zip(df["bid"], df["ask"], df["close"])],
                           df["strike"], df["opt_type"]):
        if px is None:
            continue
        book.setdefault(t.to_pydatetime(), {})[(float(k), str(o))] = px
    return book


def expiries_on(day):
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={UNDERLYING}", "*.parquet")))
    out = set()
    for f in files:
        try:
            out.update(pd.read_parquet(f, columns=["expiry"])["expiry"]
                       .astype(str).str.slice(0, 10).unique().tolist())
        except (OSError, ValueError, KeyError):
            continue
    return sorted(date.fromisoformat(e) for e in out)


def lot_for(day):
    for d in (day, date.today()):
        path = f"data/instruments/{d}.json"
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
            rows = data if isinstance(data, list) else data.get("instruments", [])
            for r in rows:
                if r.get("name") == UNDERLYING:
                    v = r.get("lot_size") or r.get("lotsize")
                    if v:
                        return int(v)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return DEFAULT_LOT


# --------------------------------------------------------------- structures

def nearest(strikes, target):
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def candidates(prices, spot, sigma, names):
    """Two-leg short-premium structures at this minute.

    Built here rather than through `strategies.build_all` because that
    routine constructs its own `pricing.Volatility`, whose one-day floor is
    exactly what makes an intraday horizon unrepresentable. Everything else
    -- the payoff maths, the cost model -- is the shared code.
    """
    strikes = sorted({k for k, _ in prices})
    if not strikes:
        return []
    atm = nearest(strikes, round(spot / STRIKE_STEP) * STRIKE_STEP)
    if atm is None:
        return []

    out = []
    for name in names:
        if name == "straddle":
            ce = pe = atm
        elif name.startswith("strangle_"):
            mult = float(name.split("_")[1])
            ce = nearest(strikes, atm + mult * sigma)
            pe = nearest(strikes, atm - mult * sigma)
            if ce is None or pe is None or ce <= pe:
                continue
        else:
            continue
        cp, pp = prices.get((ce, CE)), prices.get((pe, PE))
        if cp is None or pp is None or cp <= 0 or pp <= 0:
            continue
        out.append({"name": name, "ce": ce, "pe": pe,
                    "ce_px": cp, "pe_px": pp, "credit": cp + pp})
    return out


def score_candidate(c, spot, sigma_hold, sigma_expiry, lot, lots=1):
    """Prediction, before any outcome is known.

    TWO SIGMAS, because two different questions are being asked.

    sigma_hold -- the move between now and the 15:15 exit. This governs the
    RISK: where the index can get to while you hold, whether it finishes
    inside the zone, whether it touches a breakeven on the way.

    sigma_expiry -- the move to actual expiry. This governs the VALUE. At
    15:15 the position is bought back, not settled, so what you pay is
    intrinsic PLUS the time value still left. The first version of this
    function priced the buy-back with sigma_hold, which is the same as
    pretending the contract expired at 15:15, and it overstated EV by an
    order of magnitude.

    The correction carries a result worth stating plainly. Under Bachelier,
    integrating the buy-back value over the holding-period distribution
    gives sqrt(sigma_hold^2 + sigma_remaining^2) -- which is exactly
    sigma_expiry. So the EXPECTED profit of an intraday trade equals that of
    holding to expiry. Exiting early creates no edge under a driftless
    model; it changes the variance and it buys the optionality of the decay
    exit, and that is all. Any real intraday edge has to come from something
    this model does not contain.

    The entry gate therefore uses sigma_expiry -- whether premium is rich is
    a question about the contract, not about how long you intend to hold it.
    """
    credit = c["credit"]
    lower = c["pe"] - credit
    upper = c["ce"] + credit

    both = V.both(spot, lower, upper, sigma_hold)          # risk
    payoff = (P.expected_call(spot, c["ce"], sigma_expiry)  # value
              + P.expected_put(spot, c["pe"], sigma_expiry))
    net_pts = credit - payoff
    fees = P.transaction_costs(credit, lot, lots, defined_risk=False,
                               costs=COSTS)
    c.update({"lower": lower, "upper": upper,
              "pop_terminal": both["terminal"], "pop_no_touch": both["no_touch"],
              "pop_gap": both["gap"], "exp_payoff": payoff,
              "sigma_hold": sigma_hold, "sigma_expiry": sigma_expiry,
              "net_ev_pts": net_pts,
              "net_ev_rs": net_pts * lot * lots - fees,
              "fees_rs": fees,
              "cs": credit / sigma_expiry if sigma_expiry > 0 else 0.0})
    return c


def legs_of(c):
    return (Leg(c["ce"], CE, -1, c["ce_px"]), Leg(c["pe"], PE, -1, c["pe_px"]))


# ------------------------------------------------------------------ session

def run_session(day, every, capture, gate, names, calibrate=False,
                cal=None, guard=False):
    exps = [e for e in expiries_on(day) if e >= day]
    if not exps:
        return None
    expiry = exps[0]
    book = option_book(day, expiry)
    if not book:
        return None
    idx = index_frames(day)
    nifty, vix_by_min = idx.get(NIFTY_TOKEN, {}), idx.get(VIX_TOKEN, {})
    if not nifty or not vix_by_min:
        return None
    lot = lot_for(day)
    minutes = sorted(book)

    flat_dt = datetime.combine(day, FLAT_BY)
    rule = ExitRule(capture=capture, hard_exit_dte=-1, flat_by=FLAT_BY)

    advisory, trades = [], []
    open_pos = None
    refused = 0

    decision_times = [m for m in minutes
                      if FIRST_ENTRY <= m.time() <= LAST_ENTRY
                      and (m.hour * 60 + m.minute) % every == 0]

    for t in minutes:
        prices = book[t]

        # --- keep the book's open position marked every single minute
        if open_pos is not None:
            pos, meta = open_pos
            try:
                pos.step(t, prices, 0)
            except KeyError:
                pass                      # a leg went unquoted; hold, do not guess
            if not pos.is_open:
                trades.append((pos, meta))
                open_pos = None

        if t not in decision_times:
            continue

        spot, vix = nifty.get(t), vix_by_min.get(t)
        if spot is None or vix is None:
            continue

        # --- is this minute's index print contemporaneous with the chain?
        #
        # The index bar can lag the options by a minute near the close and
        # repeat its last value while it lags. A stale spot mislabels
        # intrinsic as time value, which inflates apparent credit -- so every
        # prediction built on one is fiction. Counted either way so the cost
        # of the guard is visible rather than silent.
        if guard:
            rows_for_parity = []
            strikes_here = {k for k, _ in prices}
            for k in sorted(strikes_here):
                c, p = prices.get((k, CE)), prices.get((k, PE))
                if c and p:
                    rows_for_parity.append({"strike": k, "ce": c, "pe": p})
            ok, _f, _d, _tol, _why = M.spot_is_believable(
                rows_for_parity, spot,
                max(0.0, (expiry - day).days
                    + V.minutes_between(t.time(), dtime(15, 30))
                    / V.SESSION_MINUTES) / 365.0)
            if not ok:
                refused += 1
                continue

        hold_days = V.horizon_days(t, flat_dt)
        if hold_days <= 0:
            continue
        # Calendar days to expiry, as the workbook counts them -- except on
        # expiry day itself, where the honest horizon is the fraction of the
        # session still to run rather than zero.
        exp_days = max((expiry - day).days,
                       V.horizon_days(t, datetime.combine(day, dtime(15, 30))))

        if cal is None:
            sigma_hold = V.Volatility(spot, vix / 100.0, hold_days).stdev
            sigma_exp = V.Volatility(spot, vix / 100.0, exp_days).stdev
        else:
            # The holding horizon crosses no night at all -- the position is
            # flat by 15:15 -- so only k_session applies. The horizon to
            # expiry crosses one gap per trading day, each priced by its own
            # class.
            mins_hold = hold_days * V.SESSION_MINUTES
            lead = V.minutes_between(t.time(), dtime(15, 30))
            classes = V.day_classes_between(day, expiry)
            sigma_hold = cal.intraday(spot, vix / 100.0, mins_hold)
            sigma_exp = cal.sigma(spot, vix / 100.0, lead_minutes=lead,
                                  day_classes=classes)
        if sigma_hold <= 0 or sigma_exp <= 0:
            continue

        # Strike offsets are a risk choice, so they use the holding sigma.
        scored = [score_candidate(c, spot, sigma_hold, sigma_exp, lot)
                  for c in candidates(prices, spot, sigma_hold, names)]
        qualified = [c for c in scored
                     if c["cs"] >= gate and c["net_ev_rs"] > 0]

        # CALIBRATION MODE scores every candidate whether or not it is worth
        # trading. "Does the model predict?" and "should we take this?" are
        # different questions, and only the second needs the gate. Keeping
        # them joined meant a shut gate produced no calibration data at all,
        # which is exactly backwards: the sessions where nothing qualifies
        # are still sessions where POP can be checked against outcome.
        #
        # Calibration rows NEVER reach the book ledger. They are predictions
        # being marked, not trades being claimed.
        if calibrate and scored:
            pick = max(scored, key=lambda c: c["net_ev_rs"])
            legs = legs_of(pick)
            adv = Position(legs=legs, lot_size=lot, entry_ts=t, expiry=expiry,
                           rule=rule, label=pick["name"])
            advisory.append({"ts": t, "spot": spot, "sigma": sigma_hold,
                             "cand": pick, "pos": adv, "taken": False,
                             "qualified": bool(qualified)})
            continue

        if not qualified:
            continue
        pick = max(qualified, key=lambda c: c["net_ev_rs"])

        # --- advisory ledger: score it whether or not the book could take it
        legs = legs_of(pick)
        adv = Position(legs=legs, lot_size=lot, entry_ts=t, expiry=expiry,
                       rule=rule, label=pick["name"])
        advisory.append({"ts": t, "spot": spot, "sigma": sigma_hold,
                         "cand": pick, "pos": adv,
                         "taken": open_pos is None, "qualified": True})

        # --- book ledger: one position at a time
        if open_pos is None:
            bp = Position(legs=legs, lot_size=lot, entry_ts=t, expiry=expiry,
                          rule=rule, label=pick["name"])
            open_pos = (bp, {"ts": t, "spot": spot, "cand": pick})

    # --- walk every advisory position to its own exit
    for a in advisory:
        pos = a["pos"]
        for t in minutes:
            if t <= a["ts"] or not pos.is_open:
                continue
            try:
                pos.step(t, book[t], 0)
            except KeyError:
                continue
        a["exit"] = pos.exit
        a["touched"] = touched(nifty, a["ts"], flat_dt,
                               a["cand"]["lower"], a["cand"]["upper"])
        a["final"] = final_index(nifty, flat_dt)

    if open_pos is not None:                      # never exited: force flat
        pos, meta = open_pos
        last = minutes[-1]
        try:
            pos.step(flat_dt, book[last], 0)
        except KeyError:
            pass
        trades.append((pos, meta))

    return {"day": day, "expiry": expiry, "lot": lot,
            "decisions": len(decision_times), "advisory": advisory,
            "trades": trades, "refused": refused}


def touched(nifty, start, end, lower, upper):
    for t, v in nifty.items():
        if start <= t <= end and (v <= lower or v >= upper):
            return True
    return False


def final_index(nifty, end):
    at = [t for t in nifty if t <= end]
    return nifty[max(at)] if at else None


# ------------------------------------------------------------------- report

def main():
    every, capture, gate = 15, 0.85, CS_GATE
    names, only = TWO_LEG, None
    calibrate = "--calibrate" in sys.argv
    guard = "--guard" in sys.argv
    newvol = "--newvol" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--every":
            every = int(sys.argv[i + 1])
        if a == "--capture":
            capture = float(sys.argv[i + 1])
        if a == "--gate":
            gate = float(sys.argv[i + 1])
        if a == "--structures":
            want = sys.argv[i + 1]
            if want == "all":
                print("--structures all is not implemented yet: the four-leg")
                print("builders live in strategies.py, which constructs its")
                print("own floored Volatility and so cannot price an intraday")
                print("horizon. Nisarg has not settled the structure set, so")
                print("porting them is deliberately deferred. Running two-leg.")
                print()
            elif want != "two-leg":
                print(f"unknown --structures {want!r}; running two-leg\n")
        if a == "--session":
            only = date.fromisoformat(sys.argv[i + 1])

    days = [d for d in sessions() if only is None or d == only]
    if not days:
        print("no sessions")
        return

    print(f"INTRADAY BACKTEST   every {every} min, gate {gate:.4f}, "
          f"exit {capture:.0%}, flat {FLAT_BY}")
    if calibrate:
        print("CALIBRATION MODE -- every candidate scored, nothing traded. "
              "The book ledger stays empty on purpose.")
    if guard:
        print("SPOT GUARD ON -- minutes whose index print disagrees with "
              "the chain are skipped, not scored.")
    cal = load_calibration() if newvol else None
    if newvol and cal is None:
        print(f"--newvol asked for, but {CALIBRATION} is missing or "
              f"unreadable. Run vol_calibrate.py first. Using the old sigma.")
    elif cal is not None:
        print(f"SIGMA: fitted calibration  k_session {cal.k_session:.4f}  "
              f"rho {cal.rho:+.4f}  "
              f"k_gap overnight {cal.k_gap.get('overnight', 0):.4f}")
    else:
        print("SIGMA: the old flat sqrt(t) model")
    print()
    hdr = (f"{'session':<12}{'expiry':<12}{'looks':>7}{'signals':>9}"
           f"{'taken':>7}{'book P&L':>11}{'per trade':>11}")
    print(hdr)
    print("-" * len(hdr))

    all_adv, all_trades = [], []
    all_refused = 0
    for d in days:
        r = run_session(d, every, capture, gate, names, calibrate,
                        cal, guard)
        if r is None:
            print(f"{str(d):<12}{'':<12}  no usable data")
            continue
        all_adv.extend(r["advisory"])
        all_trades.extend(r["trades"])
        all_refused += r.get("refused", 0)
        pnl = sum(p.exit.pnl_rs(r["lot"], credit_pts=p.credit_pts)
                  for p, _ in r["trades"] if p.exit)
        n = len(r["trades"])
        print(f"{str(d):<12}{str(r['expiry']):<12}{r['decisions']:>7}"
              f"{len(r['advisory']):>9}{n:>7}{pnl:>11,.0f}"
              f"{(pnl / n if n else 0):>11,.0f}")

    print("-" * len(hdr))
    if not all_adv:
        print("\nNo signal ever qualified. With the daily book's gate shut on")
        print("all nine sessions that is the expected result, not a failure --")
        print("two weeks of fairly-priced premium does not become rich just")
        print("because you look at it more often. Try --gate 0 to see what")
        print("the market would have paid at fair value.")
        return

    # ------------------------------------------------- advisory calibration
    done = [a for a in all_adv if a.get("exit")]
    inside = [a for a in done
              if a["final"] is not None
              and a["cand"]["lower"] < a["final"] < a["cand"]["upper"]]
    untouched = [a for a in done if not a["touched"]]

    print(f"\nADVISORY LEDGER -- does the model predict?\n")
    print(f"  recommendations scored      {len(done):>8}")
    print(f"  predicted terminal POP      "
          f"{sum(a['cand']['pop_terminal'] for a in done)/len(done):>8.1%}")
    print(f"  actually finished inside    {len(inside)/len(done):>8.1%}"
          f"   ({len(inside)} of {len(done)})")
    print(f"  predicted no-touch POP      "
          f"{sum(a['cand']['pop_no_touch'] for a in done)/len(done):>8.1%}")
    print(f"  actually never touched      {len(untouched)/len(done):>8.1%}"
          f"   ({len(untouched)} of {len(done)})")
    print()
    print(f"  modelled net EV             "
          f"Rs {sum(a['cand']['net_ev_rs'] for a in done)/len(done):>8,.0f}")
    realised = [a["exit"].pnl_rs(a["pos"].lot_size,
                                 credit_pts=a["pos"].credit_pts)
                for a in done]
    print(f"  realised per recommendation Rs {sum(realised)/len(realised):>8,.0f}")

    worst = min(a["exit"].mae_pts for a in done)
    print(f"  worst adverse excursion     {worst:>8.0f} pts")

    print(f"\nBOOK LEDGER -- what a single-position account did\n")
    booked = [(p, m) for p, m in all_trades if p.exit]
    if booked:
        tot = sum(p.exit.pnl_rs(p.lot_size, credit_pts=p.credit_pts)
                  for p, _ in booked)
        wins = sum(1 for p, _ in booked if p.exit.pnl_pts > 0)
        print(f"  trades                      {len(booked):>8}")
        print(f"  win rate                    {wins/len(booked):>8.1%}")
        print(f"  total                       Rs {tot:>8,.0f}")
        print(f"  per trade                   Rs {tot/len(booked):>8,.0f}")
        by = {}
        for p, _ in booked:
            by.setdefault(p.exit.reason, []).append(p.exit.pnl_pts)
        print()
        for reason, v in sorted(by.items()):
            print(f"  exited {reason:<8} {len(v):>4} trades   "
                  f"mean {sum(v)/len(v):+7.1f} pts")

    print()
    print("SAMPLE")
    print(f"  scored recommendations      {len(done)}")
    print(f"  independent SESSIONS        {len({a['ts'].date() for a in done})}")
    if guard:
        print(f"  minutes refused by the guard{all_refused:>7}"
              f"   (index print disagreed with the chain)")
    print("  Recommendations inside one session share that session's move.")
    print("  They are separate trades but not separate tests -- the session")
    print("  count is the number that limits what can be concluded.")


if __name__ == "__main__":
    main()

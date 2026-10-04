"""Does selling premium INTRADAY actually pay, once the book is charged for?

THE QUESTION

The weekly strategy is closed: measured at the price a seller is actually
paid, no horizon from 7 to 60 days clears t = 2, and the apparent 0 DTE edge
was sigma error -- a 30-day implied volatility applied to a same-day horizon,
where the market prices volatility about a third higher.

This asks the same question at a horizon the collected data actually covers,
with that error corrected: sell the at-the-money straddle after the opening
auction settles, cover before the close, and see what is left. Nothing here
uses VIX. The only volatility input is the chain's own price.

WHAT MAKES THIS HONEST

  Sell at the BID, cover at the ASK. Both come from the collected book, so
  the spread is measured rather than modelled, and the project's
  slippage_per_leg assumption is deliberately NOT applied on top -- that
  would charge the spread twice, about four points a round trip.

  Statutory charges, brokerage and GST come from pricing.transaction_costs,
  the same function the signal uses, so there is no second cost model to
  drift. An earlier version of this note guessed the STT rate at 0.0625%
  and concluded the workbook's 0.001 OVERSTATED costs. Both halves of that
  were wrong. STT on the sale of options premium is 0.15% (0.0015), raised
  from 0.10% with effect from 1 April 2026 and payable by the seller -- so
  the workbook figure UNDERSTATED costs by a third. COSTS below now passes
  the correct rate. The correction is worth 0.05% of the credit, about
  Rs 7.50 on a 200-point NIFTY straddle, which is roughly 15% of this
  study's median session.

  Maximum adverse excursion is measured minute by minute, because it is the
  thing most likely to kill this design. A 30%-of-credit stop is only
  sensible if the straddle rarely runs 30% against a seller BEFORE decaying.
  If it routinely spikes at 10:30 and bleeds back by 15:00, that stop
  converts winning days into losing ones and the daily P&L would never tell
  you why.

  The baseline is unconditional. Every session is entered, so what comes out
  is what the strategy does, not what it does on the days a filter liked.

Usage:
    python intraday_vrp.py
    python intraday_vrp.py --stop 0.30 --entry 09:20 --exit 15:15
    python intraday_vrp.py --underlying BANKNIFTY --widths 0,2,4
"""

import glob
import os
import sys
from datetime import date, timedelta

import pandas as pd

import decay_model as DM
import pricing as P

BARS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "bars_1m")
SESSION_MINUTES = 375           # 09:15 to 15:29 inclusive
DEFAULT_LOT = 65

COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there


# --------------------------------------------------------------- loading

def sessions(underlying):
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        d = os.path.basename(p)[5:]
        if glob.glob(os.path.join(p, f"underlying={underlying}", "*.parquet")):
            try:
                out.append(date.fromisoformat(d))
            except ValueError:
                continue
    return out


def load(day, underlying):
    """{hm: {strike: {"CE": (bid, ask), "PE": (bid, ask)}}} for the front expiry.

    Keeps bid and ask rather than the mid, because the whole point is to
    charge the trade what crossing the book actually costs.
    """
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={underlying}", "*.parquet")))
    if not files:
        return {}, None, "no parquet for this session"
    cols = ["ts", "expiry", "strike", "opt_type", "bid", "ask"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception as e:
            return {}, None, f"{os.path.basename(f)}: {type(e).__name__}: {e}"
    df = pd.concat(frames, ignore_index=True)

    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    live = sorted({e for e in df["expiry"].unique() if e >= day})
    if not live:
        return {}, None, "no expiry at or after this session"
    exp = live[0]
    df = df[df["expiry"] == exp]

    # Two-sided only, and never a crossed book.
    df = df[(df["bid"] > 0) & (df["ask"] >= df["bid"])]
    if df.empty:
        return {}, exp, "no two-sided quotes"

    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.assign(hm=ts.dt.strftime("%H:%M"))

    out = {}
    for r in df.itertuples():
        out.setdefault(r.hm, {}).setdefault(float(r.strike), {})[
            str(r.opt_type)] = (float(r.bid), float(r.ask))
    return out, exp, None


def infer_step(strikes):
    ks = sorted(set(strikes))
    if len(ks) < 3:
        return 50.0
    gaps = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a]
    return float(pd.Series(gaps).mode().iloc[0]) if gaps else 50.0


def atm_by_parity(chain):
    """The strike where call and put agree. No volatility assumption in it."""
    best, gap = None, None
    for k, legs in chain.items():
        c, p = legs.get("CE"), legs.get("PE")
        if not c or not p:
            continue
        g = abs((c[0] + c[1]) / 2 - (p[0] + p[1]) / 2)
        if gap is None or g < gap:
            best, gap = k, g
    return best


def forward(chain):
    k = atm_by_parity(chain)
    if k is None:
        return None
    c, p = chain[k]["CE"], chain[k]["PE"]
    return k + (c[0] + c[1]) / 2 - (p[0] + p[1]) / 2


def legs_for(atm, step, width):
    """width 0 is the straddle; n sells the call n strikes up, put n down."""
    if width == 0:
        return [(atm, "CE"), (atm, "PE")]
    return [(atm + width * step, "CE"), (atm - width * step, "PE")]


def sell_price(chain, legs):
    """What you RECEIVE, selling into the bid. None if any leg is unquotable."""
    total = 0.0
    for k, ot in legs:
        q = chain.get(k, {}).get(ot)
        if not q or q[0] <= 0:
            return None
        total += q[0]
    return total


def cover_price(chain, legs):
    """What you PAY to close, lifting the ask."""
    total = 0.0
    for k, ot in legs:
        q = chain.get(k, {}).get(ot)
        if not q or q[1] <= 0:
            return None
        total += q[1]
    return total


# ------------------------------------------------------------ simulation

def trading_days(a, b):
    """Weekdays after `a` up to and including `b`. Holidays not handled --
    it would overstate time to expiry slightly, which understates the
    theoretical decay, so the bias is against the strategy."""
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def theoretical_decay(day, expiry, entry_idx, exit_idx):
    """The prize if nothing moves: a straddle scales with sqrt(time).

    Expressed in SESSIONS, with today counted as the fraction of the session
    still to run, so an entry at 09:20 on a 4-sessions-out expiry has
    4 + 370/375 sessions of time left and an exit at 15:15 has 4 + 15/375.
    """
    n = trading_days(day, expiry)
    s_in = n + (SESSION_MINUTES - entry_idx) / SESSION_MINUTES
    s_out = n + (SESSION_MINUTES - exit_idx) / SESSION_MINUTES
    if s_in <= 0:
        return 0.0
    return 1.0 - (s_out / s_in) ** 0.5


def simulate(bm, day, expiry, width, entry="09:20", exit_at="15:15",
             stop_frac=0.30, target_frac=None, lot=DEFAULT_LOT):
    """One session, one structure. Returns a dict or None if unpriceable.

    Marks every minute at what it would COST TO CLOSE, which is the only
    mark that tells a short position the truth.
    """
    times = sorted(t for t in bm if entry <= t <= exit_at)
    if len(times) < 2:
        return None
    t0, tN = times[0], times[-1]

    step = infer_step([k for m in bm.values() for k in m])
    atm = atm_by_parity(bm[t0])
    if atm is None:
        return None
    legs = legs_for(atm, step, width)

    credit = sell_price(bm[t0], legs)
    if not credit or credit <= 0:
        return None

    stop_at = credit * (1 + stop_frac) if stop_frac is not None else None
    target_at = credit * (1 - target_frac) if target_frac else None

    # TWO adverse-excursion numbers, because they answer different questions
    # and conflating them is how a stop gets judged on movement it prevented.
    #
    #   mae_held    what the trade actually experienced, up to its exit. If a
    #               stop fired at 10:55, nothing after 10:55 happened to it.
    #   mae_session what the window did regardless of the exit. This is the
    #               one that says whether a stop of a given size would have
    #               been TOUCHED, and it is only meaningful on an unstopped
    #               run -- which is why the study runs both.
    mae_session, mae_at = 0.0, t0
    mae_held, mae_held_at, mfe = 0.0, t0, 0.0
    exit_reason, exit_time, exit_cost = "time", tN, None
    closed = False
    marks = []

    for t in times:
        mark = cover_price(bm[t], legs)
        if mark is None:
            continue
        marks.append((t, mark))
        adverse = mark - credit
        if adverse > mae_session:
            mae_session, mae_at = adverse, t
        if closed:
            continue
        if adverse > mae_held:
            mae_held, mae_held_at = adverse, t
        if credit - mark > mfe:
            mfe = credit - mark
        if stop_at is not None and mark >= stop_at:
            exit_reason, exit_time, exit_cost, closed = "stop", t, mark, True
        elif target_at is not None and mark <= target_at:
            exit_reason, exit_time, exit_cost, closed = "target", t, mark, True

    if not marks:
        return None
    if exit_cost is None:
        exit_time, exit_cost = marks[-1]

    pnl_pts = credit - exit_cost
    fees = P.transaction_costs(credit, lot, 1, defined_risk=False, costs=COSTS)

    # Position of entry and exit within the whole session, so the theoretical
    # decay knows how much of today is already gone.
    all_times = sorted(bm)
    ei = all_times.index(t0)
    xi = all_times.index(exit_time) if exit_time in bm else len(all_times) - 1

    f0, f1 = forward(bm[t0]), forward(bm[exit_time])

    # Both ends of the spread. `credit` is a BID and `cover` is an ASK, so the
    # round trip pays half of each -- charging the entry spread twice would
    # move that difference into the decay term and flatter it. Only the
    # attribution uses this; nothing above it changes.
    _x_ask = cover_price(bm[exit_time], legs)
    _x_bid = sell_price(bm[exit_time], legs)
    spread_out = (_x_ask - _x_bid) if (_x_ask and _x_bid) else None

    # Theoretical decay, valued PER STRUCTURE rather than as a shared time
    # fraction. theoretical_decay() returns 1 - sqrt(s_out/s_in), which takes
    # no strike and no width, so scaling it by credit gave an ATM straddle and
    # a strangle four strikes out the identical decay FRACTION. Wings are
    # convex in sigma and shed relative value faster: on a 1-DTE session the
    # old model put a +/-4 strangle's decay at 5.44 points where a proper
    # revaluation gives 14.43, understating it by 2.7x. Every `capture`
    # reading for a strangle was measured against roughly a third of the right
    # yardstick.
    #
    # decay_model agrees with the old formula on a straddle to 1e-13 (its
    # probe block 2 asserts this across fifteen dte/window combinations), so
    # the fourteen journalled ATM sessions stay comparable.
    n_days = trading_days(day, expiry)
    s_in = DM.sessions_left(n_days, ei)
    s_out = DM.sessions_left(n_days, xi)
    atm_legs = legs_for(atm, step, 0)
    atm_bid = sell_price(bm[t0], atm_legs)
    atm_ask = cover_price(bm[t0], atm_legs)
    # Mid, not bid: sigma is a model input and should not carry the spread
    # the trade pays. The spread is charged separately, in pnl_pts.
    atm_mid = (atm_bid + atm_ask) / 2.0 if (atm_bid and atm_ask) else None
    sigma_sess = DM.session_sigma(atm_mid, s_in)
    theo = DM.decay_points(legs, f0, sigma_sess, s_in, s_out)
    if theo is None:
        # Unpriceable ATM straddle at entry -- fall back to the old shared
        # fraction rather than dropping the session. Flagged so a run that
        # leans on it is visible instead of silently mixing two models.
        theo = theoretical_decay(day, expiry, ei, xi) * credit
        theo_model = "time-fraction (fallback)"
    else:
        theo_model = "per-structure"

    # A session whose data stops before the exit time is not a short session,
    # it is an UNFINISHED one -- today, mid-morning, looks like a complete
    # trade that happened to decay very little. It showed up as a theoretical
    # decay of 3.1 points at 3 DTE where a comparable session gave 28.7.
    last_minute = max(bm)
    complete = last_minute >= exit_at

    return {
        "date": day, "expiry": expiry, "width": width,
        "dte": trading_days(day, expiry),
        "complete": complete, "last_minute": last_minute,
        "legs": legs, "atm": atm, "step": step,
        "entry": t0, "exit": exit_time, "exit_reason": exit_reason,
        "credit": credit, "cover": exit_cost,
        "theo_model": theo_model, "sigma_session": sigma_sess,
        # Returned so the P&L can be taken apart later without reloading the
        # book -- daytrade_api deliberately discards it after four numbers.
        "s_in": s_in, "s_out": s_out,
        "spread_at_entry": cover_price(bm[t0], legs) - credit,
        "spread_at_exit": spread_out,
        "pnl_pts": pnl_pts, "pnl_rs": pnl_pts * lot,
        "fees_rs": fees, "net_rs": pnl_pts * lot - fees,
        "mae_pts": mae_held, "mae_pct": mae_held / credit,
        "mae_at": mae_held_at,          # when the TRADE felt its worst
        "mae_session_pts": mae_session, "mae_session_pct": mae_session / credit,
        "mae_session_at": mae_at,       # when the WINDOW peaked, stop or not
        "mfe_pts": mfe, "mfe_pct": mfe / credit,
        "stopped": exit_reason == "stop",
        "fwd_open": f0, "fwd_exit": f1,
        "fwd_move": (f1 - f0) if (f0 and f1) else None,
        "theo_decay_pts": theo,
        "capture": (pnl_pts / theo) if theo else None,
        "minutes": len(marks),
    }


# ---------------------------------------------------------------- report

def med(xs):
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def fmt(v, w=9, p=2, suffix=""):
    return f"{'-':>{w}}" if v is None else f"{v:>{w - len(suffix)}.{p}f}{suffix}"


def report(rows, width, stop_frac, touch_level=None):
    label = "ATM straddle" if width == 0 else f"strangle +/-{width} strikes"
    mode = "NO STOP" if stop_frac is None else f"stop {stop_frac:.0%} of credit"
    print(f"\n{'=' * 104}\n{label.upper()}   {mode}   "
          f"(sell the bid, cover the ask)\n{'=' * 104}")
    if not rows:
        print("  no session could be priced")
        return None

    hdr = (f"{'date':<12}{'dte':>4}{'credit':>9}{'spread':>8}{'fwd move':>10}"
           f"{'MAE%':>8}{'MAE at':>8}{'exit':>7}{'pnl pts':>9}"
           f"{'net Rs':>9}{'theo':>8}{'capture':>9}")
    print(hdr + "\n" + "-" * len(hdr))
    for r in rows:
        dte = r["dte"]
        print(f"{str(r['date']):<12}{dte:>4}{fmt(r['credit'])}"
              f"{fmt(r['spread_at_entry'], 8)}"
              f"{fmt(r['fwd_move'], 10, 1)}"
              f"{fmt(r['mae_pct'] * 100, 8, 1)}{r['mae_at']:>8}"
              f"{r['exit_reason']:>7}{fmt(r['pnl_pts'])}"
              f"{fmt(r['net_rs'], 9, 0)}{fmt(r['theo_decay_pts'], 8, 1)}"
              f"{fmt(r['capture'], 9, 2)}")

    n = len(rows)
    nets = [r["net_rs"] for r in rows]
    wins = sum(1 for v in nets if v > 0)
    stops = sum(1 for r in rows if r["exit_reason"] == "stop")
    print("-" * len(hdr))
    print(f"  sessions            {n}")
    print(f"  median net          Rs {med(nets):,.0f} per lot   "
          f"(mean Rs {sum(nets) / n:,.0f})")
    print(f"  win rate            {wins}/{n} = {100 * wins / n:.0f}%")
    print(f"  total net           Rs {sum(nets):,.0f}")
    print(f"  median credit       {med([r['credit'] for r in rows]):.2f} pts")
    print(f"  median entry spread {med([r['spread_at_entry'] for r in rows]):.2f} pts"
          f"   ({100 * med([r['spread_at_entry'] for r in rows]) / med([r['credit'] for r in rows]):.1f}% of credit)")
    print(f"  median fees         Rs {med([r['fees_rs'] for r in rows]):,.0f}")
    print(f"  median MAE held     {100 * med([r['mae_pct'] for r in rows]):.1f}% of credit")
    print(f"  worst MAE held      {100 * max(r['mae_pct'] for r in rows):.1f}% of credit")
    if stop_frac is None:
        # Only meaningful unstopped: nothing closed the position early, so the
        # window's worst point is the worst point the trade would have seen.
        for lvl in (touch_level or 0.30, 0.50, 1.00):
            hit = sum(1 for r in rows if r["mae_session_pct"] >= lvl)
            print(f"  a {lvl:.0%} stop would have been touched  {hit}/{n}")
    else:
        print(f"  stop fired          {stops}/{n}")
    print(f"  median theo decay   {med([r['theo_decay_pts'] for r in rows]):.2f} pts")
    print(f"  median capture      {fmt(med([r['capture'] for r in rows]), 5, 2)}"
          f"   (1.0 = collected exactly the theoretical decay)")

    # Broken out because the pooled total hides it: on the first run, three
    # 0-DTE sessions carried the entire loss and the other ten were flat.
    buckets = {}
    for r in rows:
        buckets.setdefault(r["dte"], []).append(r["net_rs"])
    print("  by DTE:")
    for dte in sorted(buckets):
        v = buckets[dte]
        print(f"    {dte} DTE   n={len(v):<3} total Rs {sum(v):>10,.0f}   "
              f"median Rs {med(v):>8,.0f}   wins {sum(1 for x in v if x > 0)}/{len(v)}")
    return {"n": n, "median_net": med(nets), "win": wins,
            "median_mae": med([r["mae_pct"] for r in rows]),
            "total": sum(nets), "stops": stops,
            "label": f"{label} · {mode}"}


def main():
    args = {"--underlying": "NIFTY", "--entry": "09:20", "--exit": "15:15",
            "--stop": "0.30", "--widths": "0,2,4", "--lot": str(DEFAULT_LOT),
            "--min-dte": "0"}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]
    und = args["--underlying"]
    stop_frac = float(args["--stop"])
    lot = int(args["--lot"])
    min_dte = int(args["--min-dte"])
    widths = [int(w) for w in args["--widths"].split(",")]
    keep_partial = "--include-partial" in sys.argv

    days = sessions(und)
    if not days:
        print(f"no sessions under {BARS}")
        return 1
    print(f"INTRADAY PREMIUM STUDY   {und}   {len(days)} sessions   "
          f"{days[0]} to {days[-1]}")
    print(f"entry {args['--entry']}  exit {args['--exit']}  "
          f"lot {lot}  min DTE {min_dte}  no VIX anywhere in this")

    loaded = {}
    for d in days:
        bm, exp, err = load(d, und)
        if err:
            print(f"  {d}: {err}")
            continue
        loaded[d] = (bm, exp)

    # Every structure is run BOTH ways. Judging a stop without the unstopped
    # baseline beside it is how you conclude a stop "protected" you on days it
    # actually cost you the recovery.
    # Filtered once, before anything is priced, and announced before the
    # tables rather than after them -- you should know what was excluded
    # before you read a number that depends on it.
    usable = {}
    for d, (bm, exp) in sorted(loaded.items()):
        if not bm:
            print(f"  SKIPPED {d}: no usable minutes")
            continue
        last = max(bm)
        if last < args["--exit"] and not keep_partial:
            print(f"  SKIPPED {d}: data ends {last}, before the "
                  f"{args['--exit']} exit — an unfinished session, not a "
                  f"short one. --include-partial to keep it.")
            continue
        dte = trading_days(d, exp)
        if dte < min_dte:
            print(f"  SKIPPED {d}: {dte} DTE, below the {min_dte} minimum")
            continue
        usable[d] = (bm, exp)
    print(f"  {len(usable)} of {len(loaded)} sessions usable")
    if not usable:
        return 1

    summaries = []
    for w in widths:
        for sf in (None, stop_frac):
            rows = []
            for d, (bm, exp) in usable.items():
                r = simulate(bm, d, exp, w, entry=args["--entry"],
                             exit_at=args["--exit"], stop_frac=sf, lot=lot)
                if r:
                    rows.append(r)
                elif sf is None:
                    print(f"  {d}: width {w} could not be priced")
            s = report(rows, w, sf, touch_level=stop_frac)
            if s:
                summaries.append(s)

    if summaries:
        print(f"\n{'=' * 104}\nSIDE BY SIDE\n{'=' * 104}")
        print(f"{'structure':<40}{'n':>4}{'median net Rs':>15}"
              f"{'total Rs':>12}{'wins':>7}{'median MAE':>13}{'stops':>7}")
        for s in summaries:
            print(f"{s['label']:<40}{s['n']:>4}{s['median_net']:>15,.0f}"
                  f"{s['total']:>12,.0f}{s['win']:>7}"
                  f"{100 * s['median_mae']:>12.1f}%{s['stops']:>7}")

    print(f"\n  {len(loaded)} sessions is not a sample. Read the MAE column and "
          f"the spread as a share of credit;\n  those are measured properties "
          f"of the book and are already meaningful. Treat the P&L as an\n"
          f"  order of magnitude, not a result.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

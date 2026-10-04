"""The intraday paper journal: log the baseline every session, score it, keep it.

WHY THIS EXISTS

intraday_richness.py found a real and strikingly consistent premium -- implied
sat above realised in 12 of 12 sessions, median ratio 0.68. intraday_vrp.py
found the P&L from selling it is roughly zero on the mean, because three
sessions in twelve erased what the other nine made.

Both are true. The premium is the insurance you sold; the three sessions are
the claims. Twelve sessions contains three claims, which is nowhere near
enough to price a tail. Nothing else can be concluded from this data, and no
amount of re-cutting it will change that.

So this stops analysing and starts recording. Every session gets a trade,
scored against what actually happened, written to its own journal. Forty
sessions might begin to say something. Twelve never will.

PAPER ONLY. Nothing here places an order, and nothing in this project can.

NO STATE, ON PURPOSE

A row is a pure function of that session's parquet. Run this at 09:25, at
noon, at 15:20, or a week later and the same session produces the same row --
open while the session is unfinished, settled once it reaches the exit. A
missed run loses nothing, because the next run rebuilds it from the bars.
That is why it can be a timer-driven one-shot instead of a process holding a
position in memory, and why --backfill can populate the whole history at once.

NO PROFIT TARGET, BY DEFAULT

With a stop at 30% of credit, a target at T x credit breaks even at a win
rate of 0.30 / (T + 0.30). The observed win rate is 67%, which needs a target
around 15% of credit; the median decay actually achieved is about 5%. So a
reachable target cannot pay for the stop, and a target that could pay for it
is not reachable. A short straddle's profit is already capped at the credit --
a target caps it twice while the loss side has only the stop. Available via
--target for testing, off by default.

Usage:
    python daytrade.py                 update today's row
    python daytrade.py --backfill      rebuild every session from history
    python daytrade.py --show          print the journal
    python daytrade.py --overnight     the gap study (see OVERNIGHT below)
"""

import project_paths
import csv
import os
import sys
from datetime import date

import attribution as A
import intraday_vrp as V
import intraday_richness as R
import sigma_sources as SIG
import stop_touch as T

ROOT = project_paths.ROOT
JOURNAL = os.path.join(ROOT, "data", "journal", "intraday_trades.csv")

ENTRY, EXIT = "09:20", "15:15"
STOP_FRAC = 0.30          # of credit; from the sizing card, not fitted here
TRAIL = 10                # sessions of realised history behind the richness score
MIN_TRAIL = 3

COLUMNS = [
    "date", "underlying", "expiry", "dte", "status",
    "entry", "atm", "structure", "credit", "stop_level", "target_level",
    "implied_sigma", "trailing_realised", "richness", "richness_pct",
    "exit", "exit_reason", "cover", "pnl_pts", "fees_rs", "net_rs", "lot",
    "mae_pct", "realised_sigma", "ratio",
    "fwd_open", "fwd_exit", "fwd_move", "overnight_gap", "minutes",
    # Where the number in net_rs came from. Signed from the seller's side:
    # decay positive, direction negative on any move, spread always negative.
    # unexplained_pts is the reconciliation against credit-minus-cover and
    # should sit at zero -- a column rather than an assertion, so a session
    # where it does not is visible in the journal instead of raising inside a
    # timer nobody is watching.
    "decay_pts", "direction_pts", "vol_pts", "spread_pts", "entry_gap_pts",
    "unexplained_pts",
    # MODELLED probability that this session's stop is touched before the
    # exit, as it stood at entry -- see stop_touch and p_stop_fields below.
    # Two sigmas because they disagree and the journal is where to find out
    # which one the stops agree with. Blank when either input is missing.
    "p_stop_vix", "p_stop_implied", "sigma_vix_pts", "sigma_implied_pts",
]


def _vix_at(day, hm):
    """India VIX at or just before `hm` on `day`, from the INDEX partition."""
    import intraday as I
    frames = I.index_frames(day)
    vix = frames.get(I.VIX_TOKEN) or {}
    if not vix:
        return None
    h, m = map(int, hm.split(":"))
    want = [t for t in vix if (t.hour, t.minute) <= (h, m)]
    return vix[max(want)] if want else None


def p_stop_fields(day, bm, expiry, sim, stop_frac, entry, exit_at,
                  vix=None, session_share=None, is_trading_day=None):
    """The two modelled P(stop) columns for one session.

    Everything is taken as it stood at ENTRY, so a row can be rebuilt a week
    later and give the same number. The stop is on cost-to-close at the ask,
    against the credit received at the bid -- the journal's own rule -- so the
    model is anchored to the entry MID and adds the entry half-spread.

      vix       sigma = forward * VIX/100 * sqrt(dte/365), the site's formula
                (sigma_sources.sigma_points), VIX read at entry.
      implied   sigma to expiry implied by the entry straddle mid under the
                normal model: mid / sqrt(2/pi).
    """
    blank = {"p_stop_vix": "", "p_stop_implied": "",
             "sigma_vix_pts": "", "sigma_implied_pts": ""}
    if stop_frac is None or sim is None:
        return blank
    share = session_share if session_share is not None else T.load_session_share()
    if share is None:
        return blank
    if is_trading_day is None:
        import market as MK
        is_trading_day = MK.is_trading_day

    t0 = sim["entry"]
    legs_q = V.legs_for(sim["atm"], V.infer_step([k for m in bm.values() for k in m]), 0)
    bid, ask = V.sell_price(bm[t0], legs_q), V.cover_price(bm[t0], legs_q)
    fwd = V.forward(bm[t0])
    if not (bid and ask and fwd):
        return blank
    mid, half = (bid + ask) / 2.0, (ask - bid) / 2.0
    legs = [{"strike": sim["atm"], "kind": "CE", "qty": -1},
            {"strike": sim["atm"], "kind": "PE", "qty": -1}]

    h, m = map(int, t0.split(":"))
    xh, xm = map(int, exit_at.split(":"))
    from datetime import datetime as _dt
    now = _dt(day.year, day.month, day.day, h, m)
    ex = _dt(day.year, day.month, day.day, xh, xm)
    dte = max((expiry - day).days, 0)

    v = vix if vix is not None else _vix_at(day, t0)
    sig_vix = SIG.sigma_points(fwd, v, dte) if v else None
    sig_imp = mid / T.sqrt(2.0 / 3.141592653589793)

    out = dict(blank)
    for key, sig in (("vix", sig_vix), ("implied", sig_imp)):
        if not sig:
            continue
        r = T.simulate(fwd, sig, [{"name": "s", "legs": legs, "market_mid": mid,
                                   "half_spread": half, "credit": bid}],
                       now, expiry, ex, is_trading_day, share,
                       stop_fracs=(stop_frac,))["s"]
        if r is not None:
            out[f"p_stop_{key}"] = r.grid[0]["p_touch"]
            out[f"sigma_{key}_pts"] = round(sig, 1)
    return out


# ----------------------------------------------------------------- scoring

def richness_score(implied, history):
    """Today's implied sigma against how the market has ACTUALLY been moving.

    Deliberately not implied-against-implied: that would just rank today
    against other prices, and the richness study showed implied is dominated
    by DTE (corr +0.78), so a price-only score would mostly report days to
    expiry wearing a disguise. Measuring against trailing REALISED asks the
    question a seller cares about -- am I being paid more than recent
    movement justifies -- and is computable at 09:20 from settled history.
    """
    past = [h for h in history if h is not None]
    if len(past) < MIN_TRAIL or not implied:
        return None, None
    trail = R.med(past[-TRAIL:])
    if not trail:
        return None, None
    return trail, implied / trail


def percentile(value, others):
    vals = [v for v in others if v is not None]
    if value is None or len(vals) < MIN_TRAIL:
        return None
    return round(100.0 * sum(1 for v in vals if v <= value) / len(vals), 1)


# ---------------------------------------------------------------- the row

def evaluate(day, bm, expiry, underlying, history, richness_history,
             stop_frac=STOP_FRAC, target_frac=None,
             entry=ENTRY, exit_at=EXIT, lot=V.DEFAULT_LOT):
    """One session's journal row. Open if unfinished, settled if it reached the exit."""
    sim = V.simulate(bm, day, expiry, width=0, entry=entry, exit_at=exit_at,
                     stop_frac=stop_frac, target_frac=target_frac, lot=lot)
    if sim is None:
        return None
    rich = R.study(day, bm, expiry, entry, exit_at)

    att = A.attribute(sim, lot)
    implied = rich["implied"] if rich else None
    trail, score = richness_score(implied, history)
    realised = rich["rv15"] if rich else None

    return {
        "date": str(day), "underlying": underlying, "expiry": str(expiry),
        "dte": sim["dte"],
        "status": "settled" if sim["complete"] else "open",
        "entry": sim["entry"], "atm": sim["atm"], "structure": "atm_straddle",
        "credit": round(sim["credit"], 2),
        "stop_level": round(sim["credit"] * (1 + stop_frac), 2),
        "target_level": (round(sim["credit"] * (1 - target_frac), 2)
                         if target_frac else ""),
        "implied_sigma": round(implied, 1) if implied else "",
        "trailing_realised": round(trail, 1) if trail else "",
        "richness": round(score, 3) if score else "",
        "richness_pct": percentile(score, richness_history) or "",
        "exit": sim["exit"], "exit_reason": sim["exit_reason"],
        "cover": round(sim["cover"], 2),
        "pnl_pts": round(sim["pnl_pts"], 2),
        "fees_rs": round(sim["fees_rs"], 0),
        "net_rs": round(sim["net_rs"], 0), "lot": lot,
        "mae_pct": round(sim["mae_pct"], 4),
        "realised_sigma": round(realised, 1) if realised else "",
        "ratio": round(realised / implied, 3) if (realised and implied) else "",
        "fwd_open": round(sim["fwd_open"], 1) if sim["fwd_open"] else "",
        "fwd_exit": round(sim["fwd_exit"], 1) if sim["fwd_exit"] else "",
        "fwd_move": round(sim["fwd_move"], 1) if sim["fwd_move"] is not None else "",
        **A.row_fields(att),
        "overnight_gap": "", "minutes": sim["minutes"],
        **p_stop_fields(day, bm, expiry, sim, stop_frac, entry, exit_at),
    }


# ------------------------------------------------------------------- i/o

# path=None rather than path=JOURNAL: a default argument binds once, at
# definition time, so a caller that reassigns the module-level JOURNAL would
# still silently write to the original file. Resolving it per call is what
# lets the tests point this at a temp directory -- and would let a future
# caller keep a second journal without editing this module.
def read_journal(path=None):
    path = path or JOURNAL
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_journal(rows, path=None):
    path = path or JOURNAL
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(rows, key=lambda r: r["date"])
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)       # atomic; a crash mid-write cannot truncate it
    return path


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n. (lo, hi) or None."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def stop_calibration(settled):
    out = {}
    lo_hi = wilson(sum(1 for r in settled if r["exit_reason"] == "stop"), len(settled))
    if lo_hi:
        out["stop_rate_ci"] = [round(lo_hi[0], 4), round(lo_hi[1], 4)]
    for key in ("vix", "implied"):
        rows = [r for r in settled if r.get(f"p_stop_{key}") not in (None, "")]
        if not rows:
            out[f"p_stop_{key}_mean"], out[f"p_stop_{key}_n"] = None, 0
            continue
        out[f"p_stop_{key}_mean"] = round(
            sum(float(r[f"p_stop_{key}"]) for r in rows) / len(rows), 4)
        out[f"p_stop_{key}_n"] = len(rows)
        out[f"p_stop_{key}_stops"] = sum(1 for r in rows if r["exit_reason"] == "stop")
    return out


def upsert(rows, row):
    """Replace the row for this date, or append. An OPEN row is expected to be
    rewritten several times a session as more bars land."""
    out = [r for r in rows if r["date"] != row["date"]]
    out.append(row)
    return out


# ------------------------------------------------------------- overnight

EXPLAINED_NOTE = (
    "Explained near 1.0 means implied is fully accounted for by intraday "
    "movement plus the overnight gap — the premium is compensation for a gap "
    "an intraday seller never carries, and therefore never earns: the option "
    "still prices tonight's gap when they buy it back at 15:15. Well below "
    "1.0 leaves premium that neither explains.")


def overnight_data(underlying, entry=ENTRY, exit_at=EXIT, loaded=None):
    """Does intraday variance plus overnight variance account for implied?

    Returns the data rather than printing it, so the terminal and the API
    report the same numbers from one implementation. `loaded` lets a caller
    that has already read the sessions pass them in -- reading every session
    takes a couple of seconds each and the API has them cached.
    """
    if loaded is None:
        loaded = {}
        for d in V.sessions(underlying):
            bm, exp, err = V.load(d, underlying)
            if not err and max(bm) >= exit_at:
                loaded[d] = (bm, exp)
    ds = sorted(loaded)
    if len(ds) < 3:
        return [], {"reason": "too few complete sessions for the gap study"}

    rows = []
    for prev, cur in zip(ds, ds[1:]):
        pbm, _ = loaded[prev]
        cbm, cexp = loaded[cur]
        pf = V.forward(pbm[max(pbm)])
        cf = V.forward(cbm[min(cbm)])
        if pf is None or cf is None:
            continue
        rich = R.study(cur, cbm, cexp, entry, exit_at)
        if not rich:
            continue
        gap = cf - pf
        intr, imp = rich["rv15"], rich["implied"]
        # Variance adds: total^2 = intraday^2 + gap^2
        tot = (intr ** 2 + gap ** 2) ** 0.5
        rows.append({"date": str(cur), "prev_close": round(pf, 1),
                     "open": round(cf, 1), "gap": round(gap, 1),
                     "intraday_rv": round(intr, 1), "implied": round(imp, 1),
                     "explained": round(tot / imp, 3)})
    if not rows:
        return [], {"reason": "no session pair could be measured"}

    g = [abs(r["gap"]) for r in rows]
    spot = R.med([r["prev_close"] for r in rows]) or 1.0
    return rows, {
        "n": len(rows),
        "median_abs_gap": round(R.med(g), 1),
        "gap_pct_of_spot": round(100 * R.med(g) / spot, 2),
        "median_intraday_rv": round(R.med([r["intraday_rv"] for r in rows]), 1),
        "median_implied": round(R.med([r["implied"] for r in rows]), 1),
        "median_explained": round(R.med([r["explained"] for r in rows]), 3),
        "note": EXPLAINED_NOTE,
    }


def overnight_study(underlying, entry=ENTRY, exit_at=EXIT):
    rows, s = overnight_data(underlying, entry, exit_at)
    if not rows:
        print(f"  {s.get('reason')}")
        return
    print(f"\n  OVERNIGHT GAP STUDY   {s['n'] + 1} complete sessions\n")
    hdr = (f"{'date':<12}{'prev close':>12}{'open':>10}{'gap':>9}"
           f"{'intraday rv':>13}{'implied':>10}{'explained':>11}")
    print(hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r['date']:<12}{r['prev_close']:>12.1f}{r['open']:>10.1f}"
              f"{r['gap']:>+9.1f}{r['intraday_rv']:>13.1f}"
              f"{r['implied']:>10.1f}{r['explained']:>11.2f}")
    print("-" * len(hdr))
    print(f"  median |gap|                {s['median_abs_gap']:.1f} pts")
    print(f"  gap as a share of spot      {s['gap_pct_of_spot']:.2f}%")
    print(f"  median intraday realised    {s['median_intraday_rv']:.1f} pts")
    print(f"  median implied              {s['median_implied']:.1f} pts")
    print(f"  median explained fraction   {s['median_explained']:.2f}")
    print(f"\n  {EXPLAINED_NOTE}\n")


# ------------------------------------------------------------------ main

SAMPLE_FLOOR = 40       # sessions before the MEAN of a skewed payoff means much


def summary_data(rows):
    """The journal's headline numbers, as data. One implementation for the
    terminal and the page, so they can never disagree."""
    settled = [r for r in rows if r["status"] == "settled"]
    out = {"journalled": len(rows), "settled": len(settled),
           "floor": SAMPLE_FLOOR,
           "remaining": max(0, SAMPLE_FLOOR - len(settled))}
    if not settled:
        return out
    nets = [float(r["net_rs"]) for r in settled]
    ratios = [float(r["ratio"]) for r in settled if r["ratio"]]
    out.update({
        "median_net": round(R.med(nets), 0),
        "mean_net": round(sum(nets) / len(nets), 0),
        "total_net": round(sum(nets), 0),
        "wins": sum(1 for v in nets if v > 0),
        "stops": sum(1 for r in settled if r["exit_reason"] == "stop"),
        # Realised stop rate with a Wilson 95% interval, beside the mean of
        # what the model said at entry on the SAME rows. A row with no model
        # number is left out of both sides of that comparison, and the count
        # it was made on is reported with it.
        **stop_calibration(settled),
        # The median and the mean disagreeing IS the finding on a payoff this
        # skewed -- most days pay, a few take it all back -- so both are here
        # and neither is allowed to stand alone.
        "median_ratio": round(R.med(ratios), 3) if ratios else None,
        "ratio_below_one": sum(1 for x in ratios if x < 1) if ratios else None,
        "ratio_n": len(ratios),
    })
    return out


def summarise(rows):
    s = summary_data(rows)
    print(f"\n  {s['journalled']} sessions journalled, {s['settled']} settled")
    if not s["settled"]:
        return
    n = s["settled"]
    print(f"  median net Rs {s['median_net']:,.0f} per lot   "
          f"mean Rs {s['mean_net']:,.0f}   total Rs {s['total_net']:,.0f}")
    print(f"  win rate {s['wins']}/{n} = {100 * s['wins'] / n:.0f}%")
    print(f"  stopped {s['stops']}/{n}")
    if s.get("median_ratio") is not None:
        print(f"  median realised/implied {s['median_ratio']:.2f}   "
              f"{s['ratio_below_one']}/{s['ratio_n']} below 1.0")
    print(f"  toward a sample: {n}/{s['floor']}"
          + ("  — enough to start reading the tail" if not s["remaining"]
             else f"  — {s['remaining']} more before the mean means much"))


def main():
    args = {"--underlying": "NIFTY", "--stop": str(STOP_FRAC), "--target": ""}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]
    und = args["--underlying"]
    stop_frac = float(args["--stop"])
    target_frac = float(args["--target"]) if args["--target"] else None

    if "--overnight" in sys.argv:
        overnight_study(und)
        return 0

    rows = read_journal()
    if "--show" in sys.argv:
        hdr = (f"{'date':<12}{'dte':>4}{'status':>9}{'credit':>9}{'rich':>7}"
               f"{'pct':>6}{'exit':>7}{'pnl pts':>9}{'net Rs':>9}{'ratio':>7}")
        print(hdr + "\n" + "-" * len(hdr))
        # An absent score is a dash, never 0.00 -- "no trailing history yet"
        # and "priced at zero times recent movement" are not the same thing.
        def cell(v, w, p=2):
            return f"{'-':>{w}}" if v in (None, "") else f"{float(v):>{w}.{p}f}"
        for r in rows:
            print(f"{r['date']:<12}{r['dte']:>4}{r['status']:>9}"
                  f"{cell(r['credit'], 9)}{cell(r['richness'], 7)}"
                  f"{(r['richness_pct'] or '-'):>6}{r['exit_reason']:>7}"
                  f"{cell(r['pnl_pts'], 9)}{cell(r['net_rs'], 9, 0)}"
                  f"{cell(r['ratio'], 7)}")
        summarise(rows)
        return 0

    days = V.sessions(und)
    if not days:
        print(f"no sessions under {V.BARS}")
        return 1
    if "--backfill" not in sys.argv:
        days = days[-1:]        # today, or the latest session collected

    # Realised history feeds the richness score and must only ever contain
    # sessions BEFORE the one being scored -- otherwise today's score is
    # computed partly from today, which is not information you had at 09:20.
    history, rich_hist = [], []
    kept = {r["date"]: r for r in rows}
    for d in V.sessions(und):
        bm, exp, err = V.load(d, und)
        if err:
            continue
        if d in days:
            row = evaluate(d, bm, exp, und, history, rich_hist,
                           stop_frac=stop_frac, target_frac=target_frac)
            if row:
                kept[row["date"]] = row
                print(f"  {d}  {row['status']:<8} credit {row['credit']:>7.2f}  "
                      f"richness {row['richness'] or '-':>5}  "
                      f"{row['exit_reason']:>6} @ {row['exit']}  "
                      f"net Rs {row['net_rs']:>8}")
        r = R.study(d, bm, exp, ENTRY, EXIT)
        if r and max(bm) >= EXIT:
            history.append(r["rv15"])
            _, sc = richness_score(r["implied"], history[:-1])
            if sc:
                rich_hist.append(sc)

    path = write_journal(list(kept.values()))
    print(f"\n  journal: {path}")
    summarise(sorted(kept.values(), key=lambda r: r["date"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

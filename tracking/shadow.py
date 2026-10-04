"""Shadow books (agreed 27 Sep 2026): candidate rules tracked on NEW data only.

The ten-year replay (replay.py, replay_tune.py) found no loss-fix that held up
on years it was not designed on. So the candidates are not adopted -- they are
tracked here, forward, beside the live rule, under the same risk rules:

  live   the daily signal as it runs, sized: worst case <= Rs 20,000 a trade,
         <= Rs 60,000 open in total, the best DEFINED-RISK structure with
         positive net EV when the top pick is naked, 5% drawdown (Rs 1 lakh)
         -> no new trades for 10 trading days, held to expiry.
  F4     live, but only when the ATM straddle is >= 1.10 sigma (not 0.9346)
  F1     live, but never a second trade on an expiry already held
  (the fourth candidate, the day-trade without expiry-day sessions, needs no
   book: it is read straight from the day-trade journal)

PROMOTION RULE -- written here so it cannot move later: a candidate replaces
the live rule only after >= 20 settled independent EXPIRIES, and only if it
beats the live book on BOTH total P&L and worst drawdown. Until then it is
information and changes no decision.

Differences from the replay, stated: the drawdown for the breaker is on
SETTLED P&L (the live job does not mark open positions daily), and prices are
the signal's own previous-close mids.

    shadow.record(ctx, structures)   from daily_signal.py, after the journal
    python shadow.py settle          from run_score.sh, after the journal score
    python shadow.py --show
"""
import project_paths
import json, os, sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

ROOT = project_paths.ROOT
DIR = os.path.join(ROOT, "data", "shadow")
BOOK = os.path.join(DIR, "book.csv")
DECISIONS = os.path.join(DIR, "decisions.csv")
STATE = os.path.join(DIR, "state.json")
SUMMARY = os.path.join(DIR, "summary.json")
START = date(2026, 9, 28)

CAP_TRADE, CAP_BOOK, BREAKER, PAUSE_DAYS = 20_000.0, 60_000.0, 100_000.0, 10
LIVE_GATE, F4_GATE, MAX_CREDIT_OF_WING = 0.9346, 1.10, 0.90
BOOKS = {"live": {"gate": LIVE_GATE, "one_per_expiry": False},
         "F4": {"gate": F4_GATE, "one_per_expiry": False},
         "F1": {"gate": LIVE_GATE, "one_per_expiry": True}}
FIELDS = ["book", "entry_date", "expiry", "structure", "top_pick", "swapped", "credit_pts",
          "max_loss_pts", "ce_strike", "pe_strike", "long_ce_strike", "long_pe_strike",
          "lot", "lots", "worst_rs", "cost_rt_rs", "net_ev_rs", "c_over_sigma",
          "status", "settle_level", "pnl_rs", "settled_at"]


# ---------------------------------------------------------------- pure

def usable(s):
    """Defined-risk, every leg priced, and not an arbitrage (see replay.valid)."""
    if not s.defined_risk or s.credit_pts <= 0:
        return False
    if min(s.ce_premium, s.pe_premium, s.long_ce_premium, s.long_pe_premium) <= 0:
        return False                       # strategies.Chain prices a missing strike at 0
    w = s.wing_width
    c_side, p_side = s.ce_premium - s.long_ce_premium, s.pe_premium - s.long_pe_premium
    return 0 < c_side < w and 0 < p_side < w and s.credit_pts <= MAX_CREDIT_OF_WING * w


def choose(structures, cs, gate):
    """(structure, top_pick_name, swapped, reason) -- or (None, ..., reason)."""
    if cs is None or cs < gate:
        return None, None, False, f"gate: credit/sigma {cs:.3f} < {gate}" if cs is not None else "gate: no c/σ"
    top = max(structures.values(), key=lambda s: s.net_ev_rs)
    if top.net_ev_rs <= 0:
        return None, top.name, False, "no positive net EV"
    if usable(top):
        return top, top.name, False, "ok"
    dr = [s for s in structures.values() if usable(s) and s.net_ev_rs > 0]
    if not dr:
        return None, top.name, False, "top pick naked; no usable defined-risk structure with +EV"
    return max(dr, key=lambda s: s.net_ev_rs), top.name, True, "ok (swapped to defined risk)"


def size(s):
    """(lots, worst_rs_per_lot, cost_rt_rs_per_lot)."""
    cost = (s.fees_rs + s.slippage_rs) / max(s.lots, 1)
    worst = s.max_loss_pts * s.lot_size + cost
    return (int(CAP_TRADE // worst) if worst > 0 else 0), worst, cost


def intrinsic(level, k, kind):
    return max(0.0, level - k) if kind == "CE" else max(0.0, k - level)


def settle_row(r, level):
    pay = (intrinsic(level, r["ce_strike"], "CE") + intrinsic(level, r["pe_strike"], "PE")
           - intrinsic(level, r["long_ce_strike"], "CE") - intrinsic(level, r["long_pe_strike"], "PE"))
    units = float(r["lot"]) * float(r["lots"])
    # held to expiry: no closing order, so half the round-trip cost
    return (float(r["credit_pts"]) - pay) * units - float(r["cost_rt_rs"]) * float(r["lots"]) / 2


def drawdown(pnls):
    peak = eq = dd = 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); dd = max(dd, peak - eq)
    return dd


# ------------------------------------------------------------------ io

def load_book():
    if not os.path.exists(BOOK):
        return pd.DataFrame(columns=FIELDS)
    return pd.read_csv(BOOK, dtype={"status": object, "settled_at": object,
                                    "expiry": object, "entry_date": object})


def save_book(df):
    os.makedirs(DIR, exist_ok=True)
    df[FIELDS].to_csv(BOOK, index=False)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {b: {"peak": 0.0, "paused_until": None} for b in BOOKS}


def log_decision(rows):
    os.makedirs(DIR, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(DECISIONS, mode="a", header=not os.path.exists(DECISIONS), index=False)


def record(ctx, structures, write=True, today=None):
    """Called by daily_signal.py after the journal. One decision per book."""
    today = today or ctx["today"]
    if today < START:
        return []
    book, state = load_book(), load_state()
    cs, exp = ctx.get("c_over_sigma"), ctx["expiry"]
    new, decisions = [], []
    for name, cfg in BOOKS.items():
        st = state.setdefault(name, {"peak": 0.0, "paused_until": None})
        mine = book[book.book == name]
        open_ = mine[mine.status == "open"]
        s, top, swapped, why = choose(structures, cs, cfg["gate"])
        action = "skip"
        if st.get("paused_until") and str(today) < st["paused_until"]:
            why = f"circuit breaker pause until {st['paused_until']}"
            s = None
        if s is not None and cfg["one_per_expiry"] and (open_.expiry.astype(str) == str(exp)).any():
            s, why = None, "F1: this expiry already held"
        if s is not None:
            lots, worst, cost = size(s)
            held = float((open_.worst_rs * open_.lots).sum()) if len(open_) else 0.0
            if lots < 1:
                why = f"one lot's worst case Rs {worst:,.0f} > Rs {CAP_TRADE:,.0f}"
            elif held + lots * worst > CAP_BOOK:
                why = f"book cap: Rs {held:,.0f} open + Rs {lots * worst:,.0f} > Rs {CAP_BOOK:,.0f}"
            else:
                action = "trade"
                new.append({"book": name, "entry_date": str(today), "expiry": str(exp),
                            "structure": s.name, "top_pick": top, "swapped": swapped,
                            "credit_pts": round(s.credit_pts, 2), "max_loss_pts": round(s.max_loss_pts, 2),
                            "ce_strike": s.ce_strike, "pe_strike": s.pe_strike,
                            "long_ce_strike": s.long_ce_strike, "long_pe_strike": s.long_pe_strike,
                            "lot": s.lot_size, "lots": lots, "worst_rs": round(worst, 0),
                            "cost_rt_rs": round(cost, 2), "net_ev_rs": round(s.net_ev_rs, 0),
                            "c_over_sigma": round(cs, 4), "status": "open"})
                why = f"{s.name} x{lots}" + (" (swapped from " + top + ")" if swapped else "")
        decisions.append({"date": str(today), "book": name, "action": action, "reason": why})
    if write:
        if new:
            save_book(pd.concat([book, pd.DataFrame(new)], ignore_index=True) if len(book) else pd.DataFrame(new, columns=FIELDS))
        log_decision(decisions)
    for d in decisions:
        print(f"shadow {d['book']:5s} {d['action']:5s} {d['reason']}")
    return decisions


def trading_days_after(d, n):
    try:
        import market
        is_td = market.is_trading_day
    except Exception:  # noqa: BLE001
        is_td = lambda x: x.weekday() < 5
    x, k = d, 0
    while k < n:
        x += timedelta(days=1)
        if is_td(x):
            k += 1
    return x


def settle(today=None, level_fn=None):
    today = today or datetime.now(timezone.utc).date()
    book, state = load_book(), load_state()
    if book.empty:
        print("shadow: nothing to settle"); write_summary(book); return []
    if level_fn is None:
        import journal
        level_fn = lambda e: journal.settlement(e)[0]
    done = []
    for i, r in book[(book.status == "open")].iterrows():
        e = pd.to_datetime(r.expiry).date()
        if e >= today:
            continue
        lvl = level_fn(e)
        if lvl is None:
            print(f"shadow: no settlement level for {e} yet"); continue
        pnl = settle_row(r, lvl)
        book.loc[i, ["status", "settle_level", "pnl_rs", "settled_at"]] = ["settled", round(lvl, 2), round(pnl, 0), str(today)]
        done.append((r.book, str(e), round(pnl)))
    for name in BOOKS:
        st = state.setdefault(name, {"peak": 0.0, "paused_until": None})
        s = book[(book.book == name) & (book.status == "settled")].sort_values("settled_at")
        eq = float(s.pnl_rs.sum())
        st["peak"] = max(st.get("peak", 0.0), eq)
        paused = st.get("paused_until") and str(today) < st["paused_until"]
        if not paused and st["peak"] - eq >= BREAKER:
            st["paused_until"] = str(trading_days_after(today, PAUSE_DAYS))
            st["peak"] = eq                      # the clock restarts after the pause
            print(f"shadow {name}: CIRCUIT BREAKER -- paused until {st['paused_until']}")
    save_book(book)
    os.makedirs(DIR, exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=1)
    write_summary(book, done)
    for b, e, p in done:
        print(f"shadow {b}: {e} settled, Rs {p:+,}")
    return done


def daytrade_candidate():
    """Day-trade with and without expiry-day sessions, from START on."""
    try:
        import daytrade as DT
        rows = [r for r in DT.read_journal() if str(r.get("date")) >= str(START)
                and r.get("status") == "settled" and r.get("underlying", "NIFTY") == "NIFTY"]
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    def f(x):
        try: return float(x)
        except (TypeError, ValueError): return 0.0
    allp = [f(r.get("net_rs")) for r in rows]
    ex0 = [f(r.get("net_rs")) for r in rows if str(r.get("dte")) not in ("0", "0.0")]
    return {"sessions": len(allp), "net_all": round(sum(allp)), "sessions_no_expiry_day": len(ex0),
            "net_no_expiry_day": round(sum(ex0))}


def write_summary(book, settled_today=()):
    out = {"since": str(START), "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "promotion_rule": ">= 20 settled expiries AND beats live on total P&L AND worst drawdown",
           "books": {}, "settled_today": [list(x) for x in settled_today]}
    try:
        st = load_state()
    except Exception:  # noqa: BLE001
        st = {}
    for name in BOOKS:
        b = book[book.book == name] if len(book) else book
        s = b[b.status == "settled"].sort_values("settled_at") if len(b) else b
        out["books"][name] = {"trades": int(len(b)), "open": int((b.status == "open").sum()) if len(b) else 0,
                              "settled": int(len(s)), "expiries": int(s.expiry.nunique()) if len(s) else 0,
                              "pnl": round(float(s.pnl_rs.sum()), 0) if len(s) else 0.0,
                              "worst": round(float(s.pnl_rs.min()), 0) if len(s) else None,
                              "max_dd": round(drawdown(list(s.pnl_rs)), 0) if len(s) else 0.0,
                              "paused_until": (st.get(name) or {}).get("paused_until")}
    out["daytrade"] = daytrade_candidate()
    os.makedirs(DIR, exist_ok=True)
    json.dump(out, open(SUMMARY, "w"), indent=1, default=str)
    return out


if __name__ == "__main__":
    if "settle" in sys.argv:
        settle()
    else:
        print(json.dumps(json.load(open(SUMMARY)), indent=1) if os.path.exists(SUMMARY) else "no summary yet")

"""The Rs 20 lakh paper book: the trading agent's first step.

Every trading day at ENTRY (10:15 IST), for NIFTY and BANKNIFTY, it takes the
top strategy card from the Options page (quant/regime.py via
/api/options_view). When the view is unclear it takes the leading side's card
anyway and marks the trade `weak`. The same entry is logged in two books that
differ only in how they exit:

    intraday  target / stop, else closed at 15:15 the same day
    hold      target / stop, else held and closed at 15:15 on expiry day

Target: half the max profit (or 1 x the max loss when profit is uncapped).
Stop: half the max loss. Marks use the chain's mid prices every minute; costs
are the catalogue's estimated fees + slippage for the round trip.

Nothing here places an order. State is one JSON file, data/paper_book.json.
step() is cheap and safe to call every minute: it never raises, and a failed
price lookup just skips that mark.
"""
import json
import os
from datetime import date, datetime, time as dtime, timedelta, timezone

import project_paths

IST = timezone(timedelta(hours=5, minutes=30))
OUT = os.path.join(project_paths.ROOT, "data", "paper_book.json")
CAPITAL = 2_000_000
BOOKS = ("intraday", "hold")
INDICES = ("NIFTY", "BANKNIFTY")
ENTRY = dtime(10, 15)
LAST_ENTRY = dtime(14, 30)        # a missed 10:15 (restart, no data) may still enter until here
CLOSE = dtime(15, 15)
TARGET_OF_MAX_PROFIT = 0.5
STOP_OF_MAX_LOSS = 0.5


# ------------------------------------------------------------------ state

def empty_state():
    return {"capital": CAPITAL, "trades": [], "skips": [], "next_id": 1}


def load(path=None):
    try:
        st = json.load(open(path or OUT))
        st.setdefault("skips", [])
        return st
    except (OSError, ValueError):
        return empty_state()


def save(st, path=None):
    path = path or OUT
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(st, open(tmp, "w"), indent=0)
    os.replace(tmp, path)


# ------------------------------------------------------------------ rules

def choose(index_view):
    """The card to trade and whether the lead was weak, or (None, reason)."""
    if not index_view or not index_view.get("available"):
        return None, (index_view or {}).get("reason", "no view")
    weak = index_view.get("call") == "unclear"
    cards = index_view.get("lean_strategies") if weak else index_view.get("strategies")
    for c in cards or []:
        if (c.get("sizing") or {}).get("lots", 0) >= 1 and c.get("legs"):
            return dict(c, weak=weak), None
    return None, "no strategy card could be sized within the risk limit"


def open_trade(st, book, und, card, view, now):
    lots = card["sizing"]["lots"]
    ml = abs(card.get("max_loss_rs") or 0) * lots
    mp = card.get("max_profit_rs")
    target = TARGET_OF_MAX_PROFIT * mp * lots if mp is not None else ml
    legs = [{"strike": l["strike"], "kind": l["kind"], "expiry": str(l["expiry"]),
             "qty": l["qty"], "entry": l["premium"]} for l in card["legs"]]
    t = {"id": st["next_id"], "book": book, "underlying": und, "date": now.date().isoformat(),
         "opened": now.isoformat(timespec="minutes"), "key": card["key"], "label": card["label"],
         "view": view.get("call"), "lean": view.get("lean"), "odds": view.get("odds"),
         "weak": bool(card.get("weak")), "vol": (view.get("vol") or {}).get("state"),
         "lots": lots, "lot_size": view.get("lot"), "legs": legs,
         "max_loss_rs": round(ml), "max_profit_rs": None if mp is None else round(mp * lots),
         "target_rs": round(target), "stop_rs": round(-STOP_OF_MAX_LOSS * ml),
         "costs_rs": round((card.get("costs_rs") or 0) * lots),
         "expiry": min(l["expiry"] for l in legs),
         "status": "open", "mark": None, "closed": None, "exit_reason": None, "pnl_rs": None}
    st["next_id"] += 1
    st["trades"].append(t)
    return t


def mark_pnl(trade, price):
    """Gross P&L in rupees at current mids; None if any leg has no price."""
    pnl = 0.0
    lot = trade.get("lot_size") or 1
    for l in trade["legs"]:
        px = price(trade["underlying"], l["expiry"], l["strike"], l["kind"])
        if px is None:
            return None
        pnl += (px - l["entry"]) * l["qty"] * lot * trade["lots"]
    return pnl


def exit_reason(trade, gross, now):
    if gross is not None:
        if gross >= trade["target_rs"]:
            return "target"
        if gross <= trade["stop_rs"]:
            return "stop"
    today = now.date().isoformat()
    if trade["book"] == "intraday" and trade["date"] < today:
        return "time (closed late: missed 15:15)"
    if trade["book"] == "hold" and trade["expiry"] < today:
        return "expiry (closed late)"
    if now.time() >= CLOSE:
        if trade["book"] == "intraday" and trade["date"] <= now.date().isoformat():
            return "time (15:15)"
        if trade["book"] == "hold" and trade["expiry"] <= now.date().isoformat():
            return "expiry"
    return None


def close_trade(trade, gross, now, reason):
    trade["status"] = "closed"
    trade["closed"] = now.isoformat(timespec="minutes")
    trade["exit_reason"] = reason
    trade["pnl_gross_rs"] = round(gross)
    trade["pnl_rs"] = round(gross - trade["costs_rs"])


def step(st, now, views, price, is_trading_day):
    """One minute of the agent. views: /api/options_view payload (or a callable
    returning it, only called when an entry is due). price(und, expiry, strike,
    kind) -> mid or None. Returns the list of things that happened."""
    events = []
    today = now.date().isoformat()
    if not is_trading_day(now.date()):
        return events
    # 1. mark and exit
    for t in st["trades"]:
        if t["status"] != "open":
            continue
        if now.time() < dtime(9, 16):
            continue
        g = mark_pnl(t, price)
        if g is not None:
            t["mark"] = {"at": now.isoformat(timespec="minutes"), "pnl_rs": round(g - t["costs_rs"])}
        why = exit_reason(t, g, now)
        if why:
            if g is None and t["mark"]:          # no fresh price at the bell: last mark
                g = t["mark"]["pnl_rs"] + t["costs_rs"]
            if g is not None:
                close_trade(t, g, now, why)
                events.append(f"closed {t['book']} {t['underlying']} {t['label']}: {why}, {t['pnl_rs']:+,}")
    # 2. enter
    if not (ENTRY <= now.time() <= LAST_ENTRY):
        return events
    due = [u for u in INDICES
           if not any(t["underlying"] == u and t["date"] == today for t in st["trades"])
           and not any(s["underlying"] == u and s["date"] == today and s.get("final") for s in st["skips"])]
    if not due:
        return events
    v = views() if callable(views) else views
    for u in due:
        iv = ((v or {}).get("indices") or {}).get(u)
        card, why = choose(iv)
        if not card:
            final = now.time() >= LAST_ENTRY
            st["skips"] = [s for s in st["skips"] if not (s["underlying"] == u and s["date"] == today)]
            st["skips"].append({"date": today, "underlying": u, "reason": why, "final": final})
            continue
        st["skips"] = [s for s in st["skips"] if not (s["underlying"] == u and s["date"] == today)]
        for b in BOOKS:
            # one open trade per index per book: a 'hold' trade still running blocks a new one
            if any(t["book"] == b and t["underlying"] == u and t["status"] == "open" for t in st["trades"]):
                events.append(f"{b} {u}: previous trade still open, no new entry")
                continue
            t = open_trade(st, b, u, card, iv, now)
            events.append(f"opened {b} {u} {t['label']} x{t['lots']}{' (weak lead)' if t['weak'] else ''}")
    return events


# ------------------------------------------------------------------ summary

def book_summary(st, book, recent=30):
    ts = [t for t in st["trades"] if t["book"] == book]
    closed = sorted([t for t in ts if t["status"] == "closed"], key=lambda t: t["closed"])
    open_ = [t for t in ts if t["status"] == "open"]
    realised = sum(t["pnl_rs"] for t in closed)
    open_pnl = sum((t["mark"] or {}).get("pnl_rs", 0) for t in open_)
    eq, peak, dd, curve = st["capital"], st["capital"], 0.0, []
    for t in closed:
        eq += t["pnl_rs"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        curve.append([t["closed"][:10], round(eq)])
    wins = [t for t in closed if t["pnl_rs"] > 0]
    by = {}
    for t in closed:
        b = by.setdefault(t["label"], {"trades": 0, "pnl_rs": 0, "wins": 0})
        b["trades"] += 1
        b["pnl_rs"] += t["pnl_rs"]
        b["wins"] += t["pnl_rs"] > 0
    return {"book": book, "capital": st["capital"],
            "equity": round(st["capital"] + realised + open_pnl),
            "realised_rs": round(realised), "open_pnl_rs": round(open_pnl),
            "trades": len(closed), "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "max_drawdown_rs": round(dd), "curve": curve,
            "by_strategy": [{"label": k, **v} for k, v in sorted(by.items(), key=lambda kv: -kv[1]["pnl_rs"])],
            "open": open_, "recent": list(reversed(closed))[:recent]}


def summary(st, now=None):
    now = now or datetime.now(IST)
    today = now.date().isoformat()
    return {"generated": now.isoformat(timespec="seconds"), "capital": st["capital"],
            "rules": {"entry": ENTRY.strftime("%H:%M"), "close": CLOSE.strftime("%H:%M"),
                      "target": "50% of max profit (1x max loss if profit uncapped)",
                      "stop": "50% of max loss", "risk_per_trade": "1% of the book per index"},
            "books": {b: book_summary(st, b) for b in BOOKS},
            "skips_today": [s for s in st["skips"] if s["date"] == today]}

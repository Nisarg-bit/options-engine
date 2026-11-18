"""End-of-day summary and Telegram alerts for the stock-futures paper system.

Both read the durable event log, so they are restart-safe:
  * end_of_day(): once per session, after the close (control key eod:<date>),
    builds the day's report and stores it as a DailySummary event.
  * Notifier: a consumer of the event log ("notifier"). Each event is claimed
    in its own transaction BEFORE the message is sent, so a crash can drop an
    alert but never send it twice. A failed send is logged and never retried,
    and nothing here can stop trading (notify.send never raises).
"""
import html
import json
from collections import Counter
from datetime import timedelta

from sf_core import LONG, MODES, iso, ist, parse_ts

ALERT_TYPES = ("PositionOpened", "PositionClosed", "ExitUnresolved", "OverrideApplied", "DailySummary", "WorkerStarted")


def rs(x):
    return "—" if x is None else ("−" if x < 0 else "+") + "₹" + f"{abs(x):,.0f}"


# ====================================================================== daily report

def day_report(store, day):
    d = str(day)
    scans = store.q("SELECT * FROM scans WHERE substr(started,1,10)=?", d)
    stops = [e for e in store.events("type='ScanStopped' AND substr(event_time,1,10)=?", d)]
    regimes = store.events("type='RegimeClassified' AND substr(event_time,1,10)=?", d)
    trades = store.trades()
    opened = [t["body"] for t in trades if (t["body"].get("opened_at") or "")[:10] == d]
    closed = [t["body"] for t in trades if (t["body"].get("closed_at") or "")[:10] == d and t["status"] == "CLOSED"]
    unresolved = [t["body"] for t in trades if t["status"] in ("EXITING", "UNRESOLVED") and t["body"].get("unresolved")]
    still_open = [t["body"] for t in trades if t["status"] == "OPEN"]
    by = {}
    for t in closed:
        r = t["result"]
        k = f"{t['mode']}:{t['direction']}"
        b = by.setdefault(k, {"trades": 0, "gross": 0.0, "costs": 0.0, "net": 0.0, "wins": 0})
        b["trades"] += 1
        b["gross"] += r["gross_pnl"]
        b["costs"] += r["costs"]
        b["net"] += r["net_pnl"]
        b["wins"] += r["net_pnl"] > 0
    reasons = Counter()
    for e in stops:
        reason = e["payload"]["reason"]
        reasons[f"{e['mode']}: {e['payload']['stage']}: {reason.split('(')[0].strip()[:90]}"] += 1
    reg = {m: dict(Counter(e["payload"]["regime"] for e in regimes if e["mode"] == m)) for m in MODES}
    out = {"session": d, "scans": {m: sum(1 for s in scans if s["mode"] == m) for m in MODES},
           "regimes": reg, "opened": len(opened), "closed": len(closed), "open_overnight": len(still_open),
           "unresolved": [{"trade_id": t["trade_id"], "reason": t["unresolved"]["reason"]} for t in unresolved],
           "by_mode_direction": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                                 for k, v in by.items()},
           "net": round(sum(b["net"] for b in by.values()), 2), "costs": round(sum(b["costs"] for b in by.values()), 2),
           "top_no_trade_reasons": reasons.most_common(4),
           "trades": [{"symbol": t["underlying"], "mode": t["mode"], "direction": t["direction"],
                       "net": t["result"]["net_pnl"], "reason": t["exit"]["reason"]} for t in closed]}
    out["text"] = report_text(out)
    return out


def report_text(r):
    e = html.escape
    lines = [f"<b>Stock futures (paper) · {e(r['session'])}</b>",
             f"Scans: intraday {r['scans'].get('intraday', 0)}, swing {r['scans'].get('swing', 0)}",
             f"Trades: {r['opened']} opened, {r['closed']} closed, {r['open_overnight']} open now",
             f"Net P&amp;L: <b>{e(rs(r['net']))}</b> (costs {e(rs(r['costs']))})"]
    for k, b in sorted(r["by_mode_direction"].items()):
        lines.append(f"  {e(k)}: {b['trades']} trades, {b['wins']} wins, net {e(rs(b['net']))}")
    for t in r["trades"][:6]:
        lines.append(f"  {e(t['symbol'])} {e(t['mode'])} {e(t['direction'])}: {e(rs(t['net']))} ({e(t['reason'])})")
    if r["unresolved"]:
        lines.append(f"<b>Unresolved exits: {len(r['unresolved'])}</b>")
        lines += [f"  {e(u['trade_id'])}: {e(u['reason'][:80])}" for u in r["unresolved"][:4]]
    for m, c in r["regimes"].items():
        if c:
            lines.append(f"Market ({e(m)}): " + ", ".join(f"{e(k)} {v}" for k, v in sorted(c.items())))
    if r["top_no_trade_reasons"]:
        lines.append("Most common no-trade reasons:")
        lines += [f"  {n}× {e(k)}" for k, n in r["top_no_trade_reasons"]]
    lines.append("<i>Paper trading only. Not investment advice.</i>")
    return "\n".join(lines)


def end_of_day(engine, now, after="15:35"):
    """Once per session, after the close: store the day's report as an event."""
    day = now.date()
    if not engine.cal.is_session(day) or now < ist(day, after):
        return None
    key = f"eod:{day}"
    if engine.store.control(key):
        return None
    rep = day_report(engine.store, day)
    with engine.store.tx() as c:
        if c.execute("SELECT 1 FROM control WHERE key=?", (key,)).fetchone():
            return None
        c.execute("INSERT INTO control(key,value,updated,actor) VALUES(?,?,?,?)", (key, "1", iso(now), "eod"))
        engine.store.emit(c, "DailySummary", "eod-report", now, rep, event_id=f"eod:{day}")
    return rep


# ====================================================================== alerts

def message(ev):
    """Telegram text for one event, or None."""
    e, p, t = html.escape, ev["payload"], ev["type"]
    tag = f"[{e(ev.get('mode') or '')} {e(ev.get('direction') or '')}]".replace("[ ]", "")
    if t == "PositionOpened":
        f = p.get("first_fill") or {}
        side = p.get("entry_side")
        return (f"📄 <b>Paper {('LONG' if ev.get('direction') == LONG else 'SHORT')} opened</b> {tag}\n"
                f"{e(side or '')} {p.get('units')} units ({p.get('lots')} lot) @ {f.get('price')}\n"
                f"trade {e(ev.get('trade_id') or '')}")
    if t == "PositionClosed":
        r = p.get("result") or {}
        if not r:
            return f"📄 Paper trade {e(ev.get('trade_id') or '')} {tag} ended without a fill ({e(p.get('reason') or '')})"
        exc = f"\n⚠️ data exception: {e(str((p.get('exception') or {}).get('reason', ''))[:80])}" if p.get("exception") else ""
        return (f"📄 <b>Paper trade closed</b> {tag}: <b>{e(rs(r.get('net_pnl')))}</b> net\n"
                f"{e(r.get('entry_side', ''))} {r.get('entry_avg')} → {e(r.get('exit_side', ''))} {r.get('exit_avg')} "
                f"× {r.get('closed_units')} · {e(p.get('reason') or '')} · costs {e(rs(r.get('costs')))}{exc}")
    if t == "ExitUnresolved":
        return f"⚠️ <b>Unresolved paper exit</b> {tag}\n{e(ev.get('trade_id') or '')}\n{e(p.get('reason', '')[:200])}"
    if t == "OverrideApplied":
        return f"✏️ Exit plan override {tag} by {e(p.get('actor', ''))}: {e(json.dumps(p.get('edits')))}"
    if t == "DailySummary":
        return p.get("text")
    if t == "WorkerStarted":
        return (f"🟢 Stock-futures worker started ({e(p.get('worker', ''))}).\n"
                f"Entries: {e(p.get('gate') or 'ENABLED (paper)')}")
    return None


class Notifier:
    name = "notifier"

    def __init__(self, store, send=None, since=None):
        self.store = store
        if send is None:
            import notify
            send = notify.send
        self.send = send
        self.since = since          # ignore events older than this (no backlog flood on first start)

    def poll(self, now):
        sent = 0
        for ev in self.store.pending(self.name, ALERT_TYPES, limit=50):
            with self.store.tx() as c:
                if not self.store.claim(c, self.name, ev["event_id"], now):
                    continue
            if self.since and parse_ts(ev["event_time"]) < self.since:
                continue
            text = message(ev)
            if text:
                ok = self.send(text, silent=ev["type"] in ("DailySummary", "WorkerStarted"))
                if not ok:
                    print(f"[sf] alert not delivered: {ev['type']} {ev.get('trade_id')}")
                sent += 1
        return sent

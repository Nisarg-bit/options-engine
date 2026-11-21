"""Read-only audit of the stock-futures paper system: the ledger, the scan funnel
and the data it decided on. Opens the SQLite store with mode=ro and never writes.

    python sf_audit.py ledger  [--db PATH]          trades, orders, fills, P&L checks
    python sf_audit.py funnel  [--db PATH] [--day D] where scans stopped and why
    python sf_audit.py data    [--day D]             bar coverage / first-bar defect (repo data)
    python sf_audit.py all     [--db PATH] [--day D] --json   machine-readable, for the daily job

Every check returns findings as {"check", "status": OK|WARN|FAIL, "detail"} so the
daily evaluation can store them as data-quality metrics.
"""
import argparse
import collections
import glob
import json
import os
import sqlite3
import sys

import project_paths
from sf_core import LONG, SHORT, gross_pnl, open_side, close_side

DB = os.path.join(project_paths.ROOT, "data", "futures", "futures.db")
STOCKS = os.path.join(project_paths.ROOT, "data", "stocks_1m")


def ro(path=DB):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _body(r):
    try:
        return json.loads(r["body"]) if r["body"] else {}
    except (TypeError, ValueError):
        return {}


# ------------------------------------------------------------------ ledger

def ledger(c):
    """Every trade with its orders and fills, plus accounting checks."""
    out, findings = [], []
    trades = [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY COALESCE(opened, updated)")]
    for tr in trades:
        t = _body(tr)
        fills = [dict(r, body=_body(r)) for r in c.execute("SELECT * FROM fills WHERE trade_id=? ORDER BY ts", (tr["trade_id"],))]
        orders = [dict(r, body=_body(r)) for r in c.execute("SELECT * FROM orders WHERE trade_id=? ORDER BY created", (tr["trade_id"],))]
        d = t.get("direction")
        o_f = [f for f in fills if f["purpose"] == "OPEN"]
        c_f = [f for f in fills if f["purpose"] == "CLOSE"]
        row = {"trade_id": tr["trade_id"], "mode": t.get("mode"), "direction": d, "status": t.get("status"),
               "underlying": t.get("underlying"), "contract": t.get("tradingsymbol"), "lot_size": t.get("lot_size"),
               "lots": t.get("lots"), "config_version": t.get("config_version"), "created_at": t.get("created_at"),
               "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
               "exit_reason": (t.get("exit") or {}).get("reason"), "result": t.get("result"),
               "unresolved": t.get("unresolved"), "orders": len(orders), "fills": len(fills)}
        out.append(row)
        tid = tr["trade_id"]
        # side vocabulary
        for f in o_f:
            if f["side"] != open_side(d):
                findings.append(_f("open side", "FAIL", f"{tid}: {d} entry filled {f['side']}"))
        for f in c_f:
            if f["side"] != close_side(d):
                findings.append(_f("close side", "FAIL", f"{tid}: {d} exit filled {f['side']}"))
        q_open, q_close = sum(f["qty"] for f in o_f), sum(f["qty"] for f in c_f)
        if q_close > q_open:
            findings.append(_f("over-close", "FAIL", f"{tid}: closed {q_close} > opened {q_open}"))
        if t.get("lots") not in (None, 1) or (t.get("lot_size") and q_open > t["lot_size"]):
            findings.append(_f("one lot", "FAIL", f"{tid}: {q_open} units vs lot {t.get('lot_size')}"))
        if len([o for o in orders if o["purpose"] == "OPEN"]) > 1:
            findings.append(_f("duplicate entry orders", "FAIL", f"{tid}: more than one OPEN order"))
        res = t.get("result") or {}
        if t.get("status") == "CLOSED" and res:
            e = sum(f["qty"] * f["price"] for f in o_f) / q_open if q_open else None
            x = sum(f["qty"] * f["price"] for f in c_f) / q_close if q_close else None
            g = gross_pnl(d, e, x, q_close) if e and x else None
            costs = sum((f["body"].get("costs") or {}).get("total", 0) for f in fills)
            if g is None or abs(g - res.get("gross_pnl", 0)) > 0.01 or abs(costs - res.get("costs", 0)) > 0.01 \
                    or abs(res.get("gross_pnl", 0) - res.get("costs", 0) - res.get("net_pnl", 0)) > 0.01:
                findings.append(_f("P&L recompute", "FAIL", f"{tid}: stored {res} vs recomputed gross {g} costs {costs}"))
        for f in fills:
            ev = f["body"].get("evidence") or {}
            q = ev.get("quote") or ev                      # quote fills carry bid/ask in the evidence itself
            if ev.get("model", "").startswith("quote"):
                if not (q.get("bid") and q.get("ask")):
                    findings.append(_f("fill without quote", "FAIL", f"{f['fill_id']}: no two-sided quote in evidence"))
                else:
                    ref = q["ask"] if f["side"] == "BUY" else q["bid"]
                    worse = f["price"] >= ref if f["side"] == "BUY" else f["price"] <= ref
                    if not worse:
                        findings.append(_f("fill better than quote", "FAIL",
                                           f"{f['fill_id']}: {f['side']} at {f['price']} vs quote {ref}"))
    open_res = [dict(r) for r in c.execute("SELECT * FROM reservations")]
    live = {r["trade_id"] for r in out if r["status"] in ("OPEN", "EXITING", "PENDING_ENTRY", "PENDING_APPROVAL")}
    for r in open_res:
        if r["trade_id"] not in live:
            findings.append(_f("orphan reservation", "FAIL", f"{r['underlying']} held by {r['trade_id']} with no live trade"))
    for tid in live - {r["trade_id"] for r in open_res}:
        findings.append(_f("missing reservation", "FAIL", f"live trade {tid} has no reservation"))
    if not any(x["status"] == "FAIL" for x in findings):
        findings.append(_f("ledger accounting", "OK", f"{len(out)} trades: sides, quantities, one lot, P&L and costs recompute"))
    return out, findings


def _f(check, status, detail):
    return {"check": check, "status": status, "detail": detail}


# ------------------------------------------------------------------ funnel

def funnel(c, day=None):
    """Per mode: scans, where they stopped, and the most common reasons."""
    where = "WHERE e.event_time >= ?" if day else ""
    args = (str(day),) if day else ()
    if day:
        where += " AND e.event_time < ?"
        args = (str(day), str(day) + "T99")
    rows = c.execute(f"SELECT e.type, e.mode, e.payload FROM events e {where}", args).fetchall()
    out = collections.defaultdict(lambda: {"scans": 0, "regimes": collections.Counter(), "stops": collections.Counter(),
                                           "reasons": collections.Counter(), "entries": 0, "rejected": collections.Counter()})
    for r in rows:
        m = r["mode"] or "-"
        p = json.loads(r["payload"]) if r["payload"] else {}
        if r["type"] == "ScanRequested":
            out[m]["scans"] += 1
        elif r["type"] == "RegimeClassified":
            out[m]["regimes"][p.get("regime")] += 1
        elif r["type"] == "ScanStopped":
            out[m]["stops"][p.get("stage")] += 1
            out[m]["reasons"][_bucket(p.get("reason", ""))] += 1
        elif r["type"] == "EntryOrderSubmitted":
            out[m]["entries"] += 1
        elif r["type"] == "CandidateRejected":
            out[m]["rejected"][_bucket(p.get("reason", ""))] += 1
    return {m: {k: (dict(v.most_common(12)) if isinstance(v, collections.Counter) else v) for k, v in d.items()}
            for m, d in out.items()}


def _bucket(reason):
    r = reason or ""
    for key in ("opening-range stop", "already has an active", "open-position limit", "notional", "outside the",
                "no sector qualified", "no stock in the qualifying", "no direction permitted", "spread",
                "snapshot stale", "no candidate could be entered", "scan expired", "feeds not ready", "paused"):
        if key in r:
            return key
    return r[:70]


# ------------------------------------------------------------------ data

def data_quality(day, root=STOCKS):
    """Bar coverage per tier and the collector's first-bar defect: the session's
    first 1-minute bar opens at yesterday's close (bars are chained), so its
    high or low can be a price that never traded today."""
    import pandas as pd
    findings, cover = [], {}
    for tier in ("EQ", "STKFUT", "IDXFUT"):
        files = sorted(glob.glob(os.path.join(root, f"date={day}", f"underlying={tier}", "*.parquet")))
        if not files:
            findings.append(_f(f"{tier} bars", "FAIL", f"no {tier} parts for {day}"))
            continue
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        n_sym = df["underlying"].nunique()
        mins = df.groupby("underlying")["ts"].nunique()
        dup = int(df.duplicated(["underlying", "ts"]).sum())
        zero_vol = float((df["volume"].fillna(0) <= 0).mean())
        interp = float(df["is_interpolated"].mean()) if "is_interpolated" in df else 0.0
        bad = int(((df["high"] < df["low"]) | (df["close"] <= 0)).sum())
        cover[tier] = {"parts": len(files), "symbols": int(n_sym), "median_minutes": int(mins.median()),
                       "min_minutes": int(mins.min()), "duplicates": dup, "zero_volume_share": round(zero_vol, 4),
                       "interpolated_share": round(interp, 4), "invalid_bars": bad}
        if dup:
            findings.append(_f(f"{tier} duplicates", "WARN", f"{dup} duplicate (symbol, minute) rows"))
        if bad:
            findings.append(_f(f"{tier} invalid bars", "FAIL", f"{bad} bars with high<low or close<=0"))
        first = df.sort_values("ts").groupby("underlying").head(1)
        synth = first[(first["high"] == first["open"]) | (first["low"] == first["open"])]
        share = len(synth) / max(len(first), 1)
        cover[tier]["first_bar_open_at_extreme_share"] = round(share, 3)
        if tier in ("EQ", "IDXFUT") and share > 0.2:
            findings.append(_f(f"{tier} first-bar defect", "FAIL",
                               f"{share:.0%} of symbols' 09:15 bar has its high or low equal to its open "
                               "(the open is chained from yesterday's close), which widens opening ranges"))
    return cover, findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["ledger", "funnel", "data", "all"])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--day")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = {}
    if a.what in ("ledger", "all"):
        with ro(a.db) as c:
            res["ledger"], res["ledger_findings"] = ledger(c)
    if a.what in ("funnel", "all"):
        with ro(a.db) as c:
            res["funnel"] = funnel(c, a.day)
    if a.what in ("data", "all") and a.day:
        res["data"], res["data_findings"] = data_quality(a.day)
    json.dump(res, sys.stdout, indent=None if a.json else 1, default=str)
    print()


if __name__ == "__main__":
    main()

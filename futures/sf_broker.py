"""Paper execution against the exact futures contract's quotes.

Fill policy (quote_top_of_book/1):
  BUY  fills at the ask + slippage_ticks x tick, rounded UP to the tick
  SELL fills at the bid - slippage_ticks x tick, rounded DOWN to the tick
  No fill from a stale, missing, one-sided, crossed or other-instrument quote.
  With partial_fills on, a fill is capped by the displayed top-of-book size,
  in whole units of the configured granularity (lot or unit).
  A CLOSE order can never fill more than the position currently open, so a
  close cannot flip the position.

Bar-only replay (next_bar_open/1): an order created at time t fills at the
OPEN of the first futures minute bar that starts at or after t, once that bar
is visible. Bars are not quotes; fills from them are labelled with the model.

Costs are estimates from config (see shared.costs) and are stored per fill.
"""
from datetime import timedelta

from sf_core import BUY, SELL, CLOSE, OPEN, round_to_tick, iso, parse_ts

LIVE_STATES = ("PENDING", "PARTIAL", "CANCEL_PENDING")


def quote_check(q, instrument_id, now, max_age_s):
    """(ok, reason) for using q to fill."""
    if not q:
        return False, "no quote"
    if str(q.get("instrument_id")) != str(instrument_id):
        return False, "quote is for another instrument"
    bid, ask = q.get("bid"), q.get("ask")
    if not bid or not ask or bid <= 0 or ask <= 0:
        return False, "one-sided or empty quote"
    if bid > ask:
        return False, f"crossed quote (bid {bid} > ask {ask})"
    age = (now - parse_ts(q["ts"])).total_seconds()
    if age > max_age_s:
        return False, f"stale quote ({int(age)}s old > {max_age_s}s)"
    if age < -1:
        return False, "quote time is in the future"
    return True, None


def quote_price(side, q, tick, slip_ticks):
    if side == BUY:
        return round_to_tick(q["ask"] + slip_ticks * tick, tick, BUY)
    return round_to_tick(q["bid"] - slip_ticks * tick, tick, SELL)


def fill_costs(side, price, qty, c, first_fill):
    value = price * qty
    brokerage = c["brokerage_per_order_rs"] if first_fill else 0.0
    stt = value * c["stt_sell_pct"] / 100 if side == SELL else 0.0
    txn = value * c["exchange_txn_pct"] / 100
    sebi = value / 1e7 * c["sebi_per_crore_rs"]
    stamp = value * c["stamp_buy_pct"] / 100 if side == BUY else 0.0
    gst = (brokerage + txn + sebi) * c["gst_pct"] / 100
    total = brokerage + stt + txn + sebi + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "txn": round(txn, 2),
            "sebi": round(sebi, 2), "stamp": round(stamp, 2), "gst": round(gst, 2),
            "total": round(total, 2)}


def position_qty(store, trade_id):
    """Open units from fills: OPEN fills minus CLOSE fills. The single source
    of truth for quantity, so replays and retries cannot double count."""
    r = store.q1("""SELECT COALESCE(SUM(CASE WHEN purpose='OPEN' THEN qty ELSE 0 END),0) o,
                           COALESCE(SUM(CASE WHEN purpose='CLOSE' THEN qty ELSE 0 END),0) c
                    FROM fills WHERE trade_id=?""", trade_id)
    return r["o"], r["c"]


class PaperBroker:
    producer = "paper_broker"

    def __init__(self, engine):
        self.e = engine

    def _granule(self, o, fp):
        return o["body"]["lot_size"] if fp.get("granularity", "lot") == "lot" else 1

    def step(self, now):
        st = self.e.store
        for o in st.orders("status IN ('PENDING','PARTIAL','CANCEL_PENDING')"):
            self._work(o, now)

    def _work(self, o, now):
        st, cfg = self.e.store, self.e.cfg(o["body"]["config_version"])
        fp, costs = cfg["shared"]["fill_policy"], cfg["shared"]["costs"]
        body = o["body"]
        cancel = o["status"] == "CANCEL_PENDING"
        if cancel and not body.get("fill_during_cancel"):
            return self._finish_cancel(o, now)
        remaining = o["requested_qty"] - o["filled_qty"]
        if o["purpose"] == CLOSE:
            opened, closed = position_qty(st, o["trade_id"])
            remaining = min(remaining, opened - closed)        # never over-close
            if remaining <= 0:
                return
        if self.e.fill_mode == "bars":
            px, evidence, why = self._bar_price(o, now, fp)
        else:
            q = self.e.data.quote(o["instrument_id"], now)
            ok, why = quote_check(q, o["instrument_id"], now, fp["max_quote_age_s"])
            px = quote_price(o["side"], q, body["tick_size"], fp["slippage_ticks"]) if ok else None
            evidence = {"model": fp["model"], "bid": q and q.get("bid"), "ask": q and q.get("ask"),
                        "bid_qty": q and q.get("bid_qty"), "ask_qty": q and q.get("ask_qty"),
                        "quote_ts": q and q.get("ts"), "source": q and q.get("source")}
            if ok and fp.get("partial_fills"):
                g = self._granule(o, fp)
                shown = (q.get("ask_qty") if o["side"] == BUY else q.get("bid_qty")) or 0
                shown -= (body.get("quote_used") or {}).get(q["ts"], 0)     # a displayed size fills once
                remaining = min(remaining, (max(shown, 0) // g) * g)
                if remaining <= 0:
                    ok, why, px = False, "displayed size below one fill granule", None
        if px is None:
            if body.get("last_block") != why:
                with st.tx() as c:
                    body["last_block"] = why
                    self._save(c, o, body, o["status"], o["filled_qty"], now)
                    st.emit(c, "FillBlocked", self.producer, now, {"client_order_id": o["client_order_id"],
                            "reason": why, "side": o["side"], "purpose": o["purpose"]},
                            mode=body["mode"], trade_id=o["trade_id"], direction=o["direction"],
                            instrument_id=o["instrument_id"], config_version=body["config_version"])
            if cancel:
                self._finish_cancel(o, now)
            return
        self.fill(o, remaining, px, now, evidence, costs)
        if cancel:
            self._finish_cancel(self.e.store.orders("client_order_id=?", o["client_order_id"])[0], now)

    def _bar_price(self, o, now, fp):
        bars = self.e.data.bars("FUT:" + o["instrument_id"], now, "1m")
        created = parse_ts(o["created"])
        for b in bars:
            if parse_ts(b["ts"]) >= created:
                tick = o["body"]["tick_size"]
                slip = fp["slippage_ticks"] * tick
                raw = b["open"] + slip if o["side"] == BUY else b["open"] - slip
                return (round_to_tick(raw, tick, o["side"]),
                        {"model": fp["bar_model"], "bar_ts": b["ts"], "bar_open": b["open"]}, None)
        return None, None, "no futures bar after the order yet"

    def _save(self, c, o, body, status, filled, now):
        self.e.store.put_order(c, dict(body, client_order_id=o["client_order_id"], trade_id=o["trade_id"],
                                    instrument_id=o["instrument_id"], side=o["side"], purpose=o["purpose"],
                                    direction=o["direction"], status=status, requested_qty=o["requested_qty"],
                                    filled_qty=filled, created=o["created"]), now)

    def fill(self, o, qty, px, now, evidence, costs_cfg=None, fill_id=None):
        """Record one fill (idempotent on fill_id) and publish OrderFilled."""
        st = self.e.store
        body = o["body"]
        costs_cfg = costs_cfg or self.e.cfg(body["config_version"])["shared"]["costs"]
        with st.tx() as c:
            cur = c.execute("SELECT status, filled_qty FROM orders WHERE client_order_id=?",
                            (o["client_order_id"],)).fetchone()
            n = c.execute("SELECT COUNT(*) FROM fills WHERE client_order_id=?", (o["client_order_id"],)).fetchone()[0]
            fid = fill_id or f"{o['client_order_id']}-F{n + 1}"
            if c.execute("SELECT 1 FROM fills WHERE fill_id=?", (fid,)).fetchone():
                return None                                        # duplicate fill report
            filled_before = cur["filled_qty"]
            if o["purpose"] == CLOSE:
                op = c.execute("""SELECT COALESCE(SUM(CASE WHEN purpose='OPEN' THEN qty END),0),
                                         COALESCE(SUM(CASE WHEN purpose='CLOSE' THEN qty END),0)
                                  FROM fills WHERE trade_id=?""", (o["trade_id"],)).fetchone()
                qty = min(qty, op[0] - op[1])
            qty = min(qty, o["requested_qty"] - filled_before)
            if qty <= 0:
                return None
            cst = fill_costs(o["side"], px, qty, costs_cfg, first_fill=(n == 0))
            rec = {"fill_id": fid, "client_order_id": o["client_order_id"], "trade_id": o["trade_id"],
                   "instrument_id": o["instrument_id"], "side": o["side"], "purpose": o["purpose"],
                   "direction": o["direction"], "qty": qty, "price": px, "ts": iso(now),
                   "evidence": evidence, "costs": cst, "mode": body["mode"]}
            c.execute("INSERT INTO fills(fill_id,client_order_id,trade_id,side,purpose,qty,price,ts,body) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (fid, o["client_order_id"], o["trade_id"], o["side"],
                                                   o["purpose"], qty, px, iso(now), _j(rec)))
            filled = filled_before + qty
            status = cur["status"]
            if status not in ("CANCEL_PENDING", "CANCELED"):
                status = "FILLED" if filled >= o["requested_qty"] else "PARTIAL"
            body.pop("last_block", None)
            qts = (evidence or {}).get("quote_ts")
            if qts:
                used = body.setdefault("quote_used", {})
                used[qts] = used.get(qts, 0) + qty
                for k in sorted(used)[:-5]:
                    used.pop(k)
            self._save(c, o, body, status, filled, now)
            st.emit(c, "OrderFilled", self.producer, now, rec, mode=body["mode"], trade_id=o["trade_id"],
                    direction=o["direction"], instrument_id=o["instrument_id"],
                    config_version=body["config_version"], event_id=f"fill:{fid}")
        return rec

    def _finish_cancel(self, o, now):
        st = self.e.store
        with st.tx() as c:
            cur = c.execute("SELECT status, filled_qty FROM orders WHERE client_order_id=?",
                            (o["client_order_id"],)).fetchone()
            if cur["status"] != "CANCEL_PENDING":
                return
            self._save(c, o, o["body"], "CANCELED", cur["filled_qty"], now)
            st.emit(c, "OrderCanceled", self.producer, now,
                    {"client_order_id": o["client_order_id"], "filled_qty": cur["filled_qty"],
                     "canceled_qty": o["requested_qty"] - cur["filled_qty"], "purpose": o["purpose"],
                     "side": o["side"]},
                    mode=o["body"]["mode"], trade_id=o["trade_id"], direction=o["direction"],
                    instrument_id=o["instrument_id"], config_version=o["body"]["config_version"],
                    event_id=f"cancel:{o['client_order_id']}")


def _j(x):
    import json
    return json.dumps(x, default=str, sort_keys=True)


def bar_exit_check(bar, direction, stop, target, ambiguity="stop_first"):
    """Stop/target from a bar alone. When one bar spans both levels the order
    inside the bar is unknown; the configured policy decides (stop_first is
    the conservative default) and the result says it was ambiguous."""
    from sf_core import LONG
    if direction == LONG:
        hit_s = stop is not None and bar["low"] <= stop
        hit_t = target is not None and bar["high"] >= target
    else:
        hit_s = stop is not None and bar["high"] >= stop
        hit_t = target is not None and bar["low"] <= target
    if hit_s and hit_t:
        return ("stop" if ambiguity == "stop_first" else "target"), True
    if hit_s:
        return "stop", False
    if hit_t:
        return "target", False
    return None, False

"""Ticks -> 1-minute bars. Canonical tier; everything else derives from this."""
from datetime import datetime, timezone


def floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


class Aggregator:
    """Feed ticks with add_tick(); call flush(minute_start) on each boundary."""

    def __init__(self, tokens):
        self.tokens = set(tokens)
        self.cur = {}            # token -> bar being built
        self.last = {}           # token -> last known state, for forward-fill
        self.last_cum_vol = {}   # token -> last cumulative volume seen

    def add_tick(self, t):
        tok = t.get("instrument_token")
        price = t.get("last_price")
        if tok is None or price is None:
            return

        b = self.cur.get(tok)
        if b is None:
            b = self.cur[tok] = {
                "open": price, "high": price, "low": price, "close": price,
                "volume": 0, "pv": 0.0, "oi": None,
                "bid": None, "ask": None, "bid_qty": None, "ask_qty": None,
                "tick_count": 0,
            }

        b["high"] = max(b["high"], price)
        b["low"] = min(b["low"], price)
        b["close"] = price
        b["tick_count"] += 1

        # Kite sends CUMULATIVE day volume. Store the increment, and
        # accumulate price*increment so VWAP aggregates correctly later.
        cum = t.get("volume_traded")
        if cum is not None:
            prev = self.last_cum_vol.get(tok)
            inc = cum if prev is None or cum < prev else cum - prev
            if inc > 0:
                b["volume"] += inc
                b["pv"] += price * inc
            self.last_cum_vol[tok] = cum

        # OI is a level, not a flow - last value wins
        if t.get("oi") is not None:
            b["oi"] = t["oi"]

        d = t.get("depth")
        if d:
            buy = d.get("buy") or []
            sell = d.get("sell") or []
            if buy:
                b["bid"] = buy[0].get("price")
                b["bid_qty"] = buy[0].get("quantity")
            if sell:
                b["ask"] = sell[0].get("price")
                b["ask_qty"] = sell[0].get("quantity")

    def flush(self, minute_start):
        """Emit one bar per known instrument. Silent ones are forward-filled."""
        bars = []
        for tok in self.tokens:
            b = self.cur.get(tok)

            if b is not None:
                row = dict(b)
                row["is_interpolated"] = False
                self.last[tok] = {k: b[k] for k in
                                  ("close", "oi", "bid", "ask", "bid_qty", "ask_qty")}
            else:
                prev = self.last.get(tok)
                if prev is None:
                    continue          # never seen - emit nothing, not a fake bar
                c = prev["close"]
                row = {"open": c, "high": c, "low": c, "close": c,
                       "volume": 0, "pv": 0.0, "oi": prev["oi"],
                       "bid": prev["bid"], "ask": prev["ask"],
                       "bid_qty": prev["bid_qty"], "ask_qty": prev["ask_qty"],
                       "tick_count": 0, "is_interpolated": True}

            row["ts"] = minute_start
            row["instrument_token"] = tok
            bars.append(row)

        self.cur.clear()
        return bars


def rollup(bars):
    """Aggregate 1m bars into one higher-timeframe bar. Same rules, one place."""
    bars = sorted(bars, key=lambda b: b["ts"])
    real = [b for b in bars if not b["is_interpolated"]]
    src = real or bars
    return {
        "ts": bars[0]["ts"],
        "instrument_token": bars[0]["instrument_token"],
        "open": src[0]["open"],
        "high": max(b["high"] for b in src),
        "low": min(b["low"] for b in src),
        "close": src[-1]["close"],
        "volume": sum(b["volume"] for b in bars),
        "pv": sum(b["pv"] for b in bars),
        "oi": next((b["oi"] for b in reversed(bars) if b["oi"] is not None), None),
        "bid": bars[-1]["bid"], "ask": bars[-1]["ask"],
        "bid_qty": bars[-1]["bid_qty"], "ask_qty": bars[-1]["ask_qty"],
        "tick_count": sum(b["tick_count"] for b in bars),
        "interpolated_bars": sum(1 for b in bars if b["is_interpolated"]),
    }


def vwap(bar):
    return bar["pv"] / bar["volume"] if bar["volume"] else None
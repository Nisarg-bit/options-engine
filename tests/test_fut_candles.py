"""15-minute stock-future candles for the Stock Futures market view."""
import pandas as pd
import pytest

import market_pulse as MP
import publisher as PUB
import pulse_api as PA


def bars(sym="UPCO", n=31, start="2026-11-19 03:45", prev=100.0):
    """n 1-minute bars as the collector writes them: each open = previous close."""
    t0 = pd.Timestamp(start, tz="UTC")
    rows, last = [], prev
    for i in range(n):
        c = 101.0 + i * 0.1
        rows.append({"ts": t0 + pd.Timedelta(minutes=i), "underlying": sym, "tradingsymbol": sym + "26NOVFUT",
                     "open": last, "high": max(last, c) + (0 if i == 0 else 0.05), "low": min(last, c),
                     "close": c, "volume": 10, "pv": c * 10, "is_interpolated": False})
        last = c
    return pd.DataFrame(rows)


def test_candles_are_15_minutes_in_ist_with_vwap():
    c = MP.fut_candles(bars(), 15)["UPCO"]
    assert c["t"] == ["09:15", "09:30", "09:45"]
    assert c["o"][0] == 101.0                      # first traded close, not yesterday's 100
    assert c["l"][0] == 101.0                      # the synthetic low at 100 is gone
    assert c["c"][1] == pytest.approx(103.9)
    assert c["vwap"][0] == pytest.approx(101.7)    # mean of 101.0 .. 102.4
    assert len(c["vwap"]) == 3


def test_minutes_before_the_first_trade_are_skipped():
    b = bars(n=20)
    b.loc[:4, "volume"] = 0
    c = MP.fut_candles(b, 15)["UPCO"]
    assert c["o"][0] == 101.5                      # bar 5 is the first with volume


def test_no_bars_no_candles():
    assert MP.fut_candles(None) == {} and MP.fut_candles(pd.DataFrame()) == {}


def test_publisher_writes_only_moved_symbols_in_one_update(monkeypatch):
    allc = {"AAA": {"t": ["09:15"], "o": [1], "h": [1], "l": [1], "c": [1], "vwap": [1]},
            "M&M": {"t": ["09:15"], "o": [2], "h": [2], "l": [2], "c": [2], "vwap": [2]}}
    monkeypatch.setattr(PA, "fut_candles_all", lambda day=None: ("2026-11-19", allc))
    calls = []

    class Ref:
        def __init__(self, path):
            self.path = path

        def update(self, v):
            calls.append((self.path, sorted(v)))

    class DB:
        reference = staticmethod(Ref)
    monkeypatch.setattr(PUB, "db", lambda: DB)
    PUB._candle_seen.clear()
    assert PUB.publish_fut_candles().startswith("candles 2 futures")
    assert calls == [("/candles/fut", ["AAA", "M&M"])]
    assert PUB.publish_fut_candles() == ""          # nothing moved
    allc["AAA"]["c"] = [5]
    PUB.publish_fut_candles()
    assert calls[-1] == ("/candles/fut", ["AAA"])

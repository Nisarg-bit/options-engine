from datetime import datetime, timezone
from aggregator import Aggregator, rollup, vwap

T0 = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 1, 9, 16, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 1, 9, 17, tzinfo=timezone.utc)
TOK = 111


def tick(price, cum_vol=None, oi=None, bid=None, ask=None):
    t = {"instrument_token": TOK, "last_price": price}
    if cum_vol is not None: t["volume_traded"] = cum_vol
    if oi is not None: t["oi"] = oi
    if bid is not None:
        t["depth"] = {"buy": [{"price": bid, "quantity": 10}],
                      "sell": [{"price": ask, "quantity": 20}]}
    return t


def test_ohlc():
    a = Aggregator([TOK])
    for p in (100, 105, 98, 102):
        a.add_tick(tick(p))
    b = a.flush(T0)[0]
    assert (b["open"], b["high"], b["low"], b["close"]) == (100, 105, 98, 102)
    assert b["tick_count"] == 4
    assert b["is_interpolated"] is False


def test_volume_is_delta_not_cumulative():
    a = Aggregator([TOK])
    a.add_tick(tick(100, cum_vol=1000))   # first tick: 1000 traded so far
    a.add_tick(tick(101, cum_vol=1500))   # +500
    b1 = a.flush(T0)[0]
    assert b1["volume"] == 1500

    a.add_tick(tick(102, cum_vol=1800))   # +300, NOT 1800
    b2 = a.flush(T1)[0]
    assert b2["volume"] == 300


def test_vwap_survives_rollup():
    """The whole reason we store pv: VWAP must not be averaged."""
    a = Aggregator([TOK])
    a.add_tick(tick(100, cum_vol=100))    # 100 @ 100
    b1 = a.flush(T0)[0]
    a.add_tick(tick(200, cum_vol=1100))   # 1000 @ 200
    b2 = a.flush(T1)[0]

    assert vwap(b1) == 100
    assert vwap(b2) == 200

    merged = rollup([b1, b2])
    # true VWAP = (100*100 + 1000*200) / 1100 = 190.9..., NOT (100+200)/2 = 150
    assert abs(vwap(merged) - 190.909090) < 0.001
    assert merged["volume"] == 1100


def test_oi_is_last_not_sum():
    a = Aggregator([TOK])
    a.add_tick(tick(100, oi=5000))
    a.add_tick(tick(101, oi=5200))
    b1 = a.flush(T0)[0]
    assert b1["oi"] == 5200

    a.add_tick(tick(102, oi=5100))
    b2 = a.flush(T1)[0]
    assert rollup([b1, b2])["oi"] == 5100


def test_silent_minute_is_interpolated():
    a = Aggregator([TOK])
    a.add_tick(tick(100, oi=42, bid=99, ask=101))
    a.flush(T0)

    b = a.flush(T1)[0]                    # no ticks this minute
    assert b["is_interpolated"] is True
    assert b["open"] == b["close"] == 100
    assert b["volume"] == 0
    assert b["tick_count"] == 0
    assert b["oi"] == 42                  # carried, not zeroed
    assert b["bid"] == 99


def test_unseen_instrument_emits_nothing():
    a = Aggregator([TOK, 999])
    a.add_tick(tick(100))
    bars = a.flush(T0)
    assert len(bars) == 1                 # not 2 - 999 never traded
    assert bars[0]["instrument_token"] == TOK


def test_rollup_counts_interpolated():
    a = Aggregator([TOK])
    a.add_tick(tick(100, cum_vol=10))
    b1 = a.flush(T0)[0]
    b2 = a.flush(T1)[0]
    b3 = a.flush(T2)[0]
    assert rollup([b1, b2, b3])["interpolated_bars"] == 2
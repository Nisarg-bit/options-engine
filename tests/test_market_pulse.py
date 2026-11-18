"""Market Pulse calculations (quant/market_pulse.py), on hand-built bars."""
import math

import pandas as pd
import pytest

import market_pulse as MP

T0 = pd.Timestamp("2026-11-12 03:45", tz="UTC")


def eq_bars(sym, closes, vol=100):
    rows = []
    for i, c in enumerate(closes):
        rows.append({"ts": T0 + pd.Timedelta(minutes=i), "underlying": sym, "tradingsymbol": sym,
                     "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                     "volume": vol, "pv": c * vol, "oi": None, "is_interpolated": False})
    return rows


EQ = pd.DataFrame(eq_bars("UPCO", [100, 101, 102, 103]) + eq_bars("DOWNCO", [50, 49, 48, 47])
                  + eq_bars("FLATCO", [20, 20, 20, 20]))
PREV = {"UPCO": 100.0, "DOWNCO": 50.0, "FLATCO": 20.0}
SECT = {"UPCO": "IT", "DOWNCO": "Bank"}


def test_stock_table_changes_vwap_and_flow_direction():
    t = MP.stock_table(EQ, PREV, SECT)
    assert t.loc["UPCO", "chg_pct"] == pytest.approx(3.0)
    assert t.loc["DOWNCO", "chg_pct"] == pytest.approx(-6.0)
    # every UPCO minute after the first rose -> inflow only; DOWNCO the mirror
    assert t.loc["UPCO", "outflow"] == 0 and t.loc["UPCO", "inflow"] == (101 + 102 + 103) * 100
    assert t.loc["DOWNCO", "inflow"] == 0 and t.loc["DOWNCO", "net"] < 0
    assert t.loc["FLATCO", "inflow"] == 0 and t.loc["FLATCO", "outflow"] == 0
    assert t.loc["FLATCO", "sector"] == "Others"           # not in the map
    assert bool(t.loc["UPCO", "above_vwap"]) and not bool(t.loc["DOWNCO", "above_vwap"])


def test_breadth_counts_and_ratio():
    b = MP.breadth(MP.stock_table(EQ, PREV, SECT))
    assert (b["advancers"], b["decliners"], b["unchanged"]) == (1, 1, 1)
    assert b["ratio"] == 0.0 and b["above_vwap_pct"] == pytest.approx(33.3, abs=0.1)


def test_money_flow_totals_in_crore_and_ranking():
    f = MP.money_flow(MP.stock_table(EQ, PREV, SECT))
    assert f["top_in"][0]["symbol"] == "UPCO" and f["top_out"][0]["symbol"] == "DOWNCO"
    assert -1 <= f["ratio"] <= 1


def test_sector_map_leaders_and_laggards():
    s = MP.sector_map(MP.stock_table(EQ, PREV, SECT))
    assert s["leaders"][0]["sector"] == "IT" and s["laggards"][0]["sector"] == "Bank"
    assert {r["sector"] for r in s["sectors"]} == {"IT", "Bank", "Others"}


@pytest.mark.parametrize("dp,doi,want", [(0.01, 0.05, "long buildup"), (-0.01, 0.05, "short buildup"),
                                          (0.01, -0.05, "short covering"), (-0.01, -0.05, "long unwinding"),
                                          (0.0, 0.0, "neutral")])
def test_oi_readings(dp, doi, want):
    assert MP.classify_oi(dp, doi) == want


def test_stock_oi_uses_first_bar_oi_and_future_prev_close():
    rows = eq_bars("UPCO", [101, 102, 104])
    for r, oi in zip(rows, [1000, 1100, 1200]):
        r["oi"], r["tradingsymbol"] = oi, "UPCO26NOVFUT"
    s = MP.stock_oi(pd.DataFrame(rows), {"UPCO26NOVFUT": 100.0})
    assert s["rows"][0]["read"] == "long buildup" and s["rows"][0]["oi_chg_pct"] == 20.0


def candles(seq):
    return pd.DataFrame([{"ts": T0 + pd.Timedelta(minutes=5 * i), "open": o, "high": h, "low": l,
                          "close": c, "volume": 1} for i, (o, h, l, c) in enumerate(seq)]).set_index("ts")


def test_bullish_order_block_found_then_retested():
    seq = [(100, 101, 99, 100)] * 4 + [(100, 100.5, 98, 98.5),      # last down candle
           (98.5, 103, 98.4, 102.8), (102.8, 106, 102.6, 105.5), (105.5, 107, 105, 106.5),
           (106.5, 107, 99.5, 100)]                                    # trades back into the zone
    obs = MP.order_blocks(candles(seq), k=3, mult=1.5)
    buy = [b for b in obs if b["side"] == "buy"]
    assert buy and buy[-1]["lo"] == 98 and buy[-1]["hi"] == 100 and buy[-1]["tested"]


def test_untested_block_is_reported_as_nearest():
    seq = [(100, 101, 99, 100)] * 4 + [(100, 100.5, 98, 98.5), (98.5, 103, 98.4, 102.8),
           (102.8, 106, 102.6, 105.5), (105.5, 107, 105, 106.5), (106.5, 108, 106, 107.5)]
    obs = MP.order_blocks(candles(seq))
    near = MP.nearest_untested(obs, 107.5)
    assert near is not None and near["side"] == "buy" and not near["tested"]


def test_mood_score_bounds_labels_and_missing_parts():
    best = MP.mood(1, 1, 5, 1, 0.0, 3)
    worst = MP.mood(-1, -1, -5, 1, 1.0, -3)
    assert best["score"] >= 95 and best["label"] == "Risk-on"
    assert worst["score"] <= 5 and worst["label"] == "Risk-off"
    part = MP.mood(breadth_ratio=0.2)                 # only one part known
    assert part["missing"] == ["flow", "trend", "volatility", "global"]
    assert part["score"] == 60
    assert MP.mood()["available"] is False


def test_vix_percentile_and_expected_range():
    assert MP.vix_percentile([10, 12, 14, 16, 18], 14) == pytest.approx(0.6)
    r = MP.expected_range(25000, 16, 7)
    assert r["hi"] - 25000 == pytest.approx(25000 * 0.16 * math.sqrt(7 / 365), abs=0.01)

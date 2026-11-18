"""Intraday stock screening (opening-range breakout + VWAP + NIFTY-relative),
money-flow + OI-buildup verification, and the opening-range stop.

Bars are hand-built so every boundary is exact (futures/sf_screen.py).
"""
from datetime import date, timedelta

import pytest

import sf_fixtures as F
import sf_screen as S
from sf_agents import or_risk_pct, plan_levels
from sf_core import LONG, SHORT, Calendar, ReplayClock, evaluate, ist, iso, load_config
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store

DAY = date(2026, 11, 17)
CAL = Calendar()
CFG = load_config()
SK = dict(CFG["profiles"]["intraday"]["stock"], min_turnover_rs=100_000)


def mk(rows, vol=1000, tv=None):
    """rows: (o, h, l, c) from 09:15; tv: per-bar traded price for turnover (default mid)."""
    out, t = [], ist(DAY, "09:15")
    for i, (o, h, l, c) in enumerate(rows):
        px = tv[i] if tv else (o + c) / 2
        out.append({"ts": iso(t), "available_at": iso(t + timedelta(minutes=1)), "open": o, "high": h, "low": l,
                    "close": c, "volume": vol, "turnover": px * vol})
        t += timedelta(minutes=1)
    return out


def orng(low=100, high=110, mid=105):
    rows = [(mid, mid + 0.5, mid - 0.5, mid)] * 15
    rows[3] = (mid, mid, low, mid)
    rows[9] = (mid, high, mid, mid)
    return rows


def stock(post, n_open=15, tv=None, **kw):
    return mk(orng(**kw)[:n_open] + list(post), tv=tv)


def nifty(post_close=100.5, n_post=1):
    return mk([(100, 100, 100, 100)] * 15 + [(100, post_close, 100, post_close)] * n_post)


def one(bars, bench, direction=LONG, at="09:31", sk=SK, frozen=None, max_age=180):
    return S.screen_one("X", bars, bench, direction, ist(DAY, at), sk, CAL, max_age, frozen)


# ---------------------------------------------------------------- qualifying

def test_qualifying_long():
    r = one(stock([(110, 111.5, 110, 111)]), nifty(100.5))
    assert r["status"] == "PASS", r
    m = r["metrics"]
    assert (m["or_high"], m["or_low"], m["close"]) == (110, 100, 111)
    assert m["excess_return_pct"] == pytest.approx((111 / 105 - 1 - 0.005) * 100, abs=1e-3)
    assert r["inputs"]["bar_ts"].endswith("09:30:00+05:30")


def test_qualifying_short():
    r = one(stock([(100, 100, 98.5, 99)]), nifty(99.5), SHORT)
    assert r["status"] == "PASS" and r["score"] > 0


# ---------------------------------------------------------------- failures

def test_wick_only_breakout_fails():
    r = one(stock([(108, 115, 108, 109)]), nifty(100.5))          # high 115, close 109 inside the range
    assert r["status"] == "FAIL" and "opening high" in r["reason"]


def test_close_exactly_on_the_range_edge_fails():
    assert one(stock([(109, 110, 109, 110)]), nifty(100.5))["status"] == "FAIL"
    assert one(stock([(101, 101, 100, 100)]), nifty(99.5), SHORT)["status"] == "FAIL"


def test_close_exactly_at_vwap_fails():
    # every bar trades at 111, so the session VWAP is exactly 111 = the breakout close
    r = one(stock([(110, 111, 110, 111)], tv=[111] * 16), nifty(100.5))
    assert r["metrics"]["vwap"] == 111 and r["status"] == "FAIL" and "VWAP" in r["reason"]


def test_breakout_on_the_wrong_side_of_vwap_fails():
    r = one(stock([(110, 111.5, 110, 111)], tv=[120] * 15 + [111]), nifty(100.5))
    assert r["status"] == "FAIL" and r["checks"][0]["result"] == "FAIL" and r["checks"][1]["result"] == "PASS"


def test_failed_nifty_relative_return_or_turnover():
    r = one(stock([(110, 111.5, 110, 111)]), nifty(107))          # NIFTY +7% beats the stock's +5.7%
    assert r["status"] == "FAIL" and "return > NIFTY" in r["reason"]
    r = one(stock([(110, 111.5, 110, 111)]), nifty(100.5), sk=dict(SK, min_turnover_rs=10**12))
    assert r["status"] == "FAIL" and "turnover" in r["reason"]


def test_incomplete_or_zero_width_opening_data_is_unavailable():
    bars = [b for b in stock([(110, 111.5, 110, 111)]) if not b['ts'].endswith('09:20:00+05:30')]
    r = one(bars, nifty(100.5))
    assert r["status"] == "UNAVAILABLE" and "incomplete" in r["reason"]
    flat = mk([(105, 105, 105, 105)] * 15 + [(106, 106, 106, 106)])
    assert "zero width" in one(flat, nifty(100.5))["reason"]


def test_stale_or_misaligned_inputs_are_unavailable():
    r = one(stock([(110, 111.5, 110, 111)]), nifty(100.5), at="10:30")
    assert r["status"] == "UNAVAILABLE" and "stale" in r["reason"]
    r = one(stock([(110, 111.5, 110, 111)]), nifty(100.5)[:15])   # NIFTY has no 09:30 bar
    assert r["status"] == "UNAVAILABLE" and "aligned" in r["reason"]


def test_no_screening_before_a_post_range_bar():
    r = one(stock([]), nifty(100.5, n_post=0), at="09:30")
    assert r["status"] == "UNAVAILABLE" and "after the opening range" in r["reason"]


def test_first_post_range_bar_cannot_alter_the_opening_range():
    r = one(stock([(110, 200, 50, 111)]), nifty(100.5))
    assert (r["metrics"]["or_high"], r["metrics"]["or_low"]) == (110, 100)


def test_stays_eligible_on_later_bars_while_conditions_hold():
    bars = stock([(110, 111.5, 110, 111), (111, 112, 111, 111.5)])
    r = one(bars, nifty(100.5, n_post=2), at="09:32")
    assert r["status"] == "PASS" and r["inputs"]["bar_ts"].endswith("09:31:00+05:30")


# ---------------------------------------------------------------- ranking

def fake(sym, excess, turnover):
    return {"symbol": sym, "metrics": {"excess_return_pct": excess, "turnover_rs": turnover}}


def test_ranking_both_directions_with_tie_breaks():
    longs = S.rank([fake("B", 1.0, 5), fake("A", 2.0, 1), fake("D", 1.0, 9), fake("C", 1.0, 9)], LONG)
    assert [x["symbol"] for x in longs] == ["A", "C", "D", "B"]       # excess desc, turnover desc, symbol
    assert "ahead of C" in longs[0]["rank_reason"]
    shorts = S.rank([fake("P", -0.5, 1), fake("Q", -3.0, 1), fake("R", -1.0, 1)], SHORT)
    assert [x["symbol"] for x in shorts] == ["Q", "R", "P"]           # largest underperformer first


def test_engine_screens_ranks_and_freezes_stock_ranges(tmp_path):
    sc = F.bullish()
    data = sc.build()
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "s.db"))
    e = Engine(st, FixtureData(data), clock, F.demo_config())
    e.request_scan("intraday")
    e.run_until_idle()
    cands = st.events("type='CandidatesRanked'")[0]["payload"]
    syms = [c["symbol"] for c in cands["candidates"]]
    assert syms[:4] == ["INFY", "TCS", "HCLTECH", "WIPRO"]
    top = cands["candidates"][0]
    assert {"close", "vwap", "or_high", "or_low", "breakout_distance_pct", "return_pct", "nifty_return_pct",
            "excess_return_pct", "turnover_rs"} <= set(top["metrics"])
    assert "excess return" in top["rank_reason"] and "excess return" in cands["tie_break"]
    key = f"or_stock:{DAY}:INFY"
    first = st.control(key)
    assert first
    for b in data["bars"]["INFY"]["1m"]:                  # revise INFY's opening bars after the fact
        if b["ts"] < f"{DAY}T09:30":
            b["high"] *= 1.10
    clock.advance(minutes=5)
    e.request_scan("intraday")
    e.run_until_idle()
    assert st.control(key) == first
    latest = st.events("type='CandidatesRanked'")[-1]["payload"]["candidates"]
    infy = next(c for c in latest if c["symbol"] == "INFY")
    assert infy["metrics"]["or_high"] == top["metrics"]["or_high"]


# ---------------------------------------------------------------- verification

VR = CFG["profiles"]["intraday"]["verification"]


def test_verification_needs_money_flow_and_oi_buildup():
    ok = {"turnover_rs": 6e7, "money_flow_net_rs": 1e6, "oi_read": "long buildup"}
    assert evaluate(VR["long"], ok)[0] == "PASS"
    assert evaluate(VR["long"], dict(ok, oi_read="short covering"))[0] == "FAIL"
    assert evaluate(VR["long"], dict(ok, money_flow_net_rs=-1))[0] == "FAIL"
    short = {"turnover_rs": 6e7, "money_flow_net_rs": -1e6, "oi_read": "short buildup"}
    assert evaluate(VR["short"], short)[0] == "PASS"
    assert evaluate(VR["short"], dict(short, oi_read="long unwinding"))[0] == "FAIL"
    assert evaluate(VR["long"], dict(ok, oi_read=None))[0] == "UNAVAILABLE"


def test_short_covering_fails_verification_and_runner_up_enters(tmp_path):
    sc = F.bullish()
    data = sc.build()
    for s in data["desk"]["INFY"]:
        s["metrics"]["oi_read"] = "short covering"
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "v.db"))
    e = Engine(st, FixtureData(data), clock, F.demo_config())
    e.request_scan("intraday")
    e.run_until_idle()
    vr = st.events("type='VerificationResult'")
    assert vr[0]["payload"]["symbol"] == "INFY" and vr[0]["payload"]["status"] == "FAIL"
    assert st.trades()[0]["underlying"] == "TCS"


# ---------------------------------------------------------------- exit plan

def test_opening_range_stop_and_2r_target_on_the_futures_fill(tmp_path):
    sc = F.bullish()
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "x.db"))
    e = Engine(st, FixtureData(sc.build()), clock, F.demo_config())
    e.request_scan("intraday")
    e.run_until_idle()
    t = st.trades()[0]["body"]
    m = t["candidate"]["metrics"]
    risk = (m["close"] - m["or_low"]) / m["close"]
    p = t["exit_plan"]
    assert p["stop_price"] == pytest.approx(t["entry_avg"] * (1 - risk), abs=1e-3)
    assert p["target_price"] == pytest.approx(t["entry_avg"] * (1 + 2 * risk), abs=1e-3)
    ev = p["stop_evidence"]
    assert ev["stock_edge"] == m["or_low"] and ev["target_multiple"] == 2
    assert p["stop_price"] != m["or_low"]                 # equity level and futures stop are kept apart


def test_short_opening_range_stop_sits_above_entry():
    cand = {"metrics": {"close": 99.0, "or_high": 100.0, "or_low": 95.0}}
    r = or_risk_pct(SHORT, cand)
    stop, tgt = plan_levels(SHORT, 200.0, {"stop": {"basis": "opening_range_edge"},
                                           "target": {"basis": "r_multiple", "value": 2}}, r)
    assert stop == pytest.approx(200 * (1 + 1 / 99), abs=1e-3) and tgt == pytest.approx(200 * (1 - 2 / 99), abs=1e-3)


def test_opening_range_stop_beyond_the_hard_limit_skips_the_entry(tmp_path):
    cfg = F.demo_config()
    cfg["profiles"]["intraday"]["exit_plan"]["hard_max_stop_pct"] = 0.1
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "h.db"))
    e = Engine(st, FixtureData(F.bullish().build()), clock, cfg)
    e.request_scan("intraday")
    e.run_until_idle()
    rej = st.events("type='CandidateRejected'")
    assert rej and all("beyond the hard limit" in r["payload"]["reason"] for r in rej)
    assert st.orders() == []

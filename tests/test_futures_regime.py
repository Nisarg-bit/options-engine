"""Market-regime rules for the stock-futures agents (futures/sf_regime.py).

Intraday: opening-range Fibonacci zone on NIFTY 50 + session VWAP of a volume
source (NIFTY futures proxy). Swing: 5-session return + higher-high /
lower-low structure. Bars are hand-built so every boundary is exact.
"""
from datetime import date, timedelta

import pytest

import sf_fixtures as F
import sf_regime as R
from sf_core import Calendar, ReplayClock, ist, iso, load_config
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store

DAY = date(2026, 11, 17)
CAL = Calendar()
CFG = load_config()
RC = CFG["profiles"]["intraday"]["regime"]
SRC = RC["vwap_source"]["symbol"]


def bars(day, ohlc, start="09:15", volume=0):
    out, t = [], ist(day, start)
    for o, h, l, c in ohlc:
        out.append({"ts": iso(t), "available_at": iso(t + timedelta(minutes=1)), "open": o, "high": h, "low": l,
                    "close": c, "volume": volume, "turnover": (o + c) / 2 * volume if volume else 0})
        t += timedelta(minutes=1)
    return out


def opening(low=100.0, high=110.0):
    """15 opening bars spanning exactly [low, high]."""
    rows = [(105, 106, 104, 105)] * 15
    rows[3] = (104, 105, low, 104)
    rows[9] = (106, high, 105, 107)
    return rows


def nifty(*after, opening_rows=None):
    return bars(DAY, (opening_rows or opening()) + list(after))


def vsrc(*closes, n_open=15, vol=1000):
    """VWAP source: 15 opening bars at 200, then the given closes."""
    rows = [(200, 200, 200, 200)] * n_open + [(c, c, c, c) for c in closes]
    return bars(DAY, rows, volume=vol)


def run(nb, vb, at="09:31", max_age=180, rc=RC, frozen=None):
    return R.classify_intraday(nb, vb, ist(DAY, at), rc, CAL, max_age, frozen=frozen)


# ---------------------------------------------------------------- opening range

def test_levels_are_measured_up_from_the_low():
    lv = R.opening_range(nifty(), DAY, "09:15", 15, ist(DAY, "09:30"))
    assert lv["status"] == "READY"
    from decimal import Decimal as Dm
    assert (Dm(lv["or_low"]), Dm(lv["or_high"])) == (100, 110)
    assert (Dm(lv["fib_038"]), Dm(lv["fib_050"]), Dm(lv["fib_0618"])) == (Dm("103.8"), 105, Dm("106.18"))


def test_the_0930_bar_cannot_change_the_opening_range():
    nb = nifty((107, 200, 50, 107))                      # a huge 09:30 bar
    lv = R.opening_range(nb, DAY, "09:15", 15, ist(DAY, "09:40"))
    assert (float(lv["or_low"]), float(lv["or_high"])) == (100, 110)


def test_no_signal_before_the_range_is_ready():
    out = run(nifty(), vsrc(), at="09:25")
    assert out["readiness"] == "NOT_READY" and out["regime"] == "UNAVAILABLE"
    out = run(nifty(), vsrc(), at="09:30")               # range ready, no confirmation bar yet
    assert out["regime"] == "UNAVAILABLE" and "confirmation bar" in out["reason"]


def test_incomplete_or_zero_width_range_blocks():
    out = run(bars(DAY, opening()[:14]), vsrc(), at="09:40")
    assert out["regime"] == "UNAVAILABLE" and "incomplete: 14/15" in out["reason"]
    flat = [(100, 100, 100, 100)] * 15 + [(100, 100, 100, 100)]
    out = run(bars(DAY, flat), vsrc(201), at="09:31")
    assert out["regime"] == "UNAVAILABLE" and "zero width" in out["reason"]


# ---------------------------------------------------------------- classification

def test_confirmed_breakout_is_bullish():
    out = run(nifty((106, 107.5, 106, 107)), vsrc(201))
    assert out["regime"] == "BULLISH"
    assert [c["result"] for c in out["checks"]] == ["ABOVE", "ABOVE"]
    assert out["metrics"]["fib_0618"] == 106.18 and out["metrics"]["nifty_close"] == 107


def test_confirmed_breakdown_is_bearish():
    out = run(nifty((104, 104, 103, 103.5)), vsrc(199))
    assert out["regime"] == "BEARISH"


def test_close_inside_the_zone_is_neutral():
    out = run(nifty((105, 105.5, 104.5, 105)), vsrc(201))
    assert out["regime"] == "SIDEWAYS" and out["reason"].startswith("NEUTRAL: close inside")


@pytest.mark.parametrize("close", [106.18, 103.8])
def test_close_exactly_on_a_zone_edge_is_neutral(close):
    out = run(nifty((close, close, close, close)), vsrc(201 if close > 105 else 199))
    assert out["regime"] == "SIDEWAYS" and "zone edge" in out["reason"]


def test_close_exactly_at_vwap_is_neutral():
    out = run(nifty((107, 107, 107, 107)), vsrc(200))     # every source bar at 200 -> VWAP 200
    assert out["checks"][1]["result"] == "AT"
    assert out["regime"] == "SIDEWAYS" and "exactly at VWAP" in out["reason"]


def test_breakout_without_vwap_confirmation_is_neutral():
    out = run(nifty((107, 107, 107, 107)), vsrc(199))
    assert out["regime"] == "SIDEWAYS" and "disagree" in out["reason"]


def test_a_wick_through_the_zone_that_closes_inside_does_not_qualify():
    out = run(nifty((105, 109, 105, 106)), vsrc(201))      # high 109 above 106.18, close 106 inside
    assert out["regime"] == "SIDEWAYS"


def test_confirmation_interval_uses_completed_buckets():
    rc = dict(RC, confirm_interval_min=5)
    nb = nifty(*[(107, 107, 107, 107)] * 5 + [(104, 104, 104, 104)] * 2)   # 09:30-09:34 bucket, then 2 bars
    vb = vsrc(*[201] * 7)
    out = run(nb, vb, at="09:37", rc=rc)
    assert out["regime"] == "BULLISH" and out["confirm_bar"]["end"].endswith("09:35:00+05:30")


def test_stale_missing_or_misaligned_inputs_are_unavailable():
    out = run(nifty((107, 107, 107, 107)), vsrc(201), at="10:30")
    assert out["regime"] == "UNAVAILABLE" and "stale" in out["reason"]
    out = run(nifty((107, 107, 107, 107)), [])
    assert out["regime"] == "UNAVAILABLE" and "no completed bars for VWAP source" in out["reason"]
    out = run(nifty((107, 107, 107, 107), (107, 107, 107, 107)), vsrc(201), at="09:32")
    assert out["regime"] == "UNAVAILABLE" and "not aligned" in out["reason"]
    out = run(nifty((107, 107, 107, 107)), vsrc(201, vol=0))
    assert out["regime"] == "UNAVAILABLE" and "no traded volume" in out["reason"]


def test_frozen_levels_are_reused():
    lv = R.opening_range(nifty(), DAY, "09:15", 15, ist(DAY, "09:30"))
    revised = nifty((107, 107, 107, 107), opening_rows=opening(high=120))
    out = run(revised, vsrc(201), frozen=lv)
    assert out["metrics"]["or_high"] == 110 and out["levels"]["frozen"] is True


def test_engine_freezes_levels_and_resets_next_session(tmp_path):
    sc = F.bullish(days={1: {}})
    data = sc.build()
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "r.db"))
    e = Engine(st, FixtureData(data), clock, F.demo_config())
    e.request_scan("intraday")
    e.run_until_idle()
    key = f"or_levels:intraday:{DAY}"
    first = st.control(key)
    assert first and '"status": "READY"' in first
    ev = st.events("type='RegimeClassified'")[-1]["payload"]
    assert ev["regime"] == "BULLISH" and ev["evidence"]["vwap_source"]["kind"] == "proxy"
    for b in data["bars"][F.BENCH]["1m"]:                 # a late revision of the opening bars
        if b["ts"] < f"{DAY}T09:30":
            b["high"] *= 1.05
    clock.advance(minutes=5)
    e.request_scan("intraday")
    e.run_until_idle()
    assert st.control(key) == first
    assert st.events("type='RegimeClassified'")[-1]["payload"]["evidence"]["levels"]["frozen"] is True
    nxt = sc.sessions[1]
    clock.set(ist(nxt, "10:30"))
    e.request_scan("intraday")
    e.run_until_idle()
    lv = st.events("type='RegimeClassified'")[-1]["payload"]["evidence"]["levels"]
    assert lv["start"].startswith(str(nxt)) and lv["frozen"] is False   # fresh levels for the new session


def test_engine_sideways_scenario_is_neutral(tmp_path):
    sc = F.sideways()
    clock = ReplayClock(ist(DAY, "10:30"))
    st = Store(str(tmp_path / "s.db"))
    e = Engine(st, FixtureData(sc.build()), clock, F.demo_config())
    e.request_scan("intraday")
    e.run_until_idle()
    ev = st.events("type='RegimeClassified'")[-1]["payload"]
    assert ev["regime"] == "SIDEWAYS" and "inside" in ev["reasons"][0] and st.orders() == []


# ====================================================================== swing

SW_RC = CFG["profiles"]["swing"]["regime"]
T = date(2026, 11, 20)                      # latest completed session
NOW = ist(T, "16:00")


def daily(closes, highs=None, lows=None, last=T, skip=None):
    days = [last]
    while len(days) < len(closes):
        days.insert(0, CAL.prev_session(days[0]))
    out = []
    for i, (d, c) in enumerate(zip(days, closes)):
        if skip and d == skip:
            continue
        out.append({"ts": iso(ist(d, "09:15")), "available_at": iso(ist(d, "15:31")), "open": c,
                    "high": highs[i] if highs else c + 1, "low": lows[i] if lows else c - 1, "close": c})
    return out


def sw(b, now=NOW):
    from sf_agents import last_completed_session
    return R.classify_swing(b, now, SW_RC, CAL, last_completed_session(now, CAL))


H_UP = [101] * 5 + [102]                    # higher high on the last bar
L_DN = [99] * 5 + [98]                      # lower low on the last bar


def test_swing_bullish_and_bearish():
    out = sw(daily([100] * 5 + [101], highs=H_UP))
    assert out["regime"] == "BULLISH" and out["metrics"]["return_pct"] == 1.0
    out = sw(daily([100] * 5 + [99], lows=L_DN))
    assert out["regime"] == "BEARISH"


def test_swing_return_without_structure_is_sideways():
    out = sw(daily([100] * 5 + [101], highs=[101] * 5 + [100.5]))
    assert out["regime"] == "SIDEWAYS" and "no higher high" in out["reason"]
    out = sw(daily([100] * 5 + [99], lows=[99] * 5 + [99.5]))
    assert out["regime"] == "SIDEWAYS" and "no lower low" in out["reason"]


def test_swing_exact_threshold_does_not_pass():
    assert sw(daily([100] * 5 + [100.5], highs=H_UP))["regime"] == "SIDEWAYS"
    assert sw(daily([100] * 5 + [99.5], lows=L_DN))["regime"] == "SIDEWAYS"


def test_swing_equal_highs_and_lows_do_not_count():
    assert sw(daily([100] * 5 + [101], highs=[101] * 6))["regime"] == "SIDEWAYS"
    assert sw(daily([100] * 5 + [99], lows=[99] * 6))["regime"] == "SIDEWAYS"


def test_swing_outside_day_is_resolved_by_the_return():
    hi, lo = [101] * 5 + [103], [99] * 5 + [97]
    assert sw(daily([100] * 5 + [101], highs=hi, lows=lo))["regime"] == "BULLISH"
    assert sw(daily([100] * 5 + [99], highs=hi, lows=lo))["regime"] == "BEARISH"
    out = sw(daily([100] * 5 + [100.2], highs=hi, lows=lo))
    assert out["regime"] == "SIDEWAYS" and "outside day" in out["reason"]


def test_swing_missing_incomplete_stale_or_invalid_bars_are_unavailable():
    assert "need 6" in sw(daily([100] * 4 + [101]))["reason"]
    gap = daily([100] * 6 + [101], highs=[101] * 6 + [102], skip=CAL.prev_session(T, 3))
    out = sw(gap)
    assert out["regime"] == "UNAVAILABLE" and "gap" in out["reason"]
    out = sw(daily([100] * 5 + [101], highs=H_UP, last=CAL.prev_session(T)))
    assert out["regime"] == "UNAVAILABLE" and "stale" in out["reason"]
    bad = daily([100] * 5 + [101], highs=[101] * 5 + [90])
    assert sw(bad)["regime"] == "UNAVAILABLE"


def test_swing_uses_only_completed_sessions_at_decision_time():
    b = daily([100] * 6 + [101], highs=[101] * 6 + [102])   # last bar is T, completed at 15:31
    out = sw(b, now=ist(T, "14:00"))                          # mid-session: T not complete yet
    assert out["metrics"].get("latest_session") == str(CAL.prev_session(T)) or out["regime"] != "BULLISH"

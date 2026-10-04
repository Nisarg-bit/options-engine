"""stop_touch_api: scope, cache and the payload shape the page reads.

Run:  python -m pytest test_stop_touch_api.py -q
"""
from datetime import date, datetime, timedelta, timezone

import stop_touch_api as STA

IST = timezone(timedelta(hours=5, minutes=30))
WEEKDAY = lambda d: d.weekday() < 5
DAY, EXP = date(2026, 9, 21), date(2026, 9, 22)
TS = datetime(2026, 9, 21, 10, 44, tzinfo=IST)

ROWS = [{"strike": 23400.0,
         "CE": {"bid": 58.20, "ask": 58.35, "mid": 58.28},
         "PE": {"bid": 83.35, "ask": 83.60, "mid": 83.47}}]
STRADDLE = ("short_straddle", 141.75,
            [{"strike": 23400.0, "kind": "CE", "qty": -1},
             {"strike": 23400.0, "kind": "PE", "qty": -1}])


def call(**kw):
    args = dict(underlying="NIFTY", day=DAY, expiry=EXP, nearest=EXP,
                last_ts_ist=TS, spot=23380.5, sigma_pts=141.7,
                structures=[STRADDLE], rows=ROWS, is_trading_day=WEEKDAY,
                session_share=0.6626)
    args.update(kw)
    return STA.compute(**args)


def test_nearest_expiry_gets_a_grid():
    out, meta = call()
    g = out["short_straddle"]["grid"]
    assert meta["computed"] and [x["stop_frac"] for x in g] == [0.1, 0.2, 0.3, 0.5, 0.75, 1.0]
    assert 0 < out["short_straddle"]["grid"][2]["p_touch"] < 1
    # half-spread is the sum of both legs' half-spreads, from the chain
    assert out["short_straddle"]["half_spread"] == round((0.15 + 0.25) / 2, 2)


def test_other_expiries_say_why_they_are_empty():
    out, meta = call(expiry=date(2026, 9, 29))
    assert out == {} and not meta["computed"] and "nearest expiry" in meta["note"]


def test_missing_share_is_named_not_defaulted(monkeypatch):
    monkeypatch.setattr(STA.T, "load_session_share", lambda: None)
    out, meta = call(session_share=None, last_ts_ist=TS + timedelta(minutes=1))
    assert out == {} and "gap_share" in meta["note"]


def test_a_leg_without_a_quote_is_skipped_by_name():
    rows = [{"strike": 23400.0, "CE": {"bid": 0, "ask": 58.35}, "PE": ROWS[0]["PE"]}]
    out, meta = call(rows=rows, last_ts_ist=TS + timedelta(minutes=2))
    assert out["short_straddle"] is None
    assert "short_straddle" in meta["skipped"]


def test_same_flush_is_served_from_cache():
    a = call(last_ts_ist=TS + timedelta(minutes=3))
    b = call(last_ts_ist=TS + timedelta(minutes=3), spot=99999.0)   # ignored: cached
    assert a is b

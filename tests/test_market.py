from datetime import datetime, timezone, date, timedelta
import market


def test_weekend_is_not_a_trading_day():
    assert market.is_trading_day(date(2026, 8, 29)) is False   # Saturday
    assert market.is_trading_day(date(2026, 8, 30)) is False   # Sunday


def test_normal_weekday_is_a_trading_day():
    assert market.is_trading_day(date(2026, 9, 1)) is True     # Tuesday, NIFTY expiry


def test_session_bounds_are_utc_0345_to_1000():
    s, e = market.session_bounds_utc(date(2026, 9, 1))
    assert (s.hour, s.minute) == (3, 45)     # 09:15 IST
    assert (e.hour, e.minute) == (10, 0)     # 15:30 IST


def test_in_session_boundaries():
    s, e = market.session_bounds_utc(date(2026, 9, 1))
    assert market.in_session(s) is True
    assert market.in_session(e) is True
    assert market.in_session(s - timedelta(minutes=1)) is False
    assert market.in_session(e + timedelta(minutes=1)) is False


def test_next_minute_rounds_up():
    t = datetime(2026, 9, 1, 4, 30, 42, tzinfo=timezone.utc)
    assert market.next_minute(t) == datetime(2026, 9, 1, 4, 31, tzinfo=timezone.utc)
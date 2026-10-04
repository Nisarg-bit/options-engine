"""NSE session calendar and clock. Stores UTC, thinks in IST at the edges."""
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo
import pandas as pd
import exchange_calendars as xcals

IST = ZoneInfo("Asia/Kolkata")
_CAL = xcals.get_calendar("XBOM")

OPEN = dtime(9, 15)
CLOSE = dtime(15, 30)


def is_trading_day(d):
    """d: datetime.date. Holidays come from the exchange calendar, not a guess."""
    return pd.Timestamp(d) in _CAL.sessions


def session_bounds_utc(d):
    start = datetime.combine(d, OPEN, tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(d, CLOSE, tzinfo=IST).astimezone(timezone.utc)
    return start, end


def in_session(now=None):
    now = now or datetime.now(timezone.utc)
    d = now.astimezone(IST).date()
    if not is_trading_day(d):
        return False
    start, end = session_bounds_utc(d)
    return start <= now <= end


def next_minute(now=None):
    now = now or datetime.now(timezone.utc)
    return now.replace(second=0, microsecond=0) + timedelta(minutes=1)
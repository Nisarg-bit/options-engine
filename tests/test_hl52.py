"""52-week history (hl52) and the Stock Futures market fields in Market Pulse."""
import io
import json
import zipfile
from datetime import date

import pandas as pd
import pytest

import hl52 as H
import market_pulse as MP
import pulse_api as PA

CSV = (b"TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,"
       b"FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric\n"
       b"2026-11-18,2026-11-18,CM,NSE,STK,2885,X,RELIANCE,EQ,,,,,RIL,1370,1376.5,1354,1363,1363,1367\n"
       b"2026-11-18,2026-11-18,CM,NSE,STK,2886,X,RELIANCE,BE,,,,,RIL,1,9999,1,1,1,1\n"
       b"2026-11-18,2026-11-18,CM,NSE,STK,2887,X,TINYCO,EQ,,,,,T,5,6,4,5,5,5\n")


def test_parse_keeps_eq_rows_of_wanted_symbols():
    df = H.parse(CSV, {"RELIANCE"})
    assert list(df["symbol"]) == ["RELIANCE"]
    assert df.iloc[0][["high", "low", "close"]].tolist() == [1376.5, 1354.0, 1363.0]


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "daily.parquet"), str(tmp_path / "absent.json")


def fake_fetcher(prices, calls):
    def f(d, session=None, symbols=None):
        calls.append(d)
        if d not in prices:
            return None
        return pd.DataFrame({"date": [pd.Timestamp(d)], "symbol": ["RELIANCE"], "high": [prices[d][0]],
                             "low": [prices[d][1]], "close": [prices[d][2]]})
    return f


def test_update_fetches_missing_weekdays_once_and_remembers_holidays(paths):
    store, absent = paths
    prices = {date(2026, 11, 16): (110, 90, 100), date(2026, 11, 17): (120, 95, 115),
              date(2026, 11, 18): (118, 99, 117)}
    calls = []
    added, missing = H.update(days=10, today=date(2026, 11, 19), fetcher=fake_fetcher(prices, calls),
                              symbols={"RELIANCE"}, pause=0, path=store, absent_path=absent)
    assert added == 3 and missing == 6            # 9-13 Nov and 19 Nov; the 19th is too fresh to mark absent
    assert all(d.weekday() < 5 for d in calls)
    assert sorted(json.load(open(absent))) == ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    calls.clear()
    H.update(days=10, today=date(2026, 11, 19), fetcher=fake_fetcher(prices, calls),
             symbols={"RELIANCE"}, pause=0, path=store, absent_path=absent)
    assert calls == [date(2026, 11, 19)]          # only today is asked for again


def test_levels_use_completed_sessions_before_the_day(paths):
    store, absent = paths
    prices = {date(2026, 11, 16): (110, 90, 100), date(2026, 11, 17): (120, 95, 115),
              date(2026, 11, 18): (130, 99, 117)}
    H.update(days=10, today=date(2026, 11, 19), fetcher=fake_fetcher(prices, []),
             symbols={"RELIANCE"}, pause=0, path=store, absent_path=absent)
    H._levels.update(key=None, val={})
    assert H.levels(date(2026, 11, 18), path=store) == {"RELIANCE": (120.0, 90.0, 2)}   # the 18th itself excluded


def fut_bars(sym, opens_closes, oi, volume=None):
    t0 = pd.Timestamp("2026-11-19 09:15")
    volume = volume or [100] * len(oi)
    return [{"ts": t0 + pd.Timedelta(minutes=i), "underlying": sym, "tradingsymbol": sym + "26NOVFUT",
             "open": o, "high": max(o, c), "low": min(o, c), "close": c, "oi": x, "volume": v}
            for i, ((o, c), x, v) in enumerate(zip(opens_closes, oi, volume))]


def test_stock_oi_adds_open_gap_intraday_and_sector():
    # As the collector writes it: the first bar's open is yesterday's close (100).
    df = pd.DataFrame(fut_bars("UPCO", [(100, 102), (102, 101)], [1000, 1100]))
    r = MP.stock_oi(df, {"UPCO26NOVFUT": 100.0}, {"UPCO": "Auto"})["rows"][0]
    assert r["sector"] == "Auto" and r["open"] == 102.0 and r["last"] == 101.0
    assert r["gap_pct"] == 2.0                    # 09:15 minute closed 2% above yesterday
    assert r["intraday_pct"] == pytest.approx(-0.98)
    assert r["price_chg_pct"] == 1.0


def test_open_skips_minutes_without_trades():
    df = pd.DataFrame(fut_bars("UPCO", [(100, 100), (100, 97), (97, 98)], [1000, 1000, 1000], volume=[0, 50, 50]))
    r = MP.stock_oi(df, {"UPCO26NOVFUT": 100.0})["rows"][0]
    assert r["open"] == 97.0 and r["gap_pct"] == -3.0


def test_stock_oi_without_sector_map_still_works():
    df = pd.DataFrame(fut_bars("UPCO", [(102, 103)], [1000]))
    r = MP.stock_oi(df, {})["rows"][0]
    assert r["sector"] == "Others" and r["gap_pct"] is None and r["price_chg_pct"] is None


def test_add_52week_widens_with_todays_range(monkeypatch):
    monkeypatch.setattr(H, "levels", lambda day: {"UPCO": (150.0, 80.0, 200), "NEWCO": (50.0, 40.0, 30)})
    oi = {"available": True, "rows": [{"symbol": "UPCO"}, {"symbol": "NEWCO"}, {"symbol": "GONE"}]}
    tbl = pd.DataFrame({"last": [155.0, 45.0], "high": [156.0, 46.0], "low": [149.0, 44.0]},
                       index=["UPCO", "NEWCO"])
    PA.add_52week(oi, tbl, date(2026, 11, 19))
    up, new, gone = oi["rows"]
    assert up["hi52"] == 156.0 and up["lo52"] == 80.0      # today's high is the new 52-week high
    assert up["from_high_pct"] == pytest.approx(-0.64) and up["from_low_pct"] == 93.75
    assert "hi52" not in new                                # under 120 sessions of history
    assert "hi52" not in gone
    assert oi["hl52_sessions"] == 200

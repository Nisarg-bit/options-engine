"""Live-data plumbing for the stock-futures system: the NIFTY near-month future
in the instrument master / writer / collector, and RepoData reading today's
partitions as they grow while keeping past sessions as daily summaries."""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import instruments as I
import sf_data
import writer
from sf_core import ist
from sf_data import RepoData

DAY = date(2026, 11, 17)


def test_choose_index_future_picks_nearest_nifty_contract():
    rows = [{"instrument_type": "FUT", "name": "NIFTY", "expiry": date(2026, 12, 29), "instrument_token": 2,
             "tradingsymbol": "NIFTY26DECFUT", "lot_size": 75, "tick_size": 0.1},
            {"instrument_type": "FUT", "name": "NIFTY", "expiry": date(2026, 11, 24), "instrument_token": 1,
             "tradingsymbol": "NIFTY26NOVFUT", "lot_size": 75, "tick_size": 0.1},
            {"instrument_type": "FUT", "name": "NIFTY", "expiry": date(2026, 10, 27), "instrument_token": 0,
             "tradingsymbol": "NIFTY26OCTFUT", "lot_size": 75, "tick_size": 0.1},
            {"instrument_type": "FUT", "name": "BANKNIFTY", "expiry": date(2026, 11, 24), "instrument_token": 9,
             "tradingsymbol": "BANKNIFTY26NOVFUT", "lot_size": 30, "tick_size": 0.2},
            {"instrument_type": "CE", "name": "NIFTY", "expiry": date(2026, 11, 24), "instrument_token": 7,
             "tradingsymbol": "X", "lot_size": 75}]
    r = I.choose_index_future(rows, DAY)
    assert (r["instrument_token"], r["kind"], r["tick_size"], r["expiry"]) == (1, "index_fut", 0.1, "2026-11-24")
    assert I.choose_index_future([], DAY) is None


def test_writer_puts_the_index_future_in_its_own_partition(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"instruments": [
        {"instrument_token": 1, "tradingsymbol": "NIFTY26NOVFUT", "name": "NIFTY", "expiry": "2026-11-24",
         "kind": "index_fut", "instrument_type": "FUT", "lot_size": 75}]}))
    m = writer.load_instrument_map(str(p))[1]
    assert (m["partition"], m["underlying"], m["opt_type"], m["kind"]) == ("IDXFUT", "NIFTY", "FUT", "index_fut")


# ---------------------------------------------------------------- RepoData live reads

def _part(root, tier, day, und, rows, name):
    d = root / "data" / tier / f"date={day}" / f"underlying={und}"
    d.mkdir(parents=True, exist_ok=True)
    cols = {f.name: [r.get(f.name) for r in rows] for f in writer.SCHEMA}
    pq.write_table(pa.table(cols, schema=writer.SCHEMA), d / name)


def _bar(day, i, tok, sym, und, px, vol=1000, **kw):
    t = pd.Timestamp(f"{day} 09:15", tz="Asia/Kolkata").tz_convert("UTC") + pd.Timedelta(minutes=i)
    b = {"ts": t.to_pydatetime(), "instrument_token": tok, "tradingsymbol": sym, "underlying": und,
         "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": vol, "pv": px * vol,
         "tick_count": 3, "is_interpolated": False}
    b.update(kw)
    return b


def test_repo_reads_new_minute_parts_and_the_vwap_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sf_data, "SNAP_TTL_S", 0)
    root = tmp_path
    _part(root, "stocks_1m", DAY, "EQ", [_bar(DAY, i, 1, "INFY", "INFY", 1500 + i) for i in range(30)], "part-0.parquet")
    _part(root, "stocks_1m", DAY, "IDXFUT",
          [_bar(DAY, i, 5, "NIFTY26NOVFUT", "NIFTY", 24100 + i, vol=7500, expiry=date(2026, 11, 24)) for i in range(30)]
          + [_bar(DAY, i, 6, "NIFTY26DECFUT", "NIFTY", 24300 + i, vol=750, expiry=date(2026, 12, 29)) for i in range(30)],
          "part-0.parquet")
    (root / "data" / "instruments").mkdir(parents=True)
    (root / "data" / "instruments" / f"{DAY}.json").write_text(json.dumps({"date": str(DAY), "instruments": [
        {"instrument_token": 5, "tradingsymbol": "NIFTY26NOVFUT", "name": "NIFTY", "kind": "index_fut"}]}))
    r = RepoData(str(root))
    now = ist(DAY, "10:00")
    assert len(r.bars("INFY", now, "1m")) == 30
    fut = r.bars("NIFTY FUT", now, "1m")
    assert len(fut) == 30 and fut[0]["close"] == 24100 and fut[0]["volume"] == 7500      # nearest expiry only
    assert r.health(now)["feeds"]["vwap_source_bars"] == "ok"
    # the collector writes the next minute's part: it must appear without a new adapter
    _part(root, "stocks_1m", DAY, "EQ", [_bar(DAY, 30, 1, "INFY", "INFY", 1530)], "part-1.parquet")
    assert len(r.bars("INFY", ist(DAY, "10:00"), "1m")) == 31


def test_repo_snapshot_is_reused_within_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(sf_data, "SNAP_TTL_S", 3600)
    _part(tmp_path, "stocks_1m", DAY, "EQ", [_bar(DAY, i, 1, "INFY", "INFY", 1500) for i in range(5)], "part-0.parquet")
    r = RepoData(str(tmp_path))
    assert len(r.bars("INFY", ist(DAY, "10:00"), "1m")) == 5
    _part(tmp_path, "stocks_1m", DAY, "EQ", [_bar(DAY, 5, 1, "INFY", "INFY", 1500)], "part-1.parquet")
    assert len(r.bars("INFY", ist(DAY, "10:00"), "1m")) == 5      # not re-statted inside the TTL


def test_repo_past_sessions_become_daily_summaries(tmp_path, monkeypatch):
    days = [date(2026, 11, d) for d in (11, 12, 13, 16)]
    for k, d in enumerate(days):
        _part(tmp_path, "bars_1m", d, "INDEX",
              [_bar(d, i, 256265, "NIFTY 50", "INDEX", 24000 + 10 * k + i) for i in range(375)], "part-0.parquet")
    r = RepoData(str(tmp_path))
    daily = r.bars("NIFTY 50", ist(DAY, "11:00"), "1d")
    assert [b["ts"][:10] for b in daily] == [str(d) for d in days]
    assert daily[-1]["close"] == 24030 + 374 and daily[-1]["high"] == 24030 + 375
    assert r._snap == {}                                           # no minute bars of past days held
    assert r.bars("NIFTY 50", ist(date(2026, 11, 16), "15:00"), "1d")[-1]["ts"].startswith("2026-11-13")

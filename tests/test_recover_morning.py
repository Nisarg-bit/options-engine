"""F3 recovery: conversion, no overlap with collected minutes, expiry skip."""
from datetime import date, datetime, timezone, timedelta
import json, os
import pandas as pd, pyarrow as pa, pyarrow.parquet as pq
import recover_morning as R
from writer import SCHEMA

UTC = timezone.utc
DAY = date(2026, 9, 23)
START = datetime(2026, 9, 23, 3, 45, tzinfo=UTC)
CUT = datetime(2026, 9, 23, 7, 6, tzinfo=UTC)          # first collected minute, 12:36 IST
META = {"tradingsymbol": "NIFTY26SEP25000CE", "underlying": "NIFTY",
        "expiry": date(2026, 9, 29), "strike": 25000.0, "opt_type": "CE"}


def candle(hh, mm, close=100.0, vol=10, oi=500):
    return {"date": f"2026-09-23T{hh:02d}:{mm:02d}:00+05:30", "open": close, "high": close + 2,
            "low": close - 2, "close": close, "volume": vol, "oi": oi}


def test_candles_become_schema_rows_in_utc_with_the_recovery_marker():
    rows = R.candles_to_rows(111, META, [candle(9, 15)], START, CUT)
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == START and r["tick_count"] == 0
    assert r["bid"] is None and r["ask"] is None
    assert r["pv"] == 10 * 100.0 and r["oi"] == 500
    assert set(r) == {f.name for f in SCHEMA}


def test_never_writes_a_minute_the_collector_has():
    rows = R.candles_to_rows(111, META, [candle(12, 35), candle(12, 36), candle(9, 14)], START, CUT)
    assert [r["ts"] for r in rows] == [datetime(2026, 9, 23, 7, 5, tzinfo=UTC)]


def test_expired_contracts_are_reported_not_fetched():
    imap = {1: {**META, "expiry": date(2026, 9, 24)}, 2: META,
            3: {**META, "underlying": "INDEX", "expiry": None}}
    fetch, gone = R.plan(DAY, imap, CUT, today=date(2026, 9, 26))
    assert fetch == [2, 3] and gone == [1]


def test_first_collected_ignores_kite_parts_and_assemble_writes_a_separate_file(tmp_path):
    root = str(tmp_path / "bars"); cache = str(tmp_path / "cache")
    d = os.path.join(root, f"date={DAY}", "underlying=NIFTY"); os.makedirs(d)
    coll = [dict(r, ts=CUT) for r in R.candles_to_rows(111, META, [candle(9, 15)], START, CUT + timedelta(days=1))]
    pq.write_table(pa.table({f.name: [r[f.name] for r in coll] for f in SCHEMA}, schema=SCHEMA),
                   os.path.join(d, "part-00000.parquet"))
    assert R.first_collected(DAY, root) == CUT
    os.makedirs(cache); json.dump([candle(9, 15), candle(9, 16), candle(12, 36)], open(os.path.join(cache, "111.json"), "w"))
    rep = R.assemble(DAY, {111: META}, START, CUT, cache, bars_root=root)
    assert rep["NIFTY"] == {"bars": 2, "instruments": 1, "minutes": 2}
    assert os.path.exists(os.path.join(d, f"part-kite-{DAY}.parquet"))
    assert R.first_collected(DAY, root) == CUT                 # kite part does not move it
    before = open(os.path.join(d, "part-00000.parquet"), "rb").read()
    R.assemble(DAY, {111: META}, START, CUT, cache, bars_root=root)   # idempotent rewrite
    assert open(os.path.join(d, "part-00000.parquet"), "rb").read() == before

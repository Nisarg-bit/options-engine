"""api.read_parts / session_moves caching (publisher 'slow node: chain', 19 Nov 2026).

Reading an older session used to evict every cached part of the newest one,
and session_moves re-read twenty sessions of INDEX parts whenever the
publisher's housekeeping pruned its cache entry.
"""
import os

import pandas as pd
import pytest

import api as A

DAYS = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]   # last = live


def _part(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pd.DataFrame(rows).to_parquet(path)


@pytest.fixture
def bars(tmp_path, monkeypatch):
    root = tmp_path / "bars_1m"
    for i, d in enumerate(DAYS):
        for k in range(3):                                   # three minutes
            ts = pd.Timestamp(f"{d} 09:1{5 + k}")
            _part(str(root / f"date={d}" / "underlying=INDEX" / f"part-{k:05d}.parquet"),
                  {"instrument_token": [256265, 264969], "ts": [ts, ts],
                   "close": [23000.0 + 10 * i + 5 * k, 12.0]})
            _part(str(root / f"date={d}" / "underlying=NIFTY" / f"part-{k:05d}.parquet"),
                  {"instrument_token": [1], "ts": [ts], "close": [100.0 + k]})
    monkeypatch.setattr(A, "BARS", str(root))
    A._parts.clear()
    A._cache.clear()
    A._day_index.clear()
    reads = []
    real = pd.read_parquet

    def counting(path, *a, **kw):
        reads.append(path)
        return real(path, *a, **kw)
    monkeypatch.setattr(A.pd, "read_parquet", counting)
    yield root, reads
    A._parts.clear()
    A._cache.clear()
    A._day_index.clear()


def test_reading_an_older_session_keeps_the_newest_cached(bars):
    root, reads = bars
    A.read_parts(DAYS[-1], "NIFTY")
    A.read_parts(DAYS[0], "INDEX")                           # e.g. session_moves
    reads.clear()
    A.read_parts(DAYS[-1], "NIFTY")
    assert reads == []                                       # today not re-read


def test_only_the_newest_and_the_requested_session_are_held(bars):
    A.read_parts(DAYS[-1], "NIFTY")
    A.read_parts(DAYS[0], "INDEX")
    A.read_parts(DAYS[1], "INDEX")
    held = {k.split("date=")[1][:10] for k in A._parts}
    assert held == {DAYS[1], DAYS[-1]}


def test_session_moves_values(bars):
    moves = A.session_moves(23020.0, lookback=3)
    assert moves == [10.0, 10.0, 10.0, 10.0]                 # last - first, NIFTY series only


def test_session_moves_does_not_reread_completed_sessions(bars):
    root, reads = bars
    first = A.session_moves(23020.0, lookback=3)
    A._cache.clear()                                         # what prune_cache does after 10 min
    reads.clear()
    assert A.session_moves(23020.0, lookback=3) == first
    assert all(f"date={DAYS[-1]}" in p for p in reads)       # only the live session, if anything


def test_changed_completed_session_is_recomputed(bars):
    root, reads = bars
    A.session_moves(23020.0, lookback=3)
    _part(str(root / f"date={DAYS[1]}" / "underlying=INDEX" / "part-00003.parquet"),
          {"instrument_token": [256265], "ts": [pd.Timestamp(f"{DAYS[1]} 09:18")],
           "close": [23500.0]})
    A._cache.clear()
    moves = A.session_moves(23020.0, lookback=3)
    assert moves[1] == 23500.0 - 23010.0


def test_live_session_is_always_recomputed(bars):
    root, reads = bars
    A.session_moves(23020.0, lookback=3)
    _part(str(root / f"date={DAYS[-1]}" / "underlying=INDEX" / "part-00003.parquet"),
          {"instrument_token": [256265], "ts": [pd.Timestamp(f"{DAYS[-1]} 09:18")],
           "close": [23100.0]})
    A._cache.clear()
    assert A.session_moves(23020.0, lookback=3)[-1] == 23100.0 - 23030.0


def test_a_series_far_from_spot_is_ignored(bars):
    assert A.session_moves(40000.0, lookback=3) == []        # nothing within 20%

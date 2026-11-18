"""Disk cache for daytrade_api's per-session numbers (publisher cold start)."""
import json
import os
from datetime import date

import pytest

import daytrade_api as DA


@pytest.fixture
def env(tmp_path, monkeypatch):
    bars = tmp_path / "bars"
    days = [date(2026, 11, d) for d in (12, 13, 16, 17)]
    for d in days:
        p = bars / f"date={d}" / "underlying=NIFTY"
        p.mkdir(parents=True)
        (p / "part-00000.parquet").write_bytes(b"x" * (100 + d.day))
    monkeypatch.setattr(DA.V, "BARS", str(bars))
    monkeypatch.setattr(DA, "STATS_FILE", str(tmp_path / "cache" / "daytrade_stats.json"))
    calls = []

    def fake_load(day, und):
        calls.append(day)
        return {1: "book"}, date(2026, 11, 24), None
    monkeypatch.setattr(DA.V, "load", fake_load)
    monkeypatch.setattr(DA.V, "forward", lambda snap: 24000.0)
    monkeypatch.setattr(DA.R, "study", lambda day, bm, exp, a, b: {"history_eligible": True, "implied": 1.1 + day.day,
                                                                   "rv15": 0.9})
    DA._cache.clear()
    DA._disk.update(mtime=None, entries={})
    return days, calls, bars


def test_warm_saves_completed_sessions_only(env):
    days, calls, bars = env
    added, kept = DA.warm(["NIFTY"])
    assert added == 3 and kept == 0                         # 12, 13, 16; not the live 17th
    entries = json.load(open(DA.STATS_FILE))["entries"]
    assert set(entries) == {f"NIFTY|{d}" for d in days[:-1]}
    assert DA.warm(["NIFTY"]) == (0, 3)                      # second run: nothing to do


def test_restart_reads_the_cache_instead_of_loading_books(env):
    days, calls, bars = env
    DA.warm(["NIFTY"])
    before = {d: DA._stats(d, "NIFTY") for d in days[:-1]}
    DA._cache.clear()                                       # a new publisher process
    DA._disk.update(mtime=None, entries={})
    calls.clear()
    after = {d: DA._stats(d, "NIFTY") for d in days[:-1]}
    assert calls == []                                      # no option book loaded
    assert after == before
    assert isinstance(after[days[0]]["date"], date) and isinstance(after[days[0]]["expiry"], date)


def test_changed_session_is_recomputed_and_live_session_never_cached(env):
    days, calls, bars = env
    DA.warm(["NIFTY"])
    DA._cache.clear()
    DA._disk.update(mtime=None, entries={})
    p = bars / f"date={days[1]}" / "underlying=NIFTY" / "part-00001.parquet"
    p.write_bytes(b"late part")                             # the day's files changed after warming
    calls.clear()
    DA._stats(days[1], "NIFTY")
    DA._stats(days[-1], "NIFTY")
    assert calls == [days[1], days[-1]]                     # stale entry and live session both recomputed


def test_unreadable_cache_falls_back_quietly(env):
    days, calls, bars = env
    os.makedirs(os.path.dirname(DA.STATS_FILE))
    open(DA.STATS_FILE, "w").write("{not json")
    calls.clear()
    assert DA._stats(days[0], "NIFTY")["implied"] == 1.1 + days[0].day
    assert calls == [days[0]]

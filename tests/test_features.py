"""Feature store: each group on its own, isolation, and idempotent writes."""
import math
from datetime import date, timedelta

import pandas as pd
import pytest

import features as F

SESSION = date(2026, 9, 25)
TODAY = date(2026, 9, 28)
EXPIRY = date(2026, 9, 30)


def daily_frame(n=300, start=24000.0, step=0.001, vix=13.0):
    days = [SESSION - timedelta(days=n - 1 - i) for i in range(n)]
    closes = [start * (1 + step) ** i for i in range(n)]
    return pd.DataFrame({"date": days, "open": closes, "high": [c * 1.004 for c in closes],
                         "low": [c * 0.996 for c in closes], "close": closes,
                         "vix": [vix + (i % 10) * 0.1 for i in range(n)]})


def chain(fwd=25000.0, step=50, width=20, skew=1.2):
    """Crude but monotone: puts richer than calls at the same distance."""
    rows = []
    for i in range(-width, width + 1):
        k = fwd + i * step
        ce = max(fwd - k, 0) + 120 * math.exp(-abs(k - fwd) / 300)
        pe = max(k - fwd, 0) + 120 * math.exp(-abs(k - fwd) / 300) * (skew if k < fwd else 1)
        rows.append({"strike": k, "ce": ce, "pe": pe})
    return rows


def ctx(**kw):
    c = {"today": TODAY, "session": SESSION, "expiry": EXPIRY, "dte": 2,
         "spot": 25000.0, "forward": 25000.0, "vix": 13.5, "sigma": 200.0,
         "straddle": 240.0, "c_over_sigma": 1.2, "ratio": 1.1}
    c.update(kw)
    return c


# ---------------------------------------------------------------- groups

def test_vix_rank_is_a_fraction_of_the_last_year():
    d = daily_frame()
    lo = F.vol_level(ctx(vix=0.0), d)["vix_rank_252"]
    hi = F.vol_level(ctx(vix=99.0), d)["vix_rank_252"]
    assert lo == 0.0 and hi == 1.0


def test_vix_rank_blank_on_short_history():
    assert F.vol_level(ctx(), daily_frame(n=30))["vix_rank_252"] is None


def test_realised_constant_log_return():
    r = 0.01
    closes = [100 * math.exp(r * i) for i in range(30)]
    out = F.realised(closes, vix=20.0)
    want = r * math.sqrt(252) * 100
    assert out["rv20"] == pytest.approx(want)
    assert out["rv5_over_rv20"] == pytest.approx(1.0)
    assert out["vrp"] == pytest.approx(20.0 - want)


def test_realised_blank_when_too_short():
    out = F.realised([100, 101, 102], vix=15)
    assert out["rv5"] is None and out["vrp"] is None


def test_skew_positive_when_puts_richer():
    ch = chain(skew=1.5)
    assert F.skew(ch, 25000, 200, 240)["wing_skew"] > 0
    flat = chain(skew=1.0)
    assert F.skew(flat, 25000, 200, 240)["wing_skew"] == pytest.approx(0, abs=1e-9)


def test_term_slope_zero_when_straddle_scales_with_root_time():
    near = chain()
    s1 = F.atm_straddle(near, 25000)[1]
    far = [{"strike": r["strike"], "ce": r["ce"] * 2, "pe": r["pe"] * 2} for r in near]
    # 4x the days -> 2x the straddle is a flat curve
    out = F.term(near, far, 25000, 2, 8)
    assert out["straddle_next"] == pytest.approx(2 * s1)
    assert out["term_slope"] == pytest.approx(0.0)


def test_positioning_reads_max_pain_and_tilt():
    rows = []
    for k in (24800, 24900, 25000, 25100, 25200):
        rows.append({"strike": k,
                     "CE": {"oi": 1000 if k == 25200 else 100, "volume": 10, "oi_chg": 0.5},
                     "PE": {"oi": 1000 if k == 24800 else 100, "volume": 30, "oi_chg": 1.0}})
    out = F.positioning(rows, 25000, 200)
    assert out["oi_resistance"] == 25200 and out["oi_support"] == 24800
    assert out["resistance_dist_sigma"] == pytest.approx(1.0)
    assert out["pcr_volume"] == pytest.approx(3.0)
    assert out["oi_tilt"] > 0          # puts added more than calls
    assert out["max_pain"] == 25000


def test_calendar_window_includes_expiry_day_and_skips_holidays():
    ev = [{"date": "2026-09-30", "impact": "high", "kind": "fed"},
          {"date": "2026-09-29", "impact": "medium", "kind": "holiday"},
          {"date": "2026-10-05", "impact": "high", "kind": "india_policy"}]
    out = F.calendar(TODAY, EXPIRY, ev)
    assert out["event_in_window"] is True and out["events_in_window"] == 1
    assert out["days_to_next_event"] == 2
    assert out["expiry_week"] is True


def test_trend_uptrend_sits_above_its_emas():
    out = F.trend(daily_frame(), SESSION, 13.5)
    assert out["ema20_dist"] > 0 and out["ema50_dist"] > out["ema20_dist"]
    assert out["rsi14"] == 100.0
    assert out["ret_5d"] == pytest.approx(1.001 ** 5 - 1)


# ------------------------------------------------------------ compute

def fake_builder(bars, expiry):
    if expiry == EXPIRY:
        return chain(), {}, 0
    return [{"strike": r["strike"], "ce": r["ce"] * 2, "pe": r["pe"] * 2}
            for r in chain()], {}, 0


def closing():
    return pd.DataFrame({"expiry": [EXPIRY, date(2026, 10, 7)]})


def session_bars(n=375):
    """n minutes of one CE and one PE at 25000; OI grows linearly."""
    ts = list(range(n))
    return pd.DataFrame({"ts": ts * 2, "instrument_token": [1] * n + [2] * n,
                         "expiry": [EXPIRY] * (2 * n), "strike": [25000.0] * (2 * n),
                         "opt_type": ["CE"] * n + ["PE"] * n,
                         "volume": [5] * (2 * n),
                         "oi": [100 + 20 * i / n for i in ts] + [100 + 50 * i / n for i in ts]})


def test_compute_fills_everything_with_good_inputs():
    sb = session_bars()
    row = F.compute("daily", ctx(), closing_bars=closing(), session_bars=sb,
                    daily=daily_frame(), events=[], chain_builder=fake_builder)
    assert row["decision_id"] == "daily:2026-09-28:2026-09-30"
    assert row["next_expiry"] == date(2026, 10, 7)
    assert row["missing"] == ""
    assert set(row) == set(F.FIELDS)
    assert row["oi_tilt"] > 0
    assert row["session_bars"] == 375 and row["partial_session"] is False
    assert row["feature_version"] == 4


def test_half_recorded_session_is_flagged():
    row = F.compute("daily", ctx(), closing_bars=closing(),
                    session_bars=session_bars(174), daily=daily_frame(),
                    events=[], chain_builder=fake_builder)
    assert row["session_bars"] == 174 and row["partial_session"] is True
    assert "partial_session(174/375 bars)" in row["missing"]


def test_oi_tilt_is_a_size_not_a_sign():
    """v1 returned -1 for ANY puts-down/calls-up pair. v2 scales by the book."""
    rows = [{"strike": 25000,
             "CE": {"oi": 10000, "volume": 1, "oi_chg": 0.001},    # +~10
             "PE": {"oi": 10000, "volume": 1, "oi_chg": -0.001}}]  # -~10
    tilt = F.positioning(rows, 25000, 200)["oi_tilt"]
    assert -0.01 < tilt < 0


# ------------------------------------------------------- daily cache

def _day(d, o, h, l, c, v):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "vix": v}


def test_kite_overrides_own_bars_but_blanks_never_erase():
    d1 = date(2026, 9, 24)
    own = pd.DataFrame([_day(d1, 23446.8, 23446.8, 22540.05, 23063.1, 12.63)])
    kite = pd.DataFrame([_day(d1, 23221.8, 23281.95, 23046.15, 23063.1, None)])
    m = F.merge_daily(own, kite).iloc[0]
    assert m["open"] == 23221.8 and m["low"] == 23046.15
    assert m["vix"] == 12.63            # Kite had no VIX; ours survives


def test_top_up_fills_a_new_day_with_close_only_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "DAILY", str(tmp_path / "daily.csv"))
    d0, d1 = date(2026, 9, 24), date(2026, 9, 25)
    F.save_daily(pd.DataFrame([_day(d0, 23221.8, 23281.95, 23046.15, 23063.1, 12.63)]))
    own = {d0: _day(d0, 1, 2, 3, 4, 5), d1: _day(d1, 23066.95, 23084.8, 23061.95, 23140.5, 12.16)}
    monkeypatch.setattr(F, "own_daily", lambda day, root=None: own[day])
    F.top_up_daily(d0)
    F.top_up_daily(d1)
    d = F.load_daily().set_index("date")
    assert d.loc[d0, "open"] == 23221.8 and d.loc[d0, "close"] == 23063.1
    assert pd.isna(d.loc[d1, "open"]) and pd.isna(d.loc[d1, "low"])
    assert d.loc[d1, "close"] == 23140.5


def test_trend_leaves_gap_blank_when_open_unknown():
    d = daily_frame()
    d.loc[d.index[-1], ["open", "high", "low"]] = None
    out = F.trend(d, SESSION, 13.5)
    assert out["prev_gap_sigma"] is None and out["prev_range_sigma"] is None
    assert out["rsi14"] is not None


def test_a_broken_group_blanks_only_itself():
    bad = daily_frame().drop(columns=["vix"])     # vol_level cannot run
    row = F.compute("daily", ctx(), closing_bars=closing(), session_bars=None,
                    daily=bad, events=[], chain_builder=fake_builder)
    assert "vol_level(KeyError)" in row["missing"]
    assert "positioning(no bars)" in row["missing"]
    assert row["rv20"] is not None and row["wing_skew"] is not None
    assert row["vix_rank_252"] is None


def test_compute_survives_no_inputs_at_all():
    row = F.compute("daily", ctx(), daily=pd.DataFrame(columns=F.DAILY_FIELDS),
                    events=[])
    assert row["spot"] == 25000.0
    assert "skew(no chain)" in row["missing"]


def test_upsert_is_idempotent(tmp_path):
    p = str(tmp_path / "d.csv")
    row = F.compute("daily", ctx(), daily=daily_frame(), events=[])
    F.upsert(row, p)
    F.upsert({**row, "vix": 99.0}, p)
    df = F.load(p)
    assert len(df) == 1 and float(df["vix"].iloc[0]) == 99.0
    F.upsert({**row, "decision_id": "daily:2026-09-29:2026-09-30"}, p)
    assert len(F.load(p)) == 2


def test_recovered_minutes_are_named_but_count_toward_the_session():
    sb = session_bars()
    sb["recovered"] = sb["ts"] < 200
    row = F.compute("daily", ctx(), closing_bars=closing(), session_bars=sb,
                    daily=daily_frame(), events=[], chain_builder=fake_builder)
    assert row["session_bars"] == 375 and row["partial_session"] is False
    assert "recovered_morning(200 min from Kite, no bid/ask)" in row["missing"]
    assert row["feature_version"] == 4


def test_collector_filled_in_minutes_are_not_mistaken_for_recovered(tmp_path):
    """v3's bug: tick_count 0 also marks the collector's own filled-in bars."""
    import pyarrow as pa, pyarrow.parquet as pq
    from writer import SCHEMA
    d = tmp_path / f"date={SESSION}" / "underlying=NIFTY"; d.mkdir(parents=True)
    base = {f.name: None for f in SCHEMA}
    def rows(n0, n, tick):
        return [dict(base, ts=pd.Timestamp("2026-09-25 03:45", tz="UTC") + pd.Timedelta(minutes=i),
                     instrument_token=1, underlying="NIFTY", expiry=EXPIRY, strike=25000.0,
                     opt_type="CE", volume=0, oi=100, tick_count=tick, is_interpolated=(tick == 0))
                for i in range(n0, n0 + n)]
    def write(name, rs):
        pq.write_table(pa.table({f.name: [r[f.name] for r in rs] for f in SCHEMA}, schema=SCHEMA), d / name)
    write("part-00000.parquet", rows(0, 300, 0))           # collector, interpolated, tick 0
    frame = F.session_frame(SESSION, "NIFTY", bars_root=str(tmp_path))
    assert not frame["recovered"].any()
    write("part-kite-2026-09-25.parquet", rows(300, 75, 0))
    frame = F.session_frame(SESSION, "NIFTY", bars_root=str(tmp_path))
    assert frame.loc[frame["recovered"], "ts"].nunique() == 75

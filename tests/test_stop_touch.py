"""stop_touch against its own claims.

Run:  python -m pytest test_stop_touch.py -q
"""
from datetime import date, datetime, timedelta
from math import erf, sqrt

import numpy as np
import pytest

import pricing as P
import stop_touch as T

WEEKDAY = lambda d: d.weekday() < 5
SHARE = 0.66


def norm_cdf(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


# ------------------------------------------------------------- primitives

def test_erf_matches_math_erf():
    xs = np.linspace(-5, 5, 2001)
    got = T._erf(xs)
    want = np.array([erf(x) for x in xs])
    assert np.max(np.abs(got - want)) < 2e-7


@pytest.mark.parametrize("K", [23000, 23350, 23400, 23800])
def test_bachelier_is_the_sites_formula(K):
    S, sig = 23380.5, 141.7
    assert float(T.bachelier(S, K, "CE", sig)) == pytest.approx(P.expected_call(S, K, sig), abs=1e-4)
    assert float(T.bachelier(S, K, "PE", sig)) == pytest.approx(P.expected_put(S, K, sig), abs=1e-4)


def test_zero_sigma_is_intrinsic():
    assert float(T.bachelier(100.0, 90.0, "CE", 0.0)) == 10.0
    assert float(T.bachelier(100.0, 90.0, "PE", 0.0)) == 0.0


STRADDLE = [{"strike": 23400, "kind": "CE", "qty": -1},
            {"strike": 23400, "kind": "PE", "qty": -1}]


def test_implied_reproduces_the_market_mid():
    s = T.solve_implied(23380.5, STRADDLE, 141.8, 141.7)
    assert float(T.structure_value(23380.5, STRADDLE, s)) == pytest.approx(141.8, abs=1e-6)
    # a rich straddle solves to a sigma ABOVE the site's
    assert s > 141.7


def test_an_unmatchable_price_is_refused_not_forced():
    # below intrinsic cannot be produced by any sigma
    assert T.solve_implied(23380.5, STRADDLE, 1.0, 141.7) is None


# ------------------------------------------------------------------ clock

def test_one_session_carries_the_session_share():
    now = datetime(2026, 9, 22, 9, 15)                 # Tuesday open
    st = T.schedule(now, date(2026, 9, 22), WEEKDAY, SHARE)
    assert not any(x.is_gap for x in st)
    assert sum(x.weight for x in st) == pytest.approx(SHARE)


def test_a_weeknight_gap_is_the_rest_of_the_day():
    now = datetime(2026, 9, 21, 9, 15)                 # Monday
    st = T.schedule(now, date(2026, 9, 22), WEEKDAY, SHARE)
    gaps = [x for x in st if x.is_gap]
    assert len(gaps) == 1 and gaps[0].weight == pytest.approx(1 - SHARE)
    assert gaps[0].when == datetime(2026, 9, 22, 9, 15)


def test_a_weekend_gap_carries_two_full_days_on_the_calendar_clock():
    now = datetime(2026, 9, 18, 9, 15)                 # Friday
    st = T.schedule(now, date(2026, 9, 21), WEEKDAY, SHARE)
    gaps = [x for x in st if x.is_gap]
    assert len(gaps) == 1 and gaps[0].weight == pytest.approx(2 + (1 - SHARE))


def test_a_holiday_is_folded_into_the_gap():
    hol = date(2026, 9, 23)
    cal = lambda d: d.weekday() < 5 and d != hol
    st = T.schedule(datetime(2026, 9, 22, 9, 15), date(2026, 9, 24), cal, SHARE)
    gaps = [x for x in st if x.is_gap]
    assert [g.weight for g in gaps] == [pytest.approx(1 + (1 - SHARE))]


def test_calendar_days_total_matches_the_site_clock():
    # Monday open to Monday-week expiry close: 7 calendar days, minus the
    # part of the last day after 15:30, which the site does not split out.
    st = T.schedule(datetime(2026, 9, 21, 9, 15), date(2026, 9, 28), WEEKDAY, SHARE)
    assert sum(x.weight for x in st) == pytest.approx(7 + SHARE)


# -------------------------------------------------------------- simulation

def run(structs, now, exit_at, expiry=date(2026, 9, 22), sigma=141.7, **kw):
    return T.simulate(23380.5, sigma, structs, now, expiry, exit_at, WEEKDAY, SHARE, **kw)


def straddle(mid=141.8, half=0.1, name="s"):
    return {"name": name, "legs": STRADDLE, "market_mid": mid,
            "half_spread": half, "credit": mid}


def test_a_stop_already_below_the_mark_is_certain():
    r = run([straddle()], datetime(2026, 9, 21, 9, 20),
            datetime(2026, 9, 21, 15, 15), stop_fracs=(-0.5,))["s"]
    assert r.grid[0]["p_touch"] == 1.0


def test_a_wider_stop_is_touched_less_often():
    now, ex = datetime(2026, 9, 21, 9, 20), datetime(2026, 9, 22, 15, 30)
    ps = [g["p_touch"] for g in run([straddle()], now, ex)["s"].grid]
    assert ps == sorted(ps, reverse=True) and ps[0] > ps[-1]


def test_the_grid_is_one_run_not_six():
    """Each grid point must equal a run at that single stop -- same paths."""
    now, ex = datetime(2026, 9, 21, 9, 20), datetime(2026, 9, 22, 15, 30)
    grid = run([straddle()], now, ex)["s"]
    one = run([straddle()], now, ex, stop_fracs=(0.30,))["s"]
    assert grid.at(0.30)["p_touch"] == one.grid[0]["p_touch"]


def test_holding_over_a_night_adds_gap_touches():
    now = datetime(2026, 9, 21, 9, 20)
    intraday = run([straddle()], now, datetime(2026, 9, 21, 15, 15))["s"].at(0.30)
    held = run([straddle()], now, datetime(2026, 9, 22, 15, 30))["s"].at(0.30)
    assert held["p_touch"] > intraday["p_touch"]
    assert intraday["share_at_open"] == 0.0
    assert held["share_at_open"] > 0.0


def test_same_seed_same_number():
    now, ex = datetime(2026, 9, 21, 9, 20), datetime(2026, 9, 21, 15, 15)
    assert run([straddle()], now, ex)["s"].grid == run([straddle()], now, ex)["s"].grid


def test_deep_itm_call_reduces_to_the_reflection_principle():
    """A deep in-the-money short call is short the index. Its stop is a spot
    barrier, and for Brownian motion P(max >= b) = 2 * (1 - N(b / sigma)).
    One-minute steps under-sample the max slightly, so the check is one-sided
    tight and two-sided loose."""
    K = 20000.0
    legs = [{"strike": K, "kind": "CE", "qty": -1}]
    spot, sig = 23380.5, 100.0
    mid = spot - K
    b = 80.0                                           # points of adverse move
    s = {"name": "c", "legs": legs, "market_mid": mid, "half_spread": 0.0,
         "credit": mid}
    now, ex = datetime(2026, 9, 22, 9, 15), datetime(2026, 9, 22, 15, 30)
    r = T.simulate(spot, sig, [s], now, date(2026, 9, 22), ex, WEEKDAY, SHARE,
                   n_paths=40_000, step_min=1, stop_fracs=(b / mid,))["c"].grid[0]
    # all variance to expiry is inside this one session: sigma is the session sigma
    want = 2 * (1 - norm_cdf(b / sig))
    assert r["p_touch"] <= want + 0.01
    assert r["p_touch"] == pytest.approx(want, abs=0.04)


def test_weekend_note_only_when_a_weekend_is_held():
    fri = datetime(2026, 9, 18, 9, 20)
    r = run([straddle()], fri, datetime(2026, 9, 22, 15, 30))["s"]
    assert r.weekend_held and "2.57" in r.note
    r2 = run([straddle()], datetime(2026, 9, 21, 9, 20), datetime(2026, 9, 21, 15, 15))["s"]
    assert not r2.weekend_held and r2.note == ""


def test_no_measured_share_means_no_number():
    with pytest.raises(ValueError):
        T.simulate(23380.5, 141.7, [straddle()], datetime(2026, 9, 21, 9, 20),
                   date(2026, 9, 22), datetime(2026, 9, 22, 15, 30), WEEKDAY, None)


def test_kelly_matches_the_card_formula():
    # card: p=0.678, b=141.8/42.5 -> 58.1%
    assert T.kelly(0.678, 141.8, 42.54) == pytest.approx(0.5815, abs=1e-3)

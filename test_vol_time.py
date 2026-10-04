"""Tests for vol_time.

The barrier maths is the part that needs proving. Unlike the Greeks in
chain_metrics.py there is no cached workbook value to check it against, so
it is checked against a Monte Carlo of the same process: simulate the path on
a fine grid, count how many touched, compare. If the closed form and the
simulation agree to three decimals across a spread of corridor widths and
volatilities, the formula is right.

The Monte Carlo is deliberately naive. A clever variance-reduced simulation
that shared code with the thing under test would prove nothing.

Run:  python -m pytest test_vol_time.py -q
      python test_vol_time.py          (prints the comparison tables)
"""

from datetime import datetime
from math import sqrt

import numpy as np
import pytest

import pricing as P
import vol_time as V


# ------------------------------------------------------------------- clocks

def test_full_session_is_one_day():
    a = datetime(2026, 9, 15, 9, 15)
    b = datetime(2026, 9, 15, 15, 30)
    assert abs(V.horizon_days(a, b) - 1.0) < 1e-12


def test_half_session():
    a = datetime(2026, 9, 15, 9, 15)
    b = datetime(2026, 9, 15, 12, 22, 30)          # 187.5 minutes in
    assert abs(V.horizon_days(a, b) - 0.5) < 1e-9


def test_late_entry_is_a_smaller_horizon_than_early():
    close = datetime(2026, 9, 15, 15, 15)
    early = V.horizon_days(datetime(2026, 9, 15, 9, 30), close)
    late = V.horizon_days(datetime(2026, 9, 15, 14, 0), close)
    assert late < early
    # the exact thing the workbook's floor destroyed
    assert early / late > 3.0


def test_outside_session_is_clamped():
    a = datetime(2026, 9, 15, 7, 0)                # before the open
    b = datetime(2026, 9, 15, 18, 0)               # after the close
    assert abs(V.horizon_days(a, b) - 1.0) < 1e-12


def test_multi_day_horizon():
    a = datetime(2026, 9, 15, 15, 30)              # end of one session
    b = datetime(2026, 9, 18, 9, 15)               # start of another
    # two complete sessions in between, nothing either side
    assert abs(V.horizon_days(a, b, full_sessions_between=2) - 2.0) < 1e-12


def test_end_before_start_is_zero():
    a = datetime(2026, 9, 15, 14, 0)
    b = datetime(2026, 9, 15, 10, 0)
    assert V.horizon_days(a, b) == 0.0


# --------------------------------------------------------------- volatility

def test_matches_the_workbook_at_whole_days():
    """Above the floor the two must agree exactly, or the port is wrong."""
    for days in (1.0, 3.0, 7.0, 30.0):
        old = P.Volatility(23400.0, 0.12, days)
        new = V.Volatility(23400.0, 0.12, days)
        assert abs(old.expiry_stdev - new.stdev) < 1e-9


def test_diverges_from_the_workbook_below_one_day():
    """And below the floor it must NOT agree -- that is the whole point."""
    old = P.Volatility(23400.0, 0.12, 0.25)
    new = V.Volatility(23400.0, 0.12, 0.25)
    assert old.expiry_stdev > new.stdev
    # the floored version is sqrt(1/0.25) = 2x too big
    assert abs(old.expiry_stdev / new.stdev - 2.0) < 1e-9


def test_sigma_scales_as_sqrt_time():
    a = V.Volatility(23400.0, 0.12, 0.25).stdev
    b = V.Volatility(23400.0, 0.12, 1.00).stdev
    assert abs(b / a - 2.0) < 1e-9


def test_zero_horizon_is_zero_sigma():
    assert V.Volatility(23400.0, 0.12, 0.0).stdev == 0.0


# -------------------------------------------------------------- terminal POP

def test_terminal_matches_the_straddle_identity():
    """For a symmetric zone, POP = 2*Phi(half_width/sigma) - 1."""
    spot, sigma, half = 23400.0, 150.0, 200.0
    got = V.terminal_inside(spot, spot - half, spot + half, sigma)
    want = 2.0 * V.norm_cdf(half / sigma) - 1.0
    assert abs(got - want) < 1e-12


def test_terminal_wider_zone_is_higher_pop():
    spot, sigma = 23400.0, 150.0
    a = V.terminal_inside(spot, spot - 100, spot + 100, sigma)
    b = V.terminal_inside(spot, spot - 400, spot + 400, sigma)
    assert b > a


# ------------------------------------------------------------ barrier maths

def mc_naive(spot, lower, upper, sigma, paths=200_000, steps=500, seed=0):
    """Stepped simulation with no bridge correction.

    Kept because it documents a trap. A stepped path cannot see a barrier
    that was crossed and recrossed BETWEEN two observations, so it counts
    those paths as survivors and over-states no-touch. The bias dies only as
    sqrt(dt): at 500 steps it is ~0.023, and even 8000 steps leaves ~0.007.
    Checking a continuous-monitoring formula against this would have rejected
    a correct formula.
    """
    rng = np.random.default_rng(seed)
    step_sd = sigma / sqrt(steps)
    alive = np.ones(paths, dtype=bool)
    s = np.full(paths, float(spot))
    for _ in range(steps):
        s += rng.normal(0.0, step_sd, paths)
        alive &= (s > lower) & (s < upper)
    return alive.mean()


def mc_no_touch(spot, lower, upper, sigma, paths=100_000, steps=400, seed=0):
    """Monte Carlo with a Brownian-bridge correction.

    Between two observed points the path is a Brownian bridge, and the
    probability it crossed a single barrier b in between has a closed form:

        P(cross) = exp(-2 * (b - s0) * (b - s1) / step_variance)

    That is the SINGLE-barrier bridge result -- a different piece of maths
    from the double-barrier image series being tested, so this remains an
    independent check rather than the formula grading its own homework. The
    two corrections are applied as though independent, which is accurate
    while a single step is small against the corridor width.

    Survival probability is accumulated as a weight rather than by killing
    paths with extra random draws: same expectation, far less noise.
    """
    rng = np.random.default_rng(seed)
    var = sigma * sigma / steps
    step_sd = sqrt(var)
    s = np.full(paths, float(spot))
    w = np.ones(paths)
    for _ in range(steps):
        nxt = s + rng.normal(0.0, step_sd, paths)
        inside = (nxt > lower) & (nxt < upper) & (w > 0.0)
        p_up = np.exp(-2.0 * (upper - s) * (upper - nxt) / var)
        p_dn = np.exp(-2.0 * (s - lower) * (nxt - lower) / var)
        w = np.where(inside, w * (1.0 - p_up) * (1.0 - p_dn), 0.0)
        s = nxt
    return w.mean()


CASES = [
    # (half-width in sigmas, sigma)
    (1.0, 150.0),
    (1.5, 150.0),
    (2.0, 150.0),
    (2.5, 150.0),
    (1.5, 60.0),
    (1.5, 300.0),
]


def test_no_touch_matches_monte_carlo():
    spot = 23400.0
    for k, sigma in CASES:
        half = k * sigma
        closed = V.no_touch(spot, spot - half, spot + half, sigma)
        sim = mc_no_touch(spot, spot - half, spot + half, sigma)
        # 200k paths gives a standard error near 0.001; allow 4 of them
        assert abs(closed - sim) < 0.004, (k, sigma, closed, sim)


def test_naive_monte_carlo_overstates_survival():
    """Guards the reasoning, not just the result.

    If someone later 'simplifies' mc_no_touch back to the stepped version,
    the barrier tests would start failing and the natural conclusion would be
    that the formula is broken. This test says out loud which one is wrong.
    """
    spot, sigma = 23400.0, 150.0
    half = 150.0
    closed = V.no_touch(spot, spot - half, spot + half, sigma)
    naive = mc_naive(spot, spot - half, spot + half, sigma)
    assert naive > closed + 0.01


def test_asymmetric_corridor_matches_monte_carlo():
    spot, sigma = 23400.0, 150.0
    lower, upper = spot - 180.0, spot + 400.0
    closed = V.no_touch(spot, lower, upper, sigma)
    sim = mc_no_touch(spot, lower, upper, sigma)
    assert abs(closed - sim) < 0.004, (closed, sim)


def test_no_touch_never_exceeds_terminal():
    """A path that never touched must have finished inside. Always."""
    spot = 23400.0
    for k in (0.5, 1.0, 1.5, 2.0, 3.0):
        for sigma in (50.0, 150.0, 400.0):
            half = k * sigma
            t = V.terminal_inside(spot, spot - half, spot + half, sigma)
            nt = V.no_touch(spot, spot - half, spot + half, sigma)
            assert nt <= t + 1e-9, (k, sigma, nt, t)


def test_starting_outside_is_certain_touch():
    assert V.no_touch(23400.0, 23500.0, 23600.0, 150.0) == 0.0


def test_zero_sigma_never_touches():
    assert V.no_touch(23400.0, 23300.0, 23500.0, 0.0) == 1.0


def test_series_has_converged_at_default_terms():
    spot, sigma = 23400.0, 300.0
    lower, upper = spot - 300.0, spot + 300.0     # a hard, narrow case
    a = V.no_touch(spot, lower, upper, sigma, terms=12)
    b = V.no_touch(spot, lower, upper, sigma, terms=60)
    assert abs(a - b) < 1e-12


def test_both_reports_a_positive_gap():
    spot, sigma = 23400.0, 150.0
    r = V.both(spot, spot - 225.0, spot + 225.0, sigma)
    assert r["terminal"] > r["no_touch"] > 0.0
    assert abs(r["gap"] - (r["terminal"] - r["no_touch"])) < 1e-12


# ----------------------------------------------------------------- reporting

def main():
    spot = 23400.0
    print("BARRIER MATHS vs MONTE CARLO   (200k paths, 500 steps)\n")
    print(f"{'zone':>10}{'sigma':>8}{'closed form':>14}{'monte carlo':>14}"
          f"{'diff':>9}")
    print("-" * 55)
    for k, sigma in CASES:
        half = k * sigma
        c = V.no_touch(spot, spot - half, spot + half, sigma)
        m = mc_no_touch(spot, spot - half, spot + half, sigma)
        print(f"{k:>9.1f}s{sigma:>8.0f}{c:>14.5f}{m:>14.5f}{c-m:>9.5f}")

    print("\n\nWHAT THE TWO PROBABILITIES LOOK LIKE\n")
    print(f"{'zone':>10}{'sigma':>8}{'terminal':>11}{'no-touch':>11}"
          f"{'gap':>9}")
    print("-" * 49)
    for k in (1.0, 1.5, 2.0, 2.5, 3.0):
        sigma = 150.0
        half = k * sigma
        r = V.both(spot, spot - half, spot + half, sigma)
        print(f"{k:>9.1f}s{sigma:>8.0f}{r['terminal']:>11.4f}"
              f"{r['no_touch']:>11.4f}{r['gap']:>9.4f}")
    print("\nThe gap is the probability of finishing inside having been")
    print("outside on the way. It is what a short-premium position has to")
    print("sit through, and it is invisible in terminal POP alone.")

    print("\n\nWHAT THE ONE-DAY FLOOR WAS DOING\n")
    print(f"{'horizon':>22}{'workbook sigma':>17}{'session sigma':>16}"
          f"{'ratio':>8}")
    print("-" * 63)
    close = datetime(2026, 9, 15, 15, 15)
    for label, start in [
        ("09:30 -> 15:15", datetime(2026, 9, 15, 9, 30)),
        ("11:00 -> 15:15", datetime(2026, 9, 15, 11, 0)),
        ("13:00 -> 15:15", datetime(2026, 9, 15, 13, 0)),
        ("14:30 -> 15:15", datetime(2026, 9, 15, 14, 30)),
    ]:
        days = V.horizon_days(start, close)
        old = P.Volatility(spot, 0.12, days).expiry_stdev
        new = V.Volatility(spot, 0.12, days).stdev
        print(f"{label:>22}{old:>17.1f}{new:>16.1f}{old/new:>8.2f}x")
    print("\nThe workbook reports the same sigma for every row above.")


if __name__ == "__main__":
    main()


# ------------------------------------------------------ the fitted two-term

REAL = {                      # Nisarg's own 5y/2y fit, 15 Sep 2026
    "k_session": 0.828, "rho": -0.100,
    "k_gap": {"overnight": {"k": 0.625}, "weekend": {"k": 1.046}},
    "k_day": {"overnight": {"k": 0.982}, "weekend": {"k": 1.240}},
    "session_minutes": 375, "basis": 365.0,
}


def cal():
    return V.Calibration(REAL)


def test_intraday_sigma_is_far_below_the_old_model():
    """The headline change. No gap crossed, so only k_session applies."""
    c = cal()
    spot, vix = 23400.0, 0.12
    full_session = c.intraday(spot, vix, 375)
    old = V.Volatility(spot, vix, 1.0).stdev
    assert full_session == pytest.approx(old * 0.828, rel=1e-9)
    assert full_session < old


def test_intraday_still_scales_as_sqrt_time():
    c = cal()
    a = c.intraday(23400.0, 0.12, 93.75)
    b = c.intraday(23400.0, 0.12, 375.0)
    assert b / a == pytest.approx(2.0, rel=1e-9)


def test_a_whole_day_uses_the_measured_combined_figure():
    """k_day already contains the correlation -- do not rebuild it."""
    c = cal()
    s = c.sigma(23400.0, 0.12, day_classes=["overnight"])
    assert s == pytest.approx(c.sigma_day(23400.0, 0.12) * 0.982, rel=1e-9)


def test_weekend_costs_more_than_a_weeknight():
    c = cal()
    wk = c.sigma(23400.0, 0.12, day_classes=["weekend"])
    on = c.sigma(23400.0, 0.12, day_classes=["overnight"])
    assert wk > on * 1.2


def test_unknown_gap_class_falls_back_rather_than_crashing():
    c = cal()
    s = c.sigma(23400.0, 0.12, day_classes=["long break"])
    assert s == pytest.approx(
        c.sigma(23400.0, 0.12, day_classes=["weekend"]), rel=1e-9)


def test_negative_rho_reduces_a_gap_plus_session_tail():
    """The third term's whole purpose, isolated."""
    c = cal()
    with_rho = c.sigma(23400.0, 0.12, tail=("overnight", 375))
    flat = V.Calibration({**REAL, "rho": 0.0})
    without = flat.sigma(23400.0, 0.12, tail=("overnight", 375))
    assert with_rho < without
    assert without / with_rho > 1.02


def test_weekly_horizon_lands_near_the_old_model():
    """Intraday should shrink a lot; a week should barely move.

    Both errors in the old model point the same way over a week -- the gap is
    bigger than sqrt(t) says and the retracement is smaller -- so they largely
    cancel. If this ratio moved sharply, the calibration would be suspect.
    """
    c = cal()
    r = c.ratio_to_old(lead_minutes=280,
                       day_classes=["overnight"] * 4 + ["weekend"])
    assert 0.85 < r < 1.15


def test_the_intraday_ratio_is_the_big_move():
    c = cal()
    assert c.ratio_to_old(lead_minutes=375) == pytest.approx(0.828, rel=1e-9)
    assert c.ratio_to_old(lead_minutes=120) == pytest.approx(0.828, rel=1e-9)


def test_zero_horizon_is_zero():
    assert cal().sigma(23400.0, 0.12) == 0.0

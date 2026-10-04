"""pricing.py against the golden fixture pulled from the workbook.

Phase 2 exit gate: every number reproduced to within 0.5% on net EV. In
practice these come out to floating-point noise -- if a tolerance here ever
has to be loosened, something changed in the model and it should be a
deliberate, measured change (2.4), not a slackened test.
"""

import project_paths
import json
import os
from math import sqrt, pi

import pytest

import pricing as P

FIXTURE = os.path.join(project_paths.ROOT, "fixtures",
                       "daily_signal_2026-08-28.json")


@pytest.fixture(scope="module")
def fx():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def vol(fx):
    i = fx["inputs"]
    return P.Volatility(i["spot"], i["vix"], i["days_to_expiry"])


@pytest.fixture(scope="module")
def costs(fx):
    c = fx["costs"]
    return P.CostModel(
        brokerage_per_order=c["brokerage_per_order"],
        orders_per_round_trip=c["orders_per_round_trip"],
        stt_on_sell=c["stt_on_sell"],
        exchange_per_side=c["exchange_per_side"],
        sebi_per_side=c["sebi_per_side"],
        stamp_buy=c["stamp_buy"],
        gst=c["gst"],
        slippage_per_leg=c["slippage_per_leg"],
        legs_per_round_trip=c["legs_per_round_trip"],
    )


DEFINED = {"iron_condor", "iron_butterfly", "iron_condor_wide"}


# ------------------------------------------------------------- primitives

def test_norm_cdf_known_points():
    assert P.norm_cdf(0.0) == pytest.approx(0.5)
    assert P.norm_cdf(1.0) == pytest.approx(0.8413447, abs=1e-7)
    assert P.norm_cdf(-1.96) == pytest.approx(0.0249979, abs=1e-7)


def test_mround_matches_excel():
    assert P.mround(24083.65, 50) == 24100
    assert P.mround(24074.0, 50) == 24050           # below the half, rounds down
    assert P.mround(24075.0, 50) == 24100           # exact half rounds AWAY from zero
    assert P.mround(-24075.0, 50) == -24100         # ...in both directions


def test_fair_straddle_is_atm_call_plus_put(vol):
    """J9's closed form must equal the general payoff formula at the money."""
    s, sig = 24083.65, vol.expiry_stdev
    composed = P.expected_call(s, s, sig) + P.expected_put(s, s, sig)
    assert P.fair_straddle(sig) == pytest.approx(composed, rel=1e-12)


# ------------------------------------------------------------- vol chain

def test_volatility_chain(fx, vol):
    i = fx["inputs"]
    assert vol.daily_vol == pytest.approx(i["daily_vol"], rel=1e-12)
    assert vol.expiry_vol == pytest.approx(i["expiry_vol"], rel=1e-12)
    assert vol.daily_stdev == pytest.approx(i["daily_stdev"], rel=1e-12)
    assert vol.expiry_stdev == pytest.approx(i["expiry_stdev"], rel=1e-12)


def test_atm_matches(fx):
    i = fx["inputs"]
    assert P.mround(i["spot"], i["strike_step"]) == i["atm"]


# ------------------------------------------------- per-structure reproduction

def _expected_payoff(name, d, spot, sigma):
    """Compose row 36 for one structure from the pricing primitives."""
    v = (P.expected_call(spot, d["ce_strike"], sigma)
         + P.expected_put(spot, d["pe_strike"], sigma))
    if name in DEFINED:
        v -= P.expected_call(spot, d["long_ce_strike"], sigma)
        v -= P.expected_put(spot, d["long_pe_strike"], sigma)
    return v


def _structures(fx):
    return [(n, d) for n, d in fx["expected"].items()
            if d.get("credit_pts") is not None]


def test_every_structure_has_a_fixture(fx):
    assert len(_structures(fx)) == 7


@pytest.mark.parametrize("name", [
    "short_straddle", "intraday_strangle", "expiry_strangle", "maxev_strangle",
    "iron_condor", "iron_butterfly", "iron_condor_wide",
])
def test_breakevens_and_pop(fx, vol, name):
    d = fx["expected"][name]
    spot, sigma = fx["inputs"]["spot"], vol.expiry_stdev

    assert d["lower_be"] == pytest.approx(d["pe_strike"] - d["credit_pts"], rel=1e-9)
    assert d["upper_be"] == pytest.approx(d["ce_strike"] + d["credit_pts"], rel=1e-9)

    pop = P.probability_of_profit(spot, d["lower_be"], d["upper_be"], sigma)
    assert pop == pytest.approx(d["pop"], rel=1e-9)


@pytest.mark.parametrize("name", [
    "short_straddle", "intraday_strangle", "expiry_strangle", "maxev_strangle",
    "iron_condor", "iron_butterfly", "iron_condor_wide",
])
def test_expected_payoff(fx, vol, name):
    d = fx["expected"][name]
    got = _expected_payoff(name, d, fx["inputs"]["spot"], vol.expiry_stdev)
    assert got == pytest.approx(d["exp_payoff_pts"], rel=1e-9)


@pytest.mark.parametrize("name", [
    "short_straddle", "intraday_strangle", "expiry_strangle", "maxev_strangle",
    "iron_condor", "iron_butterfly", "iron_condor_wide",
])
def test_costs_and_slippage(fx, costs, name):
    d = fx["expected"][name]
    lot, lots = fx["inputs"]["lot_size"], d["lots"]
    dr = name in DEFINED

    fees = P.transaction_costs(d["credit_pts"], lot, lots, dr, costs)
    assert fees == pytest.approx(d["fees_rs"], rel=1e-9)

    slip = P.slippage(lot, lots, dr, costs)
    assert slip == pytest.approx(d["slippage_rs"], rel=1e-12)


@pytest.mark.parametrize("name", [
    "short_straddle", "intraday_strangle", "expiry_strangle", "maxev_strangle",
    "iron_condor", "iron_butterfly", "iron_condor_wide",
])
def test_net_ev_within_phase2_tolerance(fx, vol, costs, name):
    """The Phase 2 exit gate: net EV within 0.5% of the workbook."""
    d = fx["expected"][name]
    lot, lots = fx["inputs"]["lot_size"], d["lots"]
    payoff = _expected_payoff(name, d, fx["inputs"]["spot"], vol.expiry_stdev)

    gross = P.gross_ev(d["credit_pts"], payoff, lot, lots)
    assert gross == pytest.approx(d["gross_ev_rs"], rel=1e-9)

    net = P.net_ev(d["credit_pts"], payoff, lot, lots, name in DEFINED, costs)
    assert net == pytest.approx(d["net_ev_rs"], rel=0.005)
    # and, in fact, far tighter than the gate requires
    assert net == pytest.approx(d["net_ev_rs"], rel=1e-9)


# ------------------------------------------------------------ sanity checks

def test_rich_cheap_ratio_on_the_snapshot(fx, vol):
    """28 Aug: the workbook called it NEUTRAL and every structure had negative
    net EV. credit/sigma was 0.72 -- inside the dead zone the bhavcopy study
    identified independently. The two methods should agree."""
    straddle = fx["expected"]["short_straddle"]["credit_pts"]
    ratio = P.rich_cheap_ratio(straddle, vol.expiry_stdev)
    assert 0.90 < ratio < 1.10, "workbook verdict was NEUTRAL"
    assert straddle / vol.expiry_stdev == pytest.approx(0.7219, abs=1e-3)
    for name, d in _structures(fx):
        assert d["net_ev_rs"] < 0, f"{name} should be a no-trade on this day"


def test_stop_touch_probability_is_bounded():
    assert P.probability_stop_touched(1.0) == 0.0
    assert P.probability_stop_touched(0.5) == 1.0
    assert P.probability_stop_touched(0.2) == 1.0     # clamped, not 1.6

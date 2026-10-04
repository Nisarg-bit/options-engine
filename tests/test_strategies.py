"""strategies.py against the golden fixture -- the Phase 2 exit gate.

Reproduces Daily_Signal columns B, C, D, E, L, M, N in full: strikes,
premiums, credit, breakevens, POP, expected payoff, charges, slippage,
margin and net EV. Tolerance is 0.5% on net EV per the plan; in practice
everything lands at floating-point noise, and the tight assertions below are
deliberate -- if one ever has to be loosened, the model changed.
"""

import project_paths
import json
import os

import pytest

import pricing as P
import strategies as S

FIXTURE = os.path.join(project_paths.ROOT, "fixtures",
                       "daily_signal_2026-08-28.json")

ALL = ["short_straddle", "intraday_strangle", "expiry_strangle",
       "maxev_strangle", "iron_condor", "iron_butterfly", "iron_condor_wide"]
DEFINED = ["iron_condor", "iron_butterfly", "iron_condor_wide"]
UNDEFINED = [n for n in ALL if n not in DEFINED]


@pytest.fixture(scope="module")
def fx():
    with open(FIXTURE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def built(fx):
    i, c = fx["inputs"], fx["costs"]
    costs = P.CostModel(
        c["brokerage_per_order"], c["orders_per_round_trip"], c["stt_on_sell"],
        c["exchange_per_side"], c["sebi_per_side"], c["stamp_buy"], c["gst"],
        c["slippage_per_leg"], c["legs_per_round_trip"])
    return S.build_all(
        fx["chain"], i["spot"], i["vix"], i["days_to_expiry"],
        i["strike_step"], i["lot_size"], lots=1,
        wing_width=i["wing_width"], sigma_mult=i["wide_sigma_mult"],
        margin_per_lot=fx["risk_sizing"]["margin_per_lot"], costs=costs)


# ------------------------------------------------------- strike selection

@pytest.mark.parametrize("name", ALL)
def test_strikes(fx, built, name):
    """Strike rules are where a port goes wrong silently -- an off-by-one-step
    error still produces plausible numbers."""
    e, s = fx["expected"][name], built[name]
    assert s.ce_strike == e["ce_strike"]
    assert s.pe_strike == e["pe_strike"]
    assert s.pe_strike <= s.ce_strike, "inverted strikes (Daily_Signal H21)"


@pytest.mark.parametrize("name", DEFINED)
def test_wing_strikes(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.long_ce_strike == e["long_ce_strike"]
    assert s.long_pe_strike == e["long_pe_strike"]
    assert s.long_ce_strike > s.ce_strike
    assert s.long_pe_strike < s.pe_strike


@pytest.mark.parametrize("name", ALL)
def test_premiums_come_from_the_chain(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.ce_premium == pytest.approx(e["ce_premium"], rel=1e-12)
    assert s.pe_premium == pytest.approx(e["pe_premium"], rel=1e-12)


# -------------------------------------------------------------- economics

@pytest.mark.parametrize("name", ALL)
def test_credit_and_breakevens(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.credit_pts == pytest.approx(e["credit_pts"], rel=1e-9)
    assert s.credit_rs == pytest.approx(e["credit_rs"], rel=1e-9)
    assert s.lower_be == pytest.approx(e["lower_be"], rel=1e-9)
    assert s.upper_be == pytest.approx(e["upper_be"], rel=1e-9)
    assert s.profit_zone == pytest.approx(e["profit_zone"], rel=1e-9)


@pytest.mark.parametrize("name", ALL)
def test_pop_and_payoff(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.pop == pytest.approx(e["pop"], rel=1e-9)
    assert s.exp_payoff_pts == pytest.approx(e["exp_payoff_pts"], rel=1e-9)


@pytest.mark.parametrize("name", ALL)
def test_costs(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.fees_rs == pytest.approx(e["fees_rs"], rel=1e-9)
    assert s.slippage_rs == pytest.approx(e["slippage_rs"], rel=1e-12)


@pytest.mark.parametrize("name", ALL)
def test_net_ev_meets_phase2_gate(fx, built, name):
    """The gate: net EV within 0.5% of the workbook."""
    e, s = fx["expected"][name], built[name]
    assert s.gross_ev_rs == pytest.approx(e["gross_ev_rs"], rel=1e-9)
    assert s.net_ev_rs == pytest.approx(e["net_ev_rs"], rel=0.005)
    assert s.net_ev_rs == pytest.approx(e["net_ev_rs"], rel=1e-9)


@pytest.mark.parametrize("name", ALL)
def test_margin(fx, built, name):
    e, s = fx["expected"][name], built[name]
    assert s.margin == pytest.approx(e["margin"], rel=1e-9)


# ---------------------------------------------------------- risk structure

@pytest.mark.parametrize("name", DEFINED)
def test_defined_risk_is_actually_capped(fx, built, name):
    """The whole point of a condor: loss cannot exceed wing width less credit."""
    e, s = fx["expected"][name], built[name]
    assert s.defined_risk and s.risk_type == "DEFINED"
    assert s.max_loss_pts == pytest.approx(e["max_loss_pts"], rel=1e-9)
    assert s.wing_cost == pytest.approx(e["wing_cost"], rel=1e-9)
    assert s.max_loss_pts == pytest.approx(s.wing_width - s.credit_pts, rel=1e-9)
    assert s.margin == pytest.approx(s.max_loss_pts * s.lot_size * s.lots, rel=1e-9)


@pytest.mark.parametrize("name", UNDEFINED)
def test_undefined_risk_margins_on_span_not_loss(built, name):
    s = built[name]
    assert not s.defined_risk and s.risk_type == "UNDEFINED"
    assert s.max_loss_pts == 0.0
    assert s.margin == 240000.0     # margin_per_lot * lots * 2


# ------------------------------------------------------- workbook verdicts

def test_no_trade_verdict_on_this_snapshot(built):
    """Every structure loses money after costs on 28 Aug -- the workbook's
    B48 says 'NO TRADE'. best() must agree by returning None."""
    assert all(s.net_ev_rs < 0 for s in built.values())
    assert S.best(built) is None


def test_maxev_degenerates_to_the_inner_otm_strikes(fx, built):
    """Column E's search maximises P(worthless) x premium, which has no loss
    term and rises monotonically toward the money. Its maximum therefore sits
    on the innermost candidate, one step out on each side -- so the 'max-EV'
    strangle is really a fixed ATM +/- 1 strangle. See MAXEV_SEARCH_NOTE."""
    i = fx["inputs"]
    atm, step = i["atm"], i["strike_step"]
    s = built["maxev_strangle"]
    assert s.ce_strike == atm + step
    assert s.pe_strike == atm - step
    # ...and so it collects exactly the OTM-1 strangle credit the sheet
    # reports separately in Option_Chain!B21.
    assert s.credit_pts == pytest.approx(152.85, rel=1e-9)


def test_best_picks_highest_net_ev_when_one_is_positive(built):
    """Force a winner and check best() ranks on net EV (B48), not the legacy
    row-23 EV that rows 28-29 warn about."""
    import copy
    forced = {k: copy.copy(v) for k, v in built.items()}
    forced["iron_condor"].net_ev_rs = 5000.0
    forced["short_straddle"].net_ev_rs = 4999.0
    assert S.best(forced).name == "iron_condor"

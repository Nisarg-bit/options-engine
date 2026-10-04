"""What a trade is charged, and the one place that decides it.

Two bugs of the same shape, a year apart. STT on the sale of options rose to
0.15% on 1 April 2026; five modules were updated and two were not, and the
backtest ran on the old rate for months. The NSE exchange transaction charge
fell from 0.0495% to 0.03503% on 1 October 2024 and then to 0.03553% on
1 March 2026; all seven modules kept 0.0495% for two years, and one of them
carried a comment calling it "current".

Neither was a hard number to get right. Both came from the rate being written
out in seven places. So the rates now live in pricing.py alone, and the first
test here fails if an eighth copy ever appears.

Run:  python -m pytest test_costs.py -q
"""

import glob
import os
import re

import pytest

import api as A
import pricing as P


ROOT = os.path.dirname(os.path.abspath(__file__))
LOT, CREDIT = 65, 200.0


# ------------------------------------------------------- one definition only

def _sources():
    for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        name = os.path.basename(path)
        if name.startswith("test_") or name == "pricing.py":
            continue
        yield name, open(path, encoding="utf-8").read()


def test_no_module_builds_its_own_cost_model():
    """The guard against the bug recurring. A CostModel constructed anywhere
    but pricing.py is a rate that will be updated in one place and not the
    other; that is how the STT change missed two files and how 0.0495% sat
    stale in seven."""
    offenders = [n for n, src in _sources() if re.search(r"\bCostModel\(", src)]
    assert not offenders, f"cost models built outside pricing.py: {offenders}"


@pytest.mark.parametrize("module", [
    "daily_signal.py", "intraday.py", "intraday_vrp.py", "vrp.py",
    "compare.py", "backtest_corrected.py"])
def test_every_pricing_script_points_at_the_single_definition(module):
    src = open(os.path.join(ROOT, module), encoding="utf-8").read()
    assert "COSTS = P.CURRENT_COSTS" in src


def test_the_api_names_are_aliases_not_copies():
    assert A.COSTS is P.CURRENT_COSTS
    assert A.costs_for is P.current_costs_for
    assert A.NSE_OPTION_EXCHANGE == P.NSE_OPTION_EXCHANGE


# ----------------------------------------------------------- the rates today

def test_nse_is_the_rate_billed_since_march_2026():
    assert P.NSE_OPTION_EXCHANGE == pytest.approx(0.0003553)
    assert P.CURRENT_COSTS.exchange_per_side == pytest.approx(0.0003553)


def test_the_stale_rate_is_gone():
    """0.0495% stopped being true on 1 October 2024."""
    assert P.CURRENT_COSTS.exchange_per_side != pytest.approx(0.000495)


def test_stt_is_the_april_2026_rate():
    assert P.CURRENT_COSTS.stt_on_sell == 0.0015


def test_the_workbook_default_is_untouched():
    """test_pricing reproduces the sheet to the rupee and depends on it."""
    assert P.DEFAULT_COSTS.exchange_per_side == 0.00035
    assert P.DEFAULT_COSTS.stt_on_sell == 0.001


# ----------------------------------------------------------- by exchange

def test_nse_underlyings_get_the_nse_rate():
    for name in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        assert P.current_costs_for(name).exchange_per_side == P.NSE_OPTION_EXCHANGE


def test_sensex_and_bankex_get_the_bse_rate():
    for name in ("SENSEX", "BANKEX"):
        assert P.current_costs_for(name).exchange_per_side == P.BSE_OPTION_EXCHANGE


def test_sensex50_has_its_own_lower_rate():
    """Rs 500 per crore, not SENSEX's Rs 3,250. An earlier version of this
    file asserted SENSEX50 was on the SENSEX tariff -- that was wrong, and the
    test was what encoded the mistake."""
    assert P.current_costs_for("SENSEX50").exchange_per_side == \
        pytest.approx(0.00005)


def test_an_unclassified_underlying_gets_the_dearest_rate():
    """A missing entry must make a trade look worse, not better."""
    assert P.NSE_OPTION_EXCHANGE > P.BSE_OPTION_EXCHANGE > P.BSE_SMALL_OPTION_EXCHANGE
    for name in ("SOMETHING_NEW", None):
        assert P.current_costs_for(name).exchange_per_side == P.NSE_OPTION_EXCHANGE


def test_every_model_carries_current_stt_whatever_the_exchange():
    for name in ("NIFTY", "SENSEX", "SENSEX50"):
        assert P.current_costs_for(name).stt_on_sell == P.STT_ON_SELL


def test_the_same_underlying_always_gets_the_same_object():
    assert P.current_costs_for("SENSEX") is P.current_costs_for("BANKEX")
    assert P.current_costs_for("NIFTY") is P.CURRENT_COSTS


# ------------------------------------------------------------ what it moves

def test_the_correction_lowers_costs_by_exactly_the_rate_gap():
    """Both sides of the round trip, plus the GST levied on the exchange
    charge -- and nothing else. If STT or stamp leaked into the difference,
    the two would differ by more than the one rate that changed."""
    stale = P.CostModel(exchange_per_side=0.000495, stt_on_sell=0.0015)
    now = P.transaction_costs(CREDIT, LOT, 1, costs=P.CURRENT_COSTS)
    was = P.transaction_costs(CREDIT, LOT, 1, costs=stale)
    gap = (0.000495 - P.NSE_OPTION_EXCHANGE) * CREDIT * LOT * 2 * (1 + P.CURRENT_COSTS.gst)
    assert was - now == pytest.approx(gap, abs=1e-9)
    assert 4.0 < gap < 4.5        # about Rs 4.29 on a 200-point straddle, one lot

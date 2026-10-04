"""Which underlying the entry gate is entitled to speak about.

CS_GATE = 0.9346 was fitted on two years of NIFTY bhavcopy, against that
index's own credit/sigma distribution at a POP of 0.65. Nothing about it is a
property of index options in general.

It had been rendering for BANKNIFTY since that underlying was collected --
different index, different lot, different strike step, volatility several
points higher, threshold borrowed wholesale, and nothing on the page saying
so. It read exactly like a NIFTY gate reading. Adding SENSEX is what exposed
it; the bug predates SENSEX by months.

Run:  python -m pytest test_gate_scope.py -q
"""

import pytest

import sigma_sources as SIG


SPOT, DTE, VIX = 23346.4, 4, 11.39


def res(underlying, source="vix", **kw):
    return SIG.resolve(source, SPOT, DTE, underlying=underlying,
                       vix_units=VIX, chain_iv_units=13.7,
                       session_moves=[120.0] * 20, **kw)


def test_the_gate_applies_to_the_index_it_was_fitted_on():
    r = res("NIFTY")
    assert r["gate_applies"] is True
    assert r["gate_note"] is None


@pytest.mark.parametrize("name", ["BANKNIFTY", "SENSEX", "BANKEX",
                                  "MIDCPNIFTY", "FINNIFTY"])
def test_and_to_nothing_else(name):
    r = res(name)
    assert r["gate_applies"] is False
    assert r["gate_note"]


def test_banknifty_specifically_because_it_used_to_pass(name="BANKNIFTY"):
    """Named on its own: this is the regression, not a hypothetical."""
    assert res(name)["gate_applies"] is False


def test_an_unnamed_underlying_loses_the_gate_rather_than_borrowing_one():
    """A caller that forgets to say which index it is asking about must fail
    visibly, not quietly receive NIFTY's threshold applied to something."""
    r = SIG.resolve("vix", SPOT, DTE, vix_units=VIX)
    assert r["gate_applies"] is False
    assert "no underlying was named" in r["gate_note"]


def test_the_note_says_what_the_gap_actually_is():
    note = res("SENSEX")["gate_note"]
    assert "0.9346" in note
    assert "NIFTY" in note and "SENSEX" in note
    assert "different lot" in note
    # and it does not pretend a refit is a small thing
    assert "study" in note


def test_the_payload_says_what_it_was_fitted_on():
    assert res("SENSEX")["gate_fitted_on"] == ["NIFTY"]


# --------------------------------------------- the two reasons stay separate

def test_a_wrong_source_on_the_right_index_keeps_the_source_reason():
    note = res("NIFTY", source="chain_iv")["gate_note"]
    assert "pinned near" in note
    assert "bhavcopy" not in note        # not the underlying's reason


def test_a_wrong_source_on_the_wrong_index_reports_the_source_first():
    """Both are true; the source reason is the more specific one and the one
    that would still apply after a refit."""
    note = res("SENSEX", source="realised")["gate_note"]
    assert "variance" in note


def test_sigma_itself_is_unaffected():
    """Withholding a verdict must not change the number underneath it."""
    a = res("NIFTY")["sigma"]
    b = res("SENSEX")["sigma"]
    # The payload rounds to two places; sigma_points does not. Comparing an
    # unrounded number to a rounded one to nine decimals was this test being
    # wrong about the contract, not the contract being wrong.
    assert a == b == pytest.approx(SIG.sigma_points(SPOT, VIX, DTE), abs=0.005)
    assert a == 278.37      # the live NIFTY front-expiry sigma, as it happens


def test_every_source_still_reports_all_three():
    for name in ("NIFTY", "SENSEX"):
        av = res(name)["sigma_available"]
        assert set(av) == {"vix", "chain_iv", "realised"}
        assert av["vix"] == pytest.approx(VIX)

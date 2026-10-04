"""The P&L decomposition has to add up, or it is decoration.

An attribution nobody can check is worse than none: it invites reading a
number as a measurement when it is really whatever was left over. So the
central test here is not that any one term looks sensible, it is that the
terms reconstruct `credit - cover` -- a quantity computed nowhere near this
module, from two market prices.

The sessions are built rather than loaded. A test that reads the parquet
would prove the decomposition agrees with itself on fourteen days; building
the session from the model says what it does on a forward that moved, on a
vol that fell, on a strangle, and on a spread that widened between entry and
exit -- none of which the journal happens to contain yet.

Run:  python -m pytest test_attribution.py -q
"""

from math import sqrt

import pytest

import attribution as A
import decay_model as DM
import pricing as P


LOT = 65
F0 = 23300.0
SIGMA_SESS = 120.0          # points per sqrt(session)
S_IN, S_OUT = 3.987, 3.040  # entry 09:20 and exit 15:15 on a 3-day expiry


def session(*, forward_exit=F0, legs=None, exit_vol_mult=1.0,
            spread_in=1.6, spread_out=2.4, strikes=None):
    """A simulate()-shaped dict built from the model, not from a parquet.

    `exit_vol_mult` is what the market's implied volatility did beyond pure
    time decay: 1.0 means it followed sqrt(time) exactly, which is the only
    case where the `vol` term should vanish.
    """
    legs = legs or [(F0, "CE"), (F0, "PE")]
    sigma_in = SIGMA_SESS * sqrt(S_IN)
    sigma_out = SIGMA_SESS * sqrt(S_OUT)

    mid_in = DM.structure_value(legs, F0, sigma_in)
    mid_out = DM.structure_value(legs, forward_exit, sigma_out * exit_vol_mult)

    credit = mid_in - spread_in / 2.0        # sold at the bid
    cover = mid_out + spread_out / 2.0       # bought back at the ask
    fees = P.transaction_costs(credit, LOT, 1, defined_risk=False)
    return {
        "legs": legs, "fwd_open": F0, "fwd_exit": forward_exit,
        "sigma_session": SIGMA_SESS, "s_in": S_IN, "s_out": S_OUT,
        "credit": credit, "cover": cover,
        "spread_at_entry": spread_in, "spread_at_exit": spread_out,
        "pnl_pts": credit - cover, "fees_rs": fees,
    }


# ------------------------------------------------------- it has to add up

def test_the_terms_reconstruct_credit_minus_cover():
    a = A.attribute(session(forward_exit=F0 + 140.0, exit_vol_mult=0.93), LOT)
    assert a["unexplained_pts"] == pytest.approx(0.0, abs=1e-9)
    assert a["modelled_pts"] == pytest.approx(a["pnl_pts"], abs=1e-9)


@pytest.mark.parametrize("move", [-420.0, -85.0, 0.0, 85.0, 420.0])
@pytest.mark.parametrize("volmult", [0.80, 1.0, 1.15])
def test_it_still_adds_up_wherever_the_day_went(move, volmult):
    a = A.attribute(session(forward_exit=F0 + move, exit_vol_mult=volmult), LOT)
    assert a["unexplained_pts"] == pytest.approx(0.0, abs=1e-9)


def test_net_rupees_matches_the_journal_arithmetic():
    s = session(forward_exit=F0 + 60.0)
    a = A.attribute(s, LOT)
    assert a["net_rs"] == pytest.approx(s["pnl_pts"] * LOT - s["fees_rs"],
                                        abs=1e-6)


# --------------------------------------------------------------- the terms

def test_decay_is_positive_and_is_the_whole_story_on_a_still_day():
    """Forward unchanged, vol on its sqrt(time) path: only time happened."""
    a = A.attribute(session(forward_exit=F0, exit_vol_mult=1.0), LOT)
    assert a["pts"]["decay"] > 0
    assert a["pts"]["direction"] == pytest.approx(0.0, abs=1e-9)
    assert a["pts"]["vol"] == pytest.approx(0.0, abs=1e-9)


def test_direction_is_negative_whichever_way_the_forward_goes():
    """A short straddle does not care about the sign of the move."""
    up = A.attribute(session(forward_exit=F0 + 200.0), LOT)["pts"]["direction"]
    dn = A.attribute(session(forward_exit=F0 - 200.0), LOT)["pts"]["direction"]
    assert up < 0 and dn < 0
    assert up == pytest.approx(dn, abs=1e-6)     # symmetric about the strike


def test_a_bigger_move_costs_more():
    small = A.attribute(session(forward_exit=F0 + 50.0), LOT)["pts"]["direction"]
    big = A.attribute(session(forward_exit=F0 + 250.0), LOT)["pts"]["direction"]
    assert big < small


def test_falling_vol_pays_the_seller():
    assert A.attribute(session(exit_vol_mult=0.85), LOT)["pts"]["vol"] > 0
    assert A.attribute(session(exit_vol_mult=1.15), LOT)["pts"]["vol"] < 0


# ------------------------------------------------ the spread, both ends

def test_the_spread_is_half_of_each_end_not_the_entry_twice():
    a = A.attribute(session(spread_in=1.6, spread_out=2.4), LOT)
    assert a["pts"]["spread"] == pytest.approx(-(1.6 + 2.4) / 2.0, abs=1e-9)
    # The shortcut this guards against -- charging the entry spread twice --
    # would read -1.6 here and quietly move 0.4 points into the decay.
    assert a["pts"]["spread"] != pytest.approx(-1.6, abs=1e-6)


def test_a_spread_that_widened_intraday_is_charged_where_it_happened():
    tight = A.attribute(session(spread_in=1.6, spread_out=1.6), LOT)
    wide = A.attribute(session(spread_in=1.6, spread_out=6.0), LOT)
    assert wide["pts"]["spread"] < tight["pts"]["spread"]
    # and it comes out of the P&L, not out of the decay
    assert wide["pts"]["decay"] == pytest.approx(tight["pts"]["decay"], abs=1e-9)


# ------------------------------------------------------- the entry gap

def test_the_entry_gap_vanishes_on_the_straddle_this_journal_trades():
    a = A.attribute(session(), LOT)
    assert a["pts"]["entry_gap"] == pytest.approx(0.0, abs=1e-9)


def test_but_a_strangle_is_not_free_of_it():
    """sigma is inverted from the AT-THE-MONEY straddle. The wings are not at
    the money, so the model does not land on their mid and the difference has
    to show up as a residual rather than hiding inside `vol`."""
    wings = [(F0 + 200.0, "CE"), (F0 - 200.0, "PE")]
    s = session(legs=wings)
    # Price the wings at a mid the model does NOT produce, which is what a
    # real chain does: a skewed market.
    s["credit"] = s["credit"] * 1.06
    s["pnl_pts"] = s["credit"] - s["cover"]
    a = A.attribute(s, LOT)
    assert abs(a["pts"]["entry_gap"]) > 0.01
    assert a["unexplained_pts"] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------- refusals

@pytest.mark.parametrize("missing", ["fwd_exit", "sigma_session",
                                     "spread_at_exit", "s_in"])
def test_an_unvaluable_session_returns_nothing_rather_than_part_of_an_answer(missing):
    s = session()
    s[missing] = None
    assert A.attribute(s, LOT) is None


def test_blank_columns_are_blank_not_zero():
    """A session that could not be valued and one whose decay really was zero
    must not read the same on the page."""
    f = A.row_fields(None)
    assert set(f) == {"decay_pts", "direction_pts", "vol_pts", "spread_pts",
                      "entry_gap_pts", "unexplained_pts"}
    assert all(v == "" for v in f.values())


def test_row_fields_round_for_the_csv_without_losing_the_check():
    f = A.row_fields(A.attribute(session(forward_exit=F0 + 33.0), LOT))
    assert isinstance(f["decay_pts"], float)
    assert f["unexplained_pts"] == pytest.approx(0.0, abs=1e-4)

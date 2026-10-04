"""calpop.py -- the calibrated POP. Run: python -m pytest test_calpop.py -q"""
from datetime import date, timedelta

import pytest

import calpop as C

WEEKDAY = lambda d: d.weekday() < 5          # a calendar with no holidays


def test_calibration_file_is_present_and_fitted_out_of_sample():
    c = C.load()
    assert c["model"] == "empirical_z" and len(c["z"]) > 400
    assert "held-out" in c["chosen_by"]


def test_weekend_counts_as_the_measured_gap_not_three_days():
    fri, mon = date(2026, 9, 25), date(2026, 9, 28)
    share, g = 0.6626, 2.57
    assert C.session_units(fri, mon, WEEKDAY, share, {"3": g}) == pytest.approx(share + (1 - share) * g)
    tue, wed = date(2026, 9, 22), date(2026, 9, 23)
    assert C.session_units(tue, wed, WEEKDAY, share, {"3": g}) == pytest.approx(1.0)
    # a weekend is well under three ordinary days
    assert C.session_units(fri, mon, WEEKDAY, share, {"3": g}) < 3.0


def test_zero_dte_has_a_floor():
    d = date(2026, 9, 22)
    assert C.session_units(d, d, WEEKDAY) == 0.25


def test_cdf_is_a_cdf():
    vals = [C.cdf(23400 + k * 100, 23400, 300) for k in range(-12, 13)]
    assert vals[0] < 0.01 and vals[-1] > 0.99
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_zone_and_curve_agree():
    xs = [21000 + i * 2.0 for i in range(2400)]
    ys = [1.0 if 23100 < x < 23700 else -1.0 for x in xs]
    assert C.pop_curve(xs, ys, 23400, 300) == pytest.approx(C.pop_zone(23100, 23700, 23400, 300), abs=3e-3)


def test_realised_moves_were_narrower_than_vix_sigma():
    """The finding it encodes: a +/-1 sigma zone held more than the normal 68%."""
    p = C.pop_zone(23400 - 300, 23400 + 300, 23400, 300)
    assert p > 0.70


def test_only_nifty():
    assert C.available("NIFTY") and not C.available("BANKNIFTY")


def test_public_payload():
    p = C.public()
    assert p["z"] and p["bandwidth"] > 0 and p["underlyings"] == ["NIFTY"]

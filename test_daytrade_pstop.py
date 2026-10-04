"""daytrade's modelled P(stop) columns and the stop calibration summary.

Run:  python -m pytest test_daytrade_pstop.py -q
"""
from datetime import date

import pytest

import daytrade as D
import stop_touch as T

WEEKDAY = lambda d: d.weekday() < 5
DAY, EXP = date(2026, 9, 21), date(2026, 9, 22)


def book(fwd=23380.0, atm=23400.0, straddle=150.0, half=0.1):
    """A one-minute book at 09:20: ATM call/put priced so parity gives `fwd`."""
    c = straddle / 2 + (fwd - atm) / 2
    p = straddle / 2 - (fwd - atm) / 2
    q = lambda m: (round(m - half, 2), round(m + half, 2))
    return {"09:20": {atm: {"CE": q(c), "PE": q(p)},
                      atm - 50: {"CE": q(c + 30), "PE": q(p + 5)},
                      atm + 50: {"CE": q(c - 25), "PE": q(p + 30)}}}


SIM = {"entry": "09:20", "atm": 23400.0}


def fields(**kw):
    args = dict(vix=11.58, session_share=0.6626, is_trading_day=WEEKDAY)
    args.update(kw)
    return D.p_stop_fields(DAY, book(), EXP, SIM, 0.30, "09:20", "15:15", **args)


def test_both_columns_filled_and_sane():
    f = fields()
    assert 0 < f["p_stop_vix"] < 1 and 0 < f["p_stop_implied"] < 1
    # implied sigma is the straddle mid over sqrt(2/pi)
    assert f["sigma_implied_pts"] == pytest.approx(150.0 / T.sqrt(2 / 3.141592653589793), abs=0.1)
    # the site's formula on the forward
    assert f["sigma_vix_pts"] == pytest.approx(23380 * 0.1158 * (1 / 365) ** 0.5, abs=0.2)


def test_richer_sigma_means_more_stops():
    f = fields()
    # implied (~188) > VIX (~142) here, so its P(stop) must be higher
    assert f["sigma_implied_pts"] > f["sigma_vix_pts"]
    assert f["p_stop_implied"] > f["p_stop_vix"]


def test_no_vix_leaves_only_that_column_blank(monkeypatch):
    monkeypatch.setattr(D, "_vix_at", lambda d, hm: None)
    f = fields(vix=None)
    assert f["p_stop_vix"] == "" and f["p_stop_implied"] != ""


def test_no_measured_share_blanks_both(monkeypatch):
    monkeypatch.setattr(T, "load_session_share", lambda: None)
    f = fields(session_share=None)
    assert f["p_stop_vix"] == "" and f["p_stop_implied"] == ""


def test_no_stop_no_number():
    f = D.p_stop_fields(DAY, book(), EXP, SIM, None, "09:20", "15:15",
                        vix=11.58, session_share=0.66, is_trading_day=WEEKDAY)
    assert set(f.values()) == {""}


def test_wilson_interval_known_value():
    lo, hi = D.wilson(3, 14)
    assert lo == pytest.approx(0.0757, abs=1e-3) and hi == pytest.approx(0.4759, abs=1e-3)


def test_calibration_compares_like_with_like():
    rows = ([{"exit_reason": "stop", "p_stop_vix": "0.3", "p_stop_implied": "0.4"}] * 2
            + [{"exit_reason": "time", "p_stop_vix": "0.2", "p_stop_implied": ""}] * 2)
    c = D.stop_calibration(rows)
    assert c["p_stop_vix_n"] == 4 and c["p_stop_vix_mean"] == pytest.approx(0.25)
    # implied was only computed on the two stopped rows -- both sides use those two
    assert c["p_stop_implied_n"] == 2 and c["p_stop_implied_stops"] == 2
    assert c["stop_rate_ci"][0] < 0.5 < c["stop_rate_ci"][1]

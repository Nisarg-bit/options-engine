"""The clock every sigma on this site divides by, and which day it starts on.

WHY THIS FILE EXISTS

sigma is spot * (vol/100) * sqrt(dte/365). The credit it gets compared against
is taken from the closing chain of the most recent COLLECTED session. For a
long time dte was counted from today instead, which pairs one day's premium
with another day's clock. While the market is open those are the same date and
nothing shows. Every weekend, every holiday and every collector outage they
diverge, and sigma comes out wrong by sqrt(dte_today / dte_session) with
nothing on the page to say so.

It was found on Sunday 2026-09-20 looking at the Friday 2026-09-18 session:
the front NIFTY weekly displayed sigma 196.84 where the session had priced
278.37, and credit/sigma read 0.8934 against a 0.9346 gate when the honest
figure was 0.6317 -- a 41% inflation of the one number the whole thing gates
on, two decimal places away from opening a gate that should stay shut.

Every test here is written to FAIL against the old today-anchored code. The
weekend case is the whole point, so it is not enough to assert the arithmetic
in the abstract: `resolve` is driven against a session that is deliberately
older than today.

Run:  python -m pytest test_dte_anchor.py -q
"""

from datetime import date
from math import sqrt

import pandas as pd
import pytest

import api as A


SESSION = date(2026, 9, 18)      # a Friday, the last collected session
TODAY = date(2026, 9, 20)        # the Sunday it was being looked at
FRONT = date(2026, 9, 22)        # the weekly: 4 days of risk from SESSION
BACK = date(2026, 9, 29)
SETTLED = date(2026, 9, 19)      # expired between the session and today


@pytest.fixture
def frozen(monkeypatch):
    """A session two days behind today, with three expiries on the board."""
    bars = pd.DataFrame({
        "expiry": [SETTLED, FRONT, BACK],
        "instrument_token": [1, 2, 3],
    })
    # resolve() now goes through pick_session(), which asks sessions() what
    # was collected -- so the fixture has to describe that too. Patching only
    # latest_session left the real one returning [] and every call 404ing.
    monkeypatch.setattr(A, "sessions", lambda: [SESSION])
    monkeypatch.setattr(A, "latest_session", lambda: SESSION)
    monkeypatch.setattr(A, "last_bars", lambda day, und: bars)
    monkeypatch.setattr(A, "today_ist", lambda: TODAY)
    return bars


# --------------------------------------------------------------- the maths

def test_dte_is_measured_from_the_session_not_from_today():
    assert A.dte_for(SESSION, FRONT) == 4


def test_an_expiry_already_settled_carries_no_remaining_risk():
    # Clamped rather than negative: sqrt of a negative number is a crash, and
    # a crash is a worse answer than zero.
    assert A.dte_for(SESSION, date(2026, 9, 15)) == 0


def test_same_day_is_zero_not_one():
    assert A.dte_for(SESSION, SESSION) == 0


# ------------------------------------------------------------- the wiring

def test_resolve_anchors_dte_to_the_priced_session(frozen):
    day, exp, dte = A.resolve("NIFTY", str(FRONT))
    assert day == SESSION
    assert exp == FRONT
    # The old code returned 2 here -- (FRONT - TODAY).days.
    assert dte == 4


def test_resolve_still_refuses_to_pick_a_settled_expiry(frozen):
    # Selection is anchored to TODAY even though measurement is not: an
    # expiry that has already settled is not a contract anyone can trade,
    # however good its last prices looked.
    day, exp, dte = A.resolve("NIFTY", None)
    assert exp == FRONT
    assert dte == 4


def test_the_error_is_worst_at_the_shortest_horizon(frozen):
    """The offset is fixed in days and sits under a square root, so it bites
    hardest exactly where the weekly test failed."""
    near = A.dte_for(SESSION, FRONT)
    far = A.dte_for(SESSION, date(2026, 10, 27))
    near_err = sqrt(near / (near - 2)) - 1
    far_err = sqrt(far / (far - 2)) - 1
    assert near_err > 0.40
    assert far_err < 0.03


def test_sigma_reproduces_the_figure_the_session_actually_priced(frozen):
    """The numbers this was found on, end to end through the sigma formula."""
    spot, vix = 23346.4, 11.39
    sigma = lambda dte: spot * (vix / 100) * sqrt(max(dte, 1) / 365)
    _day, _exp, dte = A.resolve("NIFTY", str(FRONT))
    assert sigma(dte) == pytest.approx(278.37, abs=0.05)
    assert sigma(2) == pytest.approx(196.84, abs=0.05)
    # credit/sigma is the credit over that sigma, so the ratio of the two
    # sigmas is exactly the inflation the gate was reading.
    assert 0.8934 / (sigma(dte) / sigma(2)) == pytest.approx(0.6317, abs=0.001)


# ------------------------------------------------------- what the page reads

def test_expiries_reports_both_clocks(frozen):
    out = A.api_expiries(underlying="NIFTY")
    assert out["session"] == str(SESSION)
    assert out["today_ist"] == str(TODAY)
    by = {r["expiry"]: r for r in out["expiries"]}
    assert by[str(FRONT)]["dte"] == 4              # priced horizon
    assert by[str(FRONT)]["dte_from_today"] == 2   # still tradeable
    # The one that settled on Saturday: positive priced horizon, negative
    # from today. A dropdown filtering on `dte` alone would offer it.
    assert by[str(SETTLED)]["dte"] == 1
    assert by[str(SETTLED)]["dte_from_today"] < 0

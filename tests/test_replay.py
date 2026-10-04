"""Replaying a session must not borrow anything from today.

THE FAILURE THIS GUARDS AGAINST

market_ref() gets spot and India VIX from a live Kite quote. Point the chain
at an old session without changing that and you get September's option prices
divided by today's spot and today's volatility. Every POP, every sigma band
and every credit/sigma reading stays internally consistent and is completely
wrong, with nothing on the page to suggest it. That is worse than a crash: a
crash gets fixed the same afternoon.

So the central test here does not check that the replayed numbers look
plausible. It makes quotes() RAISE, and then asks for a replayed session. If
anything in that path still reaches for a live price, the test fails loudly
rather than quietly agreeing with itself.

Run:  python -m pytest test_replay.py -q
"""

from datetime import date

import pandas as pd
import pytest
from fastapi import HTTPException

import api as A
import journal as J


COLLECTED = [date(2026, 9, 14), date(2026, 9, 16), date(2026, 9, 18)]
OLD, NEWEST = COLLECTED[0], COLLECTED[-1]
TODAY = date(2026, 9, 20)

# Two instruments in one partition, which is how the collector stores them:
# the index near 23,300 and India VIX near 11. Telling them apart by
# magnitude is the whole trick.
INDEX_BARS = pd.DataFrame({
    "instrument_token": [256265, 256265, 264969, 264969],
    "ts": pd.to_datetime(["2026-09-14 09:15", "2026-09-14 15:29",
                          "2026-09-14 09:15", "2026-09-14 15:29"]),
    "close": [23290.0, 23312.5, 11.40, 11.22],
})

# The expiry that was live on 14 September and has settled since. A replay of
# that session has to offer it; a view of the newest session must not.
SETTLED = date(2026, 9, 15)
LIVE = date(2026, 9, 22)
BARS = pd.DataFrame({"expiry": [SETTLED, LIVE], "instrument_token": [1, 2]})


# A counter rather than an exception. market_ref deliberately TOLERATES a
# dead quote feed -- no Kite means fall back to the parity level -- so a
# quotes() that raises proves nothing: the broad except swallows it and the
# test passes for the wrong reason. Counting the calls asks the question that
# actually matters, which is whether the live feed was consulted at all.
CALLS = {"quotes": 0}


def _counting_quotes():
    CALLS["quotes"] += 1
    raise RuntimeError("no Kite in a test")


@pytest.fixture
def wired(monkeypatch):
    CALLS["quotes"] = 0
    monkeypatch.setattr(A, "sessions", lambda: list(COLLECTED))
    monkeypatch.setattr(A, "latest_session", lambda: NEWEST)
    monkeypatch.setattr(A, "today_ist", lambda: TODAY)
    monkeypatch.setattr(A, "last_bars", lambda day, und: BARS)
    monkeypatch.setattr(A, "read_parts", lambda day, und: INDEX_BARS)
    monkeypatch.setattr(J, "implied_level", lambda rows: 23300.0)
    monkeypatch.setattr(A, "quotes", _counting_quotes)


# A chain thin but real: priced() is not patched, so these carry the {CE, PE}
# shape the rest of the module expects, and atm_iv can actually solve on it.
CHAIN = [{"strike": k, "CE": {"mid": c}, "PE": {"mid": p}} for k, c, p in
         [(23200.0, 180.0, 82.0), (23300.0, 120.0, 121.0),
          (23400.0, 76.0, 176.0)]]


# ------------------------------------------------ nothing live, ever

def test_a_replayed_session_never_reaches_for_a_live_price(wired):
    spot, vix, src = A.market_ref("NIFTY", CHAIN, 4, OLD)
    assert spot == 23312.5          # that session's own close
    assert vix == 11.22             # that session's own VIX close
    assert "session" in src


def test_a_replayed_session_asks_the_live_feed_nothing(wired):
    A.market_ref("NIFTY", CHAIN, 4, OLD)
    assert CALLS["quotes"] == 0


def test_the_newest_session_still_uses_the_live_quote(wired):
    """The replay must not cost the running session its live price."""
    A.market_ref("NIFTY", CHAIN, 4, NEWEST)
    assert CALLS["quotes"] > 0


def test_no_day_at_all_behaves_as_before(wired):
    A.market_ref("NIFTY", CHAIN, 4)
    assert CALLS["quotes"] > 0


def test_the_index_is_picked_by_magnitude_not_by_token(wired):
    """No NIFTY_TOKEN lookup: it exists for one underlying and there is no
    BANKNIFTY equivalent anywhere in the project."""
    spot, vix, _ = A.session_ref(OLD, "NIFTY", CHAIN, 4)
    assert spot == 23312.5
    assert vix == 11.22


def test_a_series_nowhere_near_the_money_is_refused(wired, monkeypatch):
    """A partition holding only a volatility series must not yield a spot of
    11 for an index trading at 23,300."""
    monkeypatch.setattr(A, "read_parts", lambda day, und:
                        INDEX_BARS[INDEX_BARS["instrument_token"] == 264969])
    spot, vix, src = A.session_ref(OLD, "NIFTY", CHAIN, 4)
    assert spot == 23300.0          # fell back to the parity level
    assert src.startswith("parity")
    assert vix == 11.22
    assert CALLS["quotes"] == 0


def test_it_does_not_fall_back_to_the_latest_known_vix(wired, monkeypatch):
    """last_known_vix() reads the most recent journal row, which is today's.
    Handing that to a replayed session is the substitution being prevented.

    With no INDEX partition the volatility has to come from the replayed
    session's OWN chain or not at all -- both are honest, and today's number
    is not."""
    monkeypatch.setattr(A, "read_parts", lambda day, und: pd.DataFrame())
    monkeypatch.setattr(A, "last_known_vix",
                        lambda: pytest.fail("reached for today's VIX"))
    spot, vix, src = A.session_ref(OLD, "NIFTY", CHAIN, 4)
    assert spot == 23300.0
    assert src.startswith("parity")
    assert "chain-atm-iv" in src or "no-vol-for-that-session" in src
    assert CALLS["quotes"] == 0


# ---------------------------------------------------- picking a session

def test_no_session_named_means_the_newest(wired):
    assert A.pick_session(None) == NEWEST
    assert A.pick_session("") == NEWEST


def test_a_collected_session_is_served(wired):
    assert A.pick_session("2026-09-14") == OLD


def test_an_uncollected_date_is_a_404_not_a_quiet_fallback(wired):
    with pytest.raises(HTTPException) as e:
        A.pick_session("2026-09-15")
    assert e.value.status_code == 404


def test_something_that_is_not_a_date_is_a_400(wired):
    with pytest.raises(HTTPException) as e:
        A.pick_session("yesterday")
    assert e.value.status_code == 400


# ------------------------------------------- which expiries are offered

def test_replaying_offers_the_expiry_that_session_was_trading(wired):
    """15 September has settled by 20 September. Replaying the 14th has to
    show it -- it is the weekly that session was actually trading."""
    day, exp, dte = A.resolve("NIFTY", None, "2026-09-14")
    assert day == OLD
    assert exp == SETTLED
    assert dte == 1


def test_the_newest_session_does_not_offer_a_settled_expiry(wired):
    day, exp, dte = A.resolve("NIFTY", None)
    assert day == NEWEST
    assert exp == LIVE


def test_dte_is_measured_from_the_replayed_session(wired):
    _day, _exp, dte = A.resolve("NIFTY", "2026-09-22", "2026-09-14")
    assert dte == 8                  # not 2, which is 22 Sep minus today


# ------------------------------------------- the INDEX partition, now with four

# Adding SENSEX put a fourth series into the partition session_ref and
# session_moves pick from by magnitude: NIFTY near 23,300, BANKNIFTY near
# 56,000, SENSEX near 74,300, India VIX near 11. The picking rules were
# written against three. They still have to choose right, and "exactly one
# series in the volatility range" still has to be true.
FOUR = pd.DataFrame({
    "instrument_token": [256265, 256265, 260105, 260105,
                         265, 265, 264969, 264969],
    "ts": pd.to_datetime(["2026-09-14 09:15", "2026-09-14 15:29"] * 4),
    "close": [23290.0, 23312.5,     # NIFTY
              56300.0, 56358.7,     # BANKNIFTY
              74250.0, 74294.96,    # SENSEX
              11.40, 11.22],        # INDIA VIX
})


def test_nifty_still_finds_nifty_among_four(wired, monkeypatch):
    monkeypatch.setattr(A, "read_parts", lambda day, und: FOUR)
    spot, vix, src = A.session_ref(OLD, "NIFTY", CHAIN, 4)
    assert spot == 23312.5
    assert vix == 11.22
    assert CALLS["quotes"] == 0


def test_sensex_finds_sensex_and_not_banknifty(wired, monkeypatch):
    """The nearest neighbour by level. BANKNIFTY sits 24% below SENSEX --
    outside the 20% guard, so the guard is what keeps them apart."""
    monkeypatch.setattr(A, "read_parts", lambda day, und: FOUR)
    monkeypatch.setattr(J, "implied_level", lambda rows: 74300.0)
    spot, _vix, src = A.session_ref(OLD, "SENSEX", CHAIN, 4)
    assert spot == 74294.96
    assert src.startswith("session-close")


def test_a_non_nifty_underlying_never_borrows_india_vix(wired, monkeypatch):
    """India VIX is a NIFTY index. Handing it to SENSEX would be the same
    borrowed-calibration error the gate had."""
    monkeypatch.setattr(A, "read_parts", lambda day, und: FOUR)
    monkeypatch.setattr(J, "implied_level", lambda rows: 74300.0)
    _spot, vix, src = A.session_ref(OLD, "SENSEX", CHAIN, 4)
    assert "session-vix" not in src
    assert vix != 11.22

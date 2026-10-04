"""Picking the subscription universe, which is the step that can be wrong quietly.

BSE F&O carries four names -- SENSEX, BANKEX, FOCIT and SENSEX50 -- and only
the `name` field separates them. A filter that catches SENSEX50 instead of
SENSEX yields a complete, plausible, entirely wrong chain: right number of
strikes, right shape, wrong index, and nothing downstream can tell. Spot 74,295
against strikes clustered at 26,000 would eventually show, but only after a
session had been collected against it.

These run without a Kite session: `choose` is pure and takes the instrument
list as an argument.

Run:  python -m pytest test_instruments.py -q
"""

from datetime import date, datetime

import pytest

import instruments as I


TODAY = date(2026, 9, 21)

# Shaped as the BFO master actually is -- the probe on 2026-09-21 confirmed
# the keys match NFO's exactly, and that SENSEX weeklies are Thursdays with a
# 100-point strike step.
def opt(name, expiry, strike, kind="CE", lot=20):
    return {"name": name, "expiry": expiry, "strike": float(strike),
            "instrument_type": kind, "lot_size": lot,
            "instrument_token": hash((name, str(expiry), strike, kind)) % 10**8,
            "tradingsymbol": f"{name}{strike:.0f}{kind}"}


def chain(name, expiry, lot=20, n=4):
    return [opt(name, expiry, 74000 + 100 * i, k, lot)
            for i in range(n) for k in ("CE", "PE")]


MASTER = (
    chain("SENSEX", date(2026, 9, 24))
    + chain("SENSEX", date(2026, 10, 1))
    + chain("SENSEX", date(2026, 10, 8))
    + chain("SENSEX50", date(2026, 9, 24), lot=75)
    + chain("BANKEX", date(2026, 9, 24), lot=30)
    + chain("FOCIT", date(2026, 9, 24), lot=100)
    # already settled, must never be subscribed
    + chain("SENSEX", date(2026, 9, 17))
    # a future, not an option -- the instrument_type filter has to drop it
    + [{"name": "SENSEX", "expiry": date(2026, 9, 24), "strike": 0.0,
        "instrument_type": "FUT", "lot_size": 20,
        "instrument_token": 1, "tradingsymbol": "SENSEXFUT"}]
)

WANT = {"SENSEX": {"segment": "BFO", "expiries": 2}}


def test_it_takes_the_named_index_and_not_its_neighbours():
    chosen, expiries = I.choose(MASTER, WANT, TODAY)
    assert {c["name"] for c in chosen} == {"SENSEX"}
    assert expiries["SENSEX"] == ["2026-09-24", "2026-10-01"]


def test_sensex50_is_not_sensex():
    """The specific confusion this filter exists to prevent."""
    chosen, _ = I.choose(MASTER, {"SENSEX50": {"expiries": 1}}, TODAY)
    assert {c["name"] for c in chosen} == {"SENSEX50"}
    assert {c["lot_size"] for c in chosen} == {75}


def test_a_settled_expiry_is_never_subscribed():
    _chosen, expiries = I.choose(MASTER, WANT, TODAY)
    assert "2026-09-17" not in expiries["SENSEX"]


def test_futures_are_dropped():
    chosen, _ = I.choose(MASTER, WANT, TODAY)
    assert all(c["instrument_type"] in ("CE", "PE") for c in chosen)


def test_it_stops_at_the_configured_number_of_expiries():
    _c, e = I.choose(MASTER, {"SENSEX": {"expiries": 1}}, TODAY)
    assert e["SENSEX"] == ["2026-09-24"]
    _c, e = I.choose(MASTER, {"SENSEX": {"expiries": 9}}, TODAY)
    assert e["SENSEX"] == ["2026-09-24", "2026-10-01", "2026-10-08"]


def test_a_name_that_matches_nothing_reports_an_empty_list():
    """Not an exception and not a quiet skip: build() checks for exactly this
    and refuses to write a master missing an index."""
    _c, e = I.choose(MASTER, {"NOTHING": {"expiries": 2}}, TODAY)
    assert e == {"NOTHING": []}


def test_an_expiry_that_arrives_as_a_datetime_is_still_a_date():
    """kite.instruments() has returned both over time."""
    rows = [opt("SENSEX", datetime(2026, 9, 24, 15, 30), 74000)]
    _c, e = I.choose(rows, WANT, TODAY)
    assert e["SENSEX"] == ["2026-09-24"]


def test_expiring_today_still_counts_as_live():
    rows = chain("SENSEX", TODAY)
    _c, e = I.choose(rows, WANT, TODAY)
    assert e["SENSEX"] == [str(TODAY)]


# ----------------------------------------------------------------- the clock

def test_it_keeps_exchange_time_not_the_box_clock():
    """The server runs UTC and the exchange does not. Between 18:30 and
    midnight UTC a naive date.today() is already YESTERDAY in Delhi, and
    run_collector.sh re-runs this file on every systemd restart -- at
    whatever hour that happens to be.

    The filename is what breaks: api.lot_for() looks for
    data/instruments/<IST date>.json. During market hours the two dates
    always agree, which is why it would never have shown up in a session."""
    import api
    assert I.today_ist() == api.today_ist()


def test_the_two_clocks_disagree_where_it_matters():
    """Not a tautology: prove the naive call and the IST one actually differ
    inside the window, so the test above is testing something."""
    from datetime import datetime, timezone
    late = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)   # 03:30 IST 21st
    assert late.date() == date(2026, 9, 20)
    assert late.astimezone(I.IST).date() == date(2026, 9, 21)


# ------------------------------------------------------- the universe itself

def test_every_underlying_names_a_segment_and_a_count():
    for name, cfg in I.UNIVERSE.items():
        assert cfg["segment"] in ("NFO", "BFO"), name
        assert cfg["expiries"] >= 1, name


def test_the_index_series_covers_every_underlying_subscribed():
    """session_ref replays a past session's spot from the INDEX partition. An
    underlying whose index is not collected can be charted but never replayed
    honestly, which is a worse state than not having it at all."""
    import api
    for name in I.UNIVERSE:
        sym = api.INDEX_SYMBOL.get(name)
        assert sym, f"{name} has no INDEX_SYMBOL"
        assert sym in I.INDEX_SYMBOLS, f"{sym} is not being collected"


def test_the_bse_names_are_priced_on_the_bse_tariff():
    import api
    for name, cfg in I.UNIVERSE.items():
        want = api.BSE_OPTION_EXCHANGE if cfg["segment"] == "BFO" \
            else api.NSE_OPTION_EXCHANGE
        assert api.costs_for(name).exchange_per_side == want, name

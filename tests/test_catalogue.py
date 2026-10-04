"""catalogue.py -- definitions, scoring, and the settle-time roll in api.py.

Run:  python -m pytest test_catalogue.py -q
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import catalogue as C
import pricing as P

IST = timezone(timedelta(hours=5, minutes=30))
NEAR, NEXT = date(2026, 9, 29), date(2026, 10, 6)
SPOT, STEP, LOT, W = 23400.0, 50.0, 65, 100.0
SIG_N = 355.0
SIG_X = SIG_N * (14 / 7) ** 0.5


def bach(k, kind, sig):
    return P.expected_call(SPOT, k, sig) if kind == "CE" else P.expected_put(SPOT, k, sig)


def fair_chain():
    """Every strike priced at exactly the model's own value, so the model's
    EV before costs must be zero for every entry -- the sharpest test of the
    sign conventions there is."""
    out = {NEAR: {}, NEXT: {}}
    for i in range(-20, 21):
        k = SPOT + i * STEP
        for kind in ("CE", "PE"):
            out[NEAR][(k, kind)] = bach(k, kind, SIG_N)
            out[NEXT][(k, kind)] = bach(k, kind, SIG_X)
    return out


def rows(chains=None, exps=(NEAR, NEXT)):
    return C.score_all(chains or fair_chain(), list(exps), SPOT, SPOT, STEP, LOT,
                       {NEAR: SIG_N, NEXT: SIG_X}, 120000.0)


def by(key, rs=None):
    return next(r for r in (rs or rows()) if r["key"] == key)


# ------------------------------------------------------------ definitions

def test_every_group_has_entries_and_the_four_requested_families_exist():
    groups = {s["group"] for s in C.STRATEGIES}
    assert groups == {"bullish", "bearish", "neutral"}
    fam = {s.get("family") for s in C.STRATEGIES}
    assert {"credit spread", "debit spread", "ratio spread",
            "calendar spread"} <= fam


def test_keys_are_unique_and_every_entry_has_a_blurb():
    keys = [s["key"] for s in C.STRATEGIES]
    assert len(keys) == len(set(keys))
    assert all(C.BLURB.get(k) for k in keys)


def test_definitions_are_json_for_the_page():
    import json
    d = json.loads(json.dumps(C.definitions()))
    assert len(d["strategies"]) == len(C.STRATEGIES)
    assert d["strategies"][0]["legs"][0].keys() == {"kind", "off", "qty", "exp"}


@pytest.mark.parametrize("key,expect", [
    ("bull_call_spread", [(23400, "CE", 1), (23500, "CE", -1)]),
    ("bull_put_spread", [(23400, "PE", -1), (23300, "PE", 1)]),
    ("call_ratio_back_spread", [(23400, "CE", -1), (23500, "CE", 2)]),
    ("put_ratio_spread", [(23400, "PE", 1), (23300, "PE", -2)]),
])
def test_strikes_follow_atm_plus_width(key, expect):
    legs = C.build_legs(key, SPOT, W, [NEAR, NEXT])
    assert [(l["strike"], l["kind"], l["qty"]) for l in legs] == expect


def test_calendar_puts_its_long_leg_on_the_next_expiry():
    legs = C.build_legs("long_calendar_calls", SPOT, W, [NEAR, NEXT])
    assert [(l["qty"], l["expiry"]) for l in legs] == [(-1, NEAR), (1, NEXT)]


# ---------------------------------------------------------------- scoring

def test_fair_prices_give_zero_model_ev_for_every_entry():
    for r in rows():
        assert r["available"], r
        assert abs(r["gross_ev_rs"]) <= 1, (r["key"], r["gross_ev_rs"])


def test_credit_and_debit_signs():
    assert by("bull_put_spread")["net_premium_pts"] > 0      # credit
    assert by("bull_call_spread")["net_premium_pts"] < 0     # debit
    assert by("long_calendar_puts")["net_premium_pts"] < 0   # far leg dearer


def test_defined_risk_spreads_have_exact_max_loss():
    r = by("bull_put_spread")
    credit = r["net_premium_pts"]
    assert r["max_loss_rs"] == pytest.approx(-(W - credit) * LOT, abs=1)
    assert r["max_profit_rs"] == pytest.approx(credit * LOT, abs=1)
    assert not r["loss_unbounded"]


def test_unbounded_shapes_are_flagged():
    assert by("sell_put")["loss_unbounded"]
    assert by("call_ratio_spread")["loss_unbounded"]        # 1:2 front ratio
    assert by("buy_call")["profit_unbounded"]
    assert by("call_ratio_back_spread")["profit_unbounded"]
    assert not by("call_ratio_back_spread")["loss_unbounded"]
    assert not by("iron_condor")["loss_unbounded"]


def test_straddle_pop_matches_the_breakeven_formula():
    r = by("short_straddle")
    lo, hi = min(r["breakevens"]), max(r["breakevens"])
    assert r["pop"] == pytest.approx(
        P.probability_of_profit(SPOT, lo, hi, SIG_N), abs=2e-3)


def test_calendar_is_bounded_and_profits_near_the_strike():
    r = by("long_calendar_calls")
    assert not r["loss_unbounded"]
    assert r["max_loss_rs"] == pytest.approx(r["net_premium_rs"], abs=2)
    assert len(r["breakevens"]) == 2
    assert min(r["breakevens"]) < SPOT < max(r["breakevens"])


def test_missing_strike_is_unavailable_not_guessed():
    ch = fair_chain()
    del ch[NEAR][(SPOT + 3 * W, "CE")]                        # condor's top wing
    r = by("bull_condor", rows(ch))
    assert r["available"] is False and "no two-sided quote" in r["why"]


def test_calendars_unavailable_without_a_next_expiry():
    r = by("long_calendar_puts", rows(exps=(NEAR,)))
    assert r["available"] is False and "next expiry" in r["why"]


def test_nothing_in_the_catalogue_is_ever_recommended():
    assert not any(r["recommended"] for r in rows())


def test_margin_estimate_rules():
    assert by("bull_call_spread")["margin_est"] == pytest.approx(
        -by("bull_call_spread")["net_premium_rs"], abs=1)     # debit paid
    assert by("short_strangle")["margin_est"] == 240000       # two naked shorts
    assert by("jade_lizard")["margin_est"] == 120000          # naked put only


def test_stop_eligibility():
    assert C.stop_eligible("bull_put_spread")
    assert C.stop_eligible("iron_condor")
    assert not C.stop_eligible("bull_call_spread")            # debit
    assert not C.stop_eligible("call_ratio_spread")           # ratio
    assert not C.stop_eligible("long_calendar_calls")         # two expiries


# ---------------------------------------------------- settle-time roll

def test_expiry_settles_at_1530_ist(monkeypatch):
    import api as A
    d = date(2026, 9, 22)
    at = lambda hm: datetime(2026, 9, 22, *hm, tzinfo=IST)
    assert not A.is_settled(d, at((15, 29)))
    assert A.is_settled(d, at((15, 30)))
    assert A.is_settled(date(2026, 9, 15), at((9, 0)))
    assert not A.is_settled(date(2026, 9, 29), at((23, 0)))


def test_live_expiries_rolls_after_the_close(monkeypatch):
    import api as A
    d = date(2026, 9, 22)
    monkeypatch.setattr(A, "latest_session", lambda: d)
    monkeypatch.setattr(A, "today_ist", lambda: d)
    exps = [d, NEAR, NEXT]
    before = datetime(2026, 9, 22, 15, 0, tzinfo=IST)
    after = datetime(2026, 9, 22, 16, 0, tzinfo=IST)
    assert A.live_expiries(d, exps, before)[0] == d
    assert A.live_expiries(d, exps, after)[0] == NEAR
    # a replay is anchored to its own date, not to the clock
    old = date(2026, 9, 21)
    assert A.live_expiries(old, exps, after)[0] == d


def test_scoring_is_centred_on_the_forward_not_the_spot():
    """Premiums priced around a forward 70 points above spot: centred on that
    forward, model EV is zero for directional spreads too."""
    fwd = SPOT + 70
    ch = {NEAR: {}}
    for i in range(-20, 21):
        k = SPOT + i * STEP
        for kind in ("CE", "PE"):
            ch[NEAR][(k, kind)] = (P.expected_call(fwd, k, SIG_N) if kind == "CE"
                                   else P.expected_put(fwd, k, SIG_N))
    rs = C.score_all(ch, [NEAR], fwd, SPOT, STEP, LOT, {NEAR: SIG_N}, 120000.0,
                     fwd_by_exp={NEAR: fwd})
    for key in ("bull_call_spread", "bear_put_spread", "buy_put", "sell_call"):
        assert abs(by(key, rs)["gross_ev_rs"]) <= 1

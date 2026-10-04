"""Shadow books: gate, naked->defined-risk swap, sizing, caps, F1, settlement, breaker."""
import math
from datetime import date
import pandas as pd
import pytest
import pricing as P
import strategies as S
import shadow as SH

MON, TUE = date(2026, 9, 28), date(2026, 9, 29)


def chain(fwd=25000.0, rich=1.0):
    rows = []
    for i in range(-30, 31):
        k = fwd + 50 * i
        tv = rich * 170 * math.exp(-(abs(k - fwd) / 260) ** 1.3)
        rows.append({"strike": k, "ce": max(fwd - k, 0) + tv, "pe": max(k - fwd, 0) + tv})
    return rows


def structs(rich=1.0):
    return S.build_all(chain(rich=rich), 25000.0, 0.12, 2, step=50, lot_size=65, lots=1,
                       wing_width=200.0, sigma_mult=1.5, margin_per_lot=120000.0, costs=P.CURRENT_COSTS)


@pytest.fixture(autouse=True)
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SH, "DIR", str(tmp_path))
    for n, f in (("BOOK", "book.csv"), ("DECISIONS", "decisions.csv"), ("STATE", "state.json"), ("SUMMARY", "summary.json")):
        monkeypatch.setattr(SH, n, str(tmp_path / f))
    monkeypatch.setattr(SH, "daytrade_candidate", lambda: {})


def ctx(cs):
    return {"today": MON, "expiry": TUE, "c_over_sigma": cs}


def test_gate_decides_per_book():
    st = structs(rich=1.3)
    d = {x["book"]: x for x in SH.record(ctx(1.0), st)}
    assert d["live"]["action"] == "trade" and d["F1"]["action"] == "trade"
    assert d["F4"]["action"] == "skip" and "gate" in d["F4"]["reason"]


def test_naked_top_pick_is_swapped_to_defined_risk_and_sized_to_the_cap():
    st = structs(rich=1.3)
    s, top, swapped, why = SH.choose(st, 1.2, SH.LIVE_GATE)
    assert s.defined_risk and (swapped or top == s.name)
    lots, worst, cost = SH.size(s)
    assert lots >= 1 and lots * worst <= SH.CAP_TRADE
    assert worst == pytest.approx(s.max_loss_pts * 65 + cost)


def test_missing_wing_is_not_usable():
    s = structs(rich=1.3)["iron_condor"]
    s.long_pe_premium = 0.0                          # strategies.Chain's price for a missing strike
    assert not SH.usable(s)


def test_one_per_expiry_and_book_cap():
    st = structs(rich=1.3)
    SH.record(ctx(1.2), st)
    d = {x["book"]: x for x in SH.record(ctx(1.2), st, today=MON)}
    assert "already held" in d["F1"]["reason"]
    b = SH.load_book()
    held = (b[b.book == "live"].worst_rs * b[b.book == "live"].lots).sum()
    assert held <= SH.CAP_BOOK


def test_settlement_charges_half_costs_and_intrinsic():
    r = {"ce_strike": 25200, "pe_strike": 24800, "long_ce_strike": 25400, "long_pe_strike": 24600,
         "credit_pts": 60.0, "lot": 65, "lots": 2, "cost_rt_rs": 400.0}
    assert SH.settle_row(r, 25000) == pytest.approx(60 * 130 - 400)          # expires worthless
    assert SH.settle_row(r, 25500) == pytest.approx((60 - 200) * 130 - 400)  # full wing lost


def test_settle_and_breaker_pause():
    st = structs(rich=1.3)
    SH.record(ctx(1.2), st)
    b = SH.load_book(); b["lots"] = 50; SH.save_book(b)          # force a large loss
    SH.settle(today=date(2026, 9, 30), level_fn=lambda e: 27000.0)
    s = SH.load_state()
    assert s["live"]["paused_until"] is not None
    d = {x["book"]: x for x in SH.record(ctx(1.2), st, today=date(2026, 10, 1))}
    assert "circuit breaker" in d["live"]["reason"]


def test_nothing_before_start():
    assert SH.record({**ctx(1.2), "today": date(2026, 9, 25)}, structs()) == []

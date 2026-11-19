"""tracking/paper_book.py: entries, marks, exits, the two books, the summary."""
from datetime import datetime, date

import paper_book as PB

IST = PB.IST
TRADING = lambda d: d.weekday() < 5


def card(key="bull_call_spread", lots=5, mp=4000, ml=-2000):
    return {"key": key, "label": key.replace("_", " ").title(), "max_profit_rs": mp, "max_loss_rs": ml,
            "costs_rs": 100, "sizing": {"lots": lots},
            "legs": [{"strike": 25000.0, "kind": "CE", "qty": 1, "premium": 100.0, "expiry": "2026-11-17"},
                     {"strike": 25100.0, "kind": "CE", "qty": -1, "premium": 60.0, "expiry": "2026-11-17"}]}


def views(call="up", cards=None, lean_cards=None):
    iv = {"available": True, "call": call, "lean": "up", "odds": {"up": .6, "down": .2, "sideways": .2},
          "vol": {"state": "normal"}, "lot": 65,
          "strategies": cards if cards is not None else [card()], "lean_strategies": lean_cards or []}
    return {"indices": {"NIFTY": iv, "BANKNIFTY": {"available": False, "reason": "chain too thin"}}}


class Px:
    def __init__(self, a=100.0, b=60.0):
        self.p = {25000.0: a, 25100.0: b}
    def __call__(self, und, exp, strike, kind):
        return self.p.get(float(strike))


def at(d, hh, mm):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST)


def test_entry_at_1015_in_both_books_and_skip_logged():
    st = PB.empty_state()
    assert PB.step(st, at(11, 10, 0), views(), Px(), TRADING) == []           # too early
    ev = PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    assert len(st["trades"]) == 2 and {t["book"] for t in st["trades"]} == {"intraday", "hold"}
    t = st["trades"][0]
    assert t["lots"] == 5 and t["target_rs"] == 10000 and t["stop_rs"] == -5000 and t["costs_rs"] == 500
    assert st["skips"][0]["underlying"] == "BANKNIFTY" and not st["skips"][0]["final"]
    PB.step(st, at(11, 10, 16), views(), Px(), TRADING)
    assert len(st["trades"]) == 2                                            # once a day
    assert any("opened" in e for e in ev)


def test_weak_lead_trades_the_leading_side():
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views("unclear", cards=[], lean_cards=[card("bull_condor")]), Px(), TRADING)
    assert st["trades"][0]["key"] == "bull_condor" and st["trades"][0]["weak"]


def test_unsizable_cards_are_skipped():
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(cards=[card(lots=0), card("bull_put_spread", lots=2)]), Px(), TRADING)
    assert st["trades"][0]["key"] == "bull_put_spread"


def test_target_stop_and_time_exits():
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    # spread 40 -> 72: +32 pts x 65 x 5 = 10,400 >= target 10,000
    PB.step(st, at(11, 11, 0), views(), Px(150.0, 78.0), TRADING)
    assert all(t["status"] == "closed" and t["exit_reason"] == "target" for t in st["trades"])
    assert st["trades"][0]["pnl_rs"] == 10400 - 500
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    PB.step(st, at(11, 12, 0), views(), Px(80.0, 56.0), TRADING)            # -16 x 325 = -5,200
    assert all(t["exit_reason"] == "stop" for t in st["trades"])
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    PB.step(st, at(11, 15, 15), views(), Px(105.0, 62.0), TRADING)
    intra = [t for t in st["trades"] if t["book"] == "intraday"][0]
    hold = [t for t in st["trades"] if t["book"] == "hold"][0]
    assert intra["status"] == "closed" and intra["exit_reason"].startswith("time")
    assert hold["status"] == "open" and hold["mark"]["pnl_rs"] == 3 * 325 - 500
    # next day: hold blocks a new hold entry, intraday enters again
    ev = PB.step(st, at(12, 10, 15), views(), Px(105.0, 62.0), TRADING)
    assert sum(t["book"] == "hold" for t in st["trades"]) == 1
    assert sum(t["book"] == "intraday" for t in st["trades"]) == 2
    assert any("still open" in e for e in ev)
    PB.step(st, at(17, 15, 15), views(), Px(130.0, 70.0), TRADING)           # expiry day
    assert hold["status"] == "closed" and hold["exit_reason"] == "expiry"


def test_missing_prices_skip_the_mark_and_holidays_do_nothing():
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    PB.step(st, at(11, 11, 0), views(), lambda *a: None, TRADING)
    assert all(t["status"] == "open" for t in st["trades"])
    assert PB.step(st, at(14, 11, 0), views(), Px(), TRADING) == []          # Saturday


def test_summary(tmp_path):
    st = PB.empty_state()
    PB.step(st, at(11, 10, 15), views(), Px(), TRADING)
    PB.step(st, at(11, 11, 0), views(), Px(150.0, 78.0), TRADING)
    PB.save(st, str(tmp_path / "b.json"))
    s = PB.summary(PB.load(str(tmp_path / "b.json")), now=at(11, 11, 1))
    b = s["books"]["intraday"]
    assert b["trades"] == 1 and b["wins"] == 1 and b["equity"] == 2_000_000 + 9900
    assert b["curve"] == [["2026-11-11", 2_009_900]] and b["by_strategy"][0]["trades"] == 1
    assert s["skips_today"][0]["underlying"] == "BANKNIFTY"

"""Tests for position.py.

Every case here is a hand-built price stream, because the point of keeping
the engine pure is that its behaviour can be stated exactly rather than
inferred from a parquet file.

Run:  python -m pytest test_position.py -q
      python test_position.py        (prints the worked examples)
"""

from datetime import date, datetime, time as dtime

import pytest

import position as X
from position import CE, PE, ExitRule, Leg, Position


EXPIRY = date(2026, 9, 15)


def at(h, m, day=15):
    return datetime(2026, 9, day, h, m)


def stream(rows):
    """rows: (ts, {key: px}, dte)"""
    return iter(rows)


# ------------------------------------------------------------------ the sums

def test_credit_of_a_short_straddle():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY)
    assert p.credit_pts == 220.0
    assert p.defined_risk is False


def test_credit_of_an_iron_condor_is_net():
    p = X.iron_condor(23500, 23300, 23700, 23100,
                      80.0, 70.0, 20.0, 18.0,
                      lot_size=75, entry_ts=at(9, 20), expiry=EXPIRY)
    assert p.credit_pts == pytest.approx(80 + 70 - 20 - 18)
    assert p.defined_risk is True


def test_value_equals_credit_when_prices_are_unchanged():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY)
    same = {(23400.0, CE): 120.0, (23400.0, PE): 100.0}
    assert p.value_pts(same) == p.credit_pts
    assert p.captured(p.value_pts(same)) == 0.0


def test_missing_leg_raises_rather_than_assuming_zero():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY)
    with pytest.raises(KeyError):
        p.value_pts({(23400.0, CE): 100.0})


def test_bad_leg_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Leg(23400, "CALL", -1, 10.0)
    with pytest.raises(ValueError):
        Leg(23400, CE, -2, 10.0)


# ------------------------------------------------------------------- decay

def test_decay_fires_at_the_threshold_not_before():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    # 220 credit; 85% captured means value <= 33.0
    rows = [
        (at(10, 0), {(23400.0, CE): 70.0, (23400.0, PE): 60.0}, 4),   # 40.9%
        (at(11, 0), {(23400.0, CE): 20.0, (23400.0, PE): 14.0}, 4),   # 84.5%
        (at(12, 0), {(23400.0, CE): 18.0, (23400.0, PE): 14.0}, 4),   # 85.5%
    ]
    e = p.run(stream(rows))
    assert e is not None
    assert e.reason == "decay"
    assert e.ts == at(12, 0)
    assert e.value_pts == pytest.approx(32.0)
    assert e.pnl_pts == pytest.approx(188.0)
    assert e.captured >= 0.85
    assert e.bars == 3


def test_a_looser_threshold_exits_earlier():
    rows = [
        (at(11, 0), {(23400.0, CE): 20.0, (23400.0, PE): 14.0}, 4),   # 84.5%
    ]
    loose = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                             entry_ts=at(9, 20), expiry=EXPIRY,
                             rule=ExitRule(capture=0.80, hard_exit_dte=0))
    tight = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                             entry_ts=at(9, 20), expiry=EXPIRY,
                             rule=ExitRule(capture=0.90, hard_exit_dte=0))
    assert loose.run(stream(list(rows))).reason == "decay"
    assert tight.run(stream(list(rows))) is None


# -------------------------------------------------------------------- time

def test_hard_time_exit_fires_at_the_dte_floor():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20, day=10), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=2))
    rows = [
        (at(15, 20, day=10), {(23400.0, CE): 90.0, (23400.0, PE): 80.0}, 5),
        (at(15, 20, day=11), {(23400.0, CE): 80.0, (23400.0, PE): 70.0}, 4),
        (at(15, 20, day=13), {(23400.0, CE): 70.0, (23400.0, PE): 60.0}, 2),
    ]
    e = p.run(stream(rows))
    assert e.reason == "time"
    assert e.pnl_pts == pytest.approx(220.0 - 130.0)


def test_expiry_day_is_not_expired():
    """dte == 0 means today IS expiry day, and it still trades until 15:30.

    Written as `dte <= 0` this closed every intraday position on its first
    bar at zero P&L -- a broken loop that reads as a flat strategy.
    """
    p = X.short_straddle(23400, 60.0, 55.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=-1,
                                       flat_by=dtime(15, 15)))
    rows = [
        (at(10, 0), {(23400.0, CE): 50.0, (23400.0, PE): 48.0}, 0),
        (at(15, 15), {(23400.0, CE): 30.0, (23400.0, PE): 25.0}, 0),
    ]
    e = p.run(stream(rows))
    assert e.reason == "eod"
    assert e.pnl_pts == pytest.approx(115.0 - 55.0)


def test_settled_once_the_date_has_passed():
    p = X.short_straddle(23400, 60.0, 55.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.99, hard_exit_dte=-5))
    rows = [(at(10, 0), {(23400.0, CE): 1.0, (23400.0, PE): 1.0}, -1)]
    assert p.run(stream(rows)).reason == "expiry"


def test_decay_beats_time_when_both_would_fire():
    """Order matters: banking 85% is a better outcome than a forced exit."""
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=2))
    rows = [(at(15, 20), {(23400.0, CE): 18.0, (23400.0, PE): 14.0}, 2)]
    assert p.run(stream(rows)).reason == "decay"


# --------------------------------------------------------------------- eod

def test_intraday_goes_flat_at_the_bell():
    p = X.short_straddle(23400, 60.0, 55.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0,
                                       flat_by=dtime(15, 15)))
    rows = [
        (at(14, 0), {(23400.0, CE): 40.0, (23400.0, PE): 38.0}, 3),
        (at(15, 15), {(23400.0, CE): 30.0, (23400.0, PE): 28.0}, 3),
    ]
    e = p.run(stream(rows))
    assert e.reason == "eod"
    assert e.ts == at(15, 15)
    assert e.pnl_pts == pytest.approx(115.0 - 58.0)


def test_intraday_still_takes_a_decay_exit_before_the_bell():
    p = X.short_straddle(23400, 60.0, 55.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0,
                                       flat_by=dtime(15, 15)))
    rows = [(at(13, 0), {(23400.0, CE): 9.0, (23400.0, PE): 8.0}, 3)]
    assert p.run(stream(rows)).reason == "decay"


# ------------------------------------------------------- adverse excursion

def test_mae_records_the_worst_point_not_the_last():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    rows = [
        (at(10, 0), {(23400.0, CE): 200.0, (23400.0, PE): 90.0}, 4),   # -70
        (at(11, 0), {(23400.0, CE): 320.0, (23400.0, PE): 60.0}, 4),   # -160
        (at(12, 0), {(23400.0, CE): 150.0, (23400.0, PE): 70.0}, 4),   # 0
        (at(13, 0), {(23400.0, CE): 18.0, (23400.0, PE): 14.0}, 4),    # exit
    ]
    e = p.run(stream(rows))
    assert e.reason == "decay"
    assert e.pnl_pts == pytest.approx(188.0)
    assert e.mae_pts == pytest.approx(-160.0)
    assert p.mfe_pts == pytest.approx(188.0)


def test_a_winner_can_have_a_brutal_mae():
    """The whole reason no-touch POP and terminal POP are both reported."""
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    rows = [
        (at(10, 0), {(23400.0, CE): 500.0, (23400.0, PE): 20.0}, 4),
        (at(15, 0), {(23400.0, CE): 20.0, (23400.0, PE): 12.0}, 4),
    ]
    e = p.run(stream(rows))
    assert e.pnl_pts > 0
    assert e.mae_pts < -290


# ------------------------------------------------------------ open positions

def test_running_out_of_data_is_not_an_exit():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    rows = [(at(10, 0), {(23400.0, CE): 110.0, (23400.0, PE): 95.0}, 4)]
    assert p.run(stream(rows)) is None
    assert p.is_open is True
    assert p.bars == 1


def test_stepping_after_an_exit_returns_the_same_exit():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    first = p.step(at(12, 0), {(23400.0, CE): 18.0, (23400.0, PE): 14.0}, 4)
    again = p.step(at(13, 0), {(23400.0, CE): 5.0, (23400.0, PE): 4.0}, 4)
    assert first is again
    assert p.bars == 1


# -------------------------------------------------------------------- money

def test_rupees_net_of_costs_are_below_gross():
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=0))
    e = p.run(stream([(at(12, 0), {(23400.0, CE): 18.0,
                                   (23400.0, PE): 14.0}, 4)]))
    gross = e.pnl_pts * 75
    net = e.pnl_rs(lot_size=75, lots=1, defined_risk=False,
                   credit_pts=p.credit_pts)
    assert net < gross
    assert gross - net > 0


def test_a_condor_pays_more_in_costs_than_a_straddle():
    """Four legs, double the brokerage -- the thing that will decide whether
    defined-risk structures survive intraday at all."""
    sd = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                          entry_ts=at(9, 20), expiry=EXPIRY)
    ic = X.iron_condor(23500, 23300, 23700, 23100, 80.0, 70.0, 20.0, 18.0,
                       lot_size=75, entry_ts=at(9, 20), expiry=EXPIRY)
    rows_sd = [(at(12, 0), {(23400.0, CE): 18.0, (23400.0, PE): 14.0}, 4)]
    rows_ic = [(at(12, 0), {(23500.0, CE): 10.0, (23300.0, PE): 9.0,
                            (23700.0, CE): 3.0, (23100.0, PE): 2.0}, 4)]
    e_sd = sd.run(stream(rows_sd))
    e_ic = ic.run(stream(rows_ic))
    fee_sd = e_sd.pnl_pts * 75 - e_sd.pnl_rs(75, credit_pts=sd.credit_pts)
    fee_ic = e_ic.pnl_pts * 75 - e_ic.pnl_rs(
        75, defined_risk=True, credit_pts=ic.credit_pts)
    assert fee_ic > fee_sd


# ----------------------------------------------------------------- reporting

def main():
    print("A SHORT STRADDLE THROUGH ONE SESSION\n")
    p = X.short_straddle(23400, 120.0, 100.0, lot_size=75,
                         entry_ts=at(9, 20), expiry=EXPIRY,
                         rule=ExitRule(capture=0.85, hard_exit_dte=2,
                                       flat_by=dtime(15, 15)))
    print(f"credit {p.credit_pts:.1f} pts   exit at "
          f"{p.rule.capture:.0%} captured = value <= "
          f"{p.credit_pts * (1 - p.rule.capture):.1f} pts\n")
    path = [
        (at(9, 30), 130.0, 105.0, 4),
        (at(10, 30), 210.0, 70.0, 4),
        (at(11, 30), 300.0, 45.0, 4),
        (at(12, 30), 180.0, 62.0, 4),
        (at(13, 30), 90.0, 70.0, 4),
        (at(14, 30), 40.0, 40.0, 4),
        (at(15, 0), 17.0, 14.0, 4),
    ]
    print(f"{'time':<8}{'CE':>8}{'PE':>8}{'value':>9}{'captured':>11}"
          f"{'P&L pts':>10}{'':>4}")
    print("-" * 52)
    for ts, ce, pe, dte in path:
        if not p.is_open:
            break
        prices = {(23400.0, CE): ce, (23400.0, PE): pe}
        v = p.value_pts(prices)
        e = p.step(ts, prices, dte)
        print(f"{ts.strftime('%H:%M'):<8}{ce:>8.1f}{pe:>8.1f}{v:>9.1f}"
              f"{p.captured(v):>10.1%}{p.credit_pts - v:>10.1f}"
              f"{'  <- ' + e.reason if e else ''}")

    e = p.exit
    print(f"\nexited {e.reason} at {e.ts.strftime('%H:%M')}")
    print(f"  banked        {e.captured:.1%} of the credit")
    print(f"  P&L           {e.pnl_pts:+.1f} pts = "
          f"Rs {e.pnl_rs(75, credit_pts=p.credit_pts):+,.0f} after costs")
    print(f"  worst point   {e.mae_pts:+.1f} pts")
    print(f"\nThat trade was {abs(e.mae_pts / p.credit_pts):.0%} of its credit")
    print("underwater at 11:30 and still finished a winner. Terminal POP")
    print("would score it a clean hit and say nothing about the hour in")
    print("between -- which is the hour that decides whether you could")
    print("actually have held it.")


if __name__ == "__main__":
    main()

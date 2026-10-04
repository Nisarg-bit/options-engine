"""Outcome store: exit rules, cost-to-close marking, coverage flags."""
from datetime import date, datetime, timedelta, timezone
import os
import pandas as pd
import pytest
import outcomes as O

UTC = timezone.utc
SIG, EXP = date(2026, 9, 28), date(2026, 9, 29)       # Mon signal, Tue expiry


def ts(d, hh, mm):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)


def q(ce, pe, spread=1.0):
    return {(25000.0, "CE"): (ce - spread / 2, ce + spread / 2),
            (25000.0, "PE"): (pe - spread / 2, pe + spread / 2)}


SHORTS = [(25000.0, "CE"), (25000.0, "PE")]


def test_cost_to_close_is_shorts_at_ask_minus_wings_at_bid():
    quotes = {**q(100, 100), (25200.0, "CE"): (10, 11), (24800.0, "PE"): (8, 9)}
    c = O.close_cost(quotes, SHORTS, [(25200.0, "CE"), (24800.0, "PE")])
    assert c == pytest.approx(100.5 + 100.5 - 10 - 8)


def test_missing_leg_skips_the_minute_not_guesses():
    path = O.mark_path([(ts(SIG, 4, 0), {(25000.0, "CE"): (99, 101)})], SHORTS, [])
    assert path == []


def test_stop_fires_on_first_touch_and_target_is_independent():
    credit = 200.0
    path = [(ts(SIG, 4, 0), 210), (ts(SIG, 5, 0), 265), (ts(SIG, 6, 0), 150),
            (ts(EXP, 4, 0), 35)]
    ev = O.evaluate(credit, path)
    assert ev["stop_30"][0] == ts(SIG, 5, 0)          # 265 >= 260
    assert ev["stop_50"] is None and ev["stop_100"] is None
    assert ev["target_50"][0] == ts(EXP, 4, 0)        # 35 <= 100 (first at/below)
    assert ev["target_80"][0] == ts(EXP, 4, 0)        # 35 <= 40


def test_time_exit_takes_last_mark_at_or_before_1515_ist():
    path = [(ts(SIG, 9, 40), 180), (ts(SIG, 9, 45), 170), (ts(SIG, 9, 50), 160)]
    ev = O.evaluate(200.0, path, time_exit_ts=ts(SIG, 9, 45))
    assert ev["time"] == (ts(SIG, 9, 45), 170)


def test_house_rule_takes_whichever_fires_first():
    path = [(ts(SIG, 4, 0), 150), (ts(SIG, 5, 0), 30), (ts(SIG, 6, 0), 500)]
    ev = O.evaluate(200.0, path, time_exit_ts=ts(SIG, 9, 45))
    assert ev["_house_reason"] == "target_80" and ev["house"] == (ts(SIG, 5, 0), 30)
    ev = O.evaluate(200.0, [(ts(SIG, 4, 0), 410)], time_exit_ts=None)
    assert ev["_house_reason"] == "stop_100"
    ev = O.evaluate(200.0, [(ts(EXP, 4, 0), 150)], time_exit_ts=None)
    assert ev["_house_reason"] == "held_to_expiry" and ev["house"] is None


# ------------------------------------------------ end to end on fake bars

def write_session(root, d, marks):
    """marks: [(hh, mm, ce_mid, pe_mid)] -> one parquet part per minute."""
    folder = os.path.join(root, f"date={d}", "underlying=NIFTY")
    os.makedirs(folder, exist_ok=True)
    for i, (hh, mm, ce, pe) in enumerate(marks):
        pd.DataFrame({"ts": [ts(d, hh, mm)] * 2, "expiry": [EXP, EXP],
                      "strike": [25000.0, 25000.0], "opt_type": ["CE", "PE"],
                      "bid": [ce - 0.5, pe - 0.5], "ask": [ce + 0.5, pe + 0.5]}
                     ).to_parquet(os.path.join(folder, f"part-{i:05d}.parquet"))


def journal_row(**kw):
    r = {"signal_date": str(SIG), "expiry": str(EXP), "structure": "short_straddle",
         "ce_strike": 25000.0, "pe_strike": 25000.0, "long_ce_strike": 0, "long_pe_strike": 0,
         "defined_risk": False, "credit_pts": 200.0, "lot": 65, "lots": 1,
         "fees_rs": 100.0, "slippage_rs": 260.0, "pnl_rs": 5000.0, "inside_zone": True,
         "final_level": 25100.0, "forward": 25000.0, "sigma": 200.0,
         "recommended": True, "gate_passed": True}
    r.update(kw)
    return r


def test_outcome_row_end_to_end(tmp_path):
    root = str(tmp_path)
    write_session(root, SIG, [(4, 0, 101, 101), (5, 0, 140, 140), (9, 45, 60, 60)])
    write_session(root, EXP, [(4, 0, 10, 10)])
    O._cache.clear()
    row = O.outcome_row(journal_row(), bars_root=root)
    assert row["decision_id"] == "daily:2026-09-28:2026-09-29"
    assert row["minutes"] == 4 and row["partial"] is True        # 4 of ~750 minutes
    assert row["open_gap_pts"] == pytest.approx(203 - 200)
    assert row["mae_pts"] == pytest.approx(281 - 200)
    assert row["stop_30_pnl_rs"] == round((200 - 281) * 65 - 360)    # 281 >= 260
    assert row["stop_100_pnl_rs"] == 5000                            # never fired: held
    assert row["time_cost"] == pytest.approx(121)
    assert row["house_reason"] == "time"                             # 15:15 IST before target
    assert row["move_sigma"] == pytest.approx(0.5)


def test_run_is_idempotent(tmp_path):
    root = str(tmp_path / "bars")
    write_session(root, SIG, [(4, 0, 101, 101)])
    jp, out = str(tmp_path / "j.csv"), str(tmp_path / "o.csv")
    pd.DataFrame([journal_row(), journal_row(structure="iron_condor", pnl_rs=None)]).to_csv(jp, index=False)
    O._cache.clear()
    assert len(O.run(journal_path=jp, bars_root=root, out=out)) == 1   # unsettled skipped
    assert len(O.run(journal_path=jp, bars_root=root, out=out)) == 0   # nothing new
    assert len(O.run(backfill=True, journal_path=jp, bars_root=root, out=out)) == 1
    assert len(pd.read_csv(out)) == 1

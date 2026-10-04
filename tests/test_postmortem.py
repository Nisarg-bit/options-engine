"""Post-mortem: range timing from closes, straddle decay, the Telegram lines."""
import postmortem as PM


def test_range_timing_uses_closes_and_reports_when_the_range_was_set():
    idx = [("09:15", 100), ("10:00", 110), ("10:15", 90), ("12:00", 95), ("15:29", 130)]
    r = PM.range_timing(idx)
    assert r["high"] == 130 and r["low"] == 90 and r["range_pts"] == 40
    assert r["high_at"] == "15:29" and r["low_at"] == "10:15"
    assert r["range_set_by_1015"] == 0.5          # 110-90 of 40
    assert r["range_set_by_1200"] == 0.5


def q(c, p):
    return {"CE": (c - 0.5, c + 0.5), "PE": (p - 0.5, p + 0.5)}


def test_straddle_curve_is_fixed_strike_mid_and_decay_is_from_the_first_mark():
    bm = {"09:20": {25000.0: q(100, 100), 25050.0: q(80, 120)},
          "09:21": {25000.0: q(99, 99)},                 # not on the 5-min grid, dropped
          "10:30": {25000.0: q(80, 80)},
          "13:00": {25000.0: q(40, 50)},
          "15:15": {25000.0: q(50, 50)}}
    curve = PM.straddle_curve(bm, 25000.0)
    assert curve == {"09:20": 200.0, "10:30": 160.0, "13:00": 90.0, "15:15": 100.0}
    d = PM.decay_stats(curve)
    assert d["captured_pct"] == 50.0 and d["bottom_at"] == "13:00" and d["bottom"] == 90.0
    assert d["checkpoints"]["12:00"] is None               # no mark there -> None, not guessed


def test_telegram_is_three_lines_and_flags_bad_data():
    pm = {"date": "2026-09-23",
          "day": {"close": 23446.8, "change_pts": 117.8, "range_pts": 110.0},
          "priced": {"move_pts": 88.0, "move_sigma": 0.62, "sigma_pts": 142.0},
          "straddle": {"start": 180.0, "at_1515": 60.0, "captured_pct": 66.7},
          "stop": {"stopped": False, "mae_session_pct": 0.12},
          "quality": {"note": "recovered morning"}}
    t = PM.telegram_lines(pm)
    assert t.count("\n") == 3
    assert "⚠ recovered morning" in t and "+0.62σ" in t and "not hit" in t
    pm["stop"] = {"stopped": True, "exit_at": "11:05"}
    assert "HIT at 11:05" in PM.telegram_lines(pm)


def test_a_late_start_is_named_in_the_message():
    pm = {"date": "2026-09-25", "full_session": False,
          "day": {"close": 23140.5, "change_pts": 77.4, "range_pts": 125.0},
          "priced": {"move_pts": 77.0, "move_sigma": 0.68, "sigma_pts": 114.0, "from": "12:07"},
          "straddle": {"start": 210.7, "at_1515": 204.8, "captured_pct": 2.8},
          "stop": {"stopped": False, "mae_session_pct": 0.12}, "quality": {"note": ""}}
    t = PM.telegram_lines(pm)
    assert t.count("(from 12:07, no quotes before)") == 2
    pm["full_session"] = True
    assert "from 12:07" not in PM.telegram_lines(pm)

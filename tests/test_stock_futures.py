"""Acceptance tests for the stock-futures paper system (futures/).

All data here is synthetic fixture data (futures/sf_fixtures.py). Each test
names the acceptance scenario it covers (S1..S10 in the feature brief).
"""
import json
import os
import threading
from datetime import timedelta

import pytest

import sf_fixtures as F
from sf_api import apply_override, decide_approval, journal, set_pause, state, summary, put_config
from sf_broker import bar_exit_check, position_qty, quote_price
from sf_core import (BUY, SELL, LONG, SHORT, OPEN, CLOSE, ReplayClock, Calendar, ist, parse_ts,
                     round_to_tick, gross_pnl, load_config, deep_merge)
from sf_data import FixtureData, LookAheadError, PointInTime
from sf_engine import Engine
from sf_store import Store

os.environ.setdefault("SF_RAISE", "")
D0 = F.INTRADAY_DAY
SW = F.SWING_DAY


# ---------------------------------------------------------------- helpers

def make(tmp_path, scenario, at="10:30", day=None, cfg=None, modes=("intraday",), fill_mode="quotes",
         env="fixture", edit=None, db="t.db"):
    sc = scenario
    data = sc.build()
    if edit:
        edit(sc, data)
    clock = ReplayClock(ist(day or sc.sessions[0], at))
    store = Store(str(tmp_path / db))
    e = Engine(store, FixtureData(data), clock, cfg or F.demo_config(), env=env, fill_mode=fill_mode, modes=modes)
    return sc, data, clock, store, e


def scan(e, mode="intraday"):
    sid = e.request_scan(mode)
    e.run_until_idle()
    return sid


def run_to(e, clock, until, step=1):
    """Step the injected clock minute by minute through sessions up to `until`,
    jumping across nights, weekends and holidays (one tick after each close)."""
    while clock.now() < until:
        now = clock.now()
        close = ist(now.date(), "15:30")
        if e.cal.in_session(now) or now < ist(now.date(), "09:15") and e.cal.is_session(now.date()):
            nxt = min(now + timedelta(minutes=step), until)
            if now < ist(now.date(), "09:15"):
                nxt = min(ist(now.date(), "09:15"), until)
            clock.set(nxt)
        elif now < close + timedelta(minutes=15) and e.cal.is_session(now.date()):
            clock.set(min(close + timedelta(minutes=15), until))
        else:
            d = e.cal.next_session(now.date())
            clock.set(min(ist(d, "09:15"), until))
        e.run_until_idle()


def ev(store, type_, **where):
    rows = store.events("type=?", type_)
    return [r for r in rows if all(r.get(k) == v or r["payload"].get(k) == v for k, v in where.items())]


def only_trade(store):
    trs = store.trades()
    assert len(trs) == 1, [t["trade_id"] for t in trs]
    return trs[0]["body"]


def iid_of(store):
    return only_trade(store)["instrument_id"]


def cfg_with(**over):
    return deep_merge(F.demo_config(), over)


def reconciled(store, trade_id):
    o, c = position_qty(store, trade_id)
    return o == c


# ====================================================================== S1

def test_s1_bullish_long_buys_one_lot_then_sells_to_close(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    reg = ev(st, "RegimeClassified")[0]["payload"]
    assert reg["regime"] == "BULLISH" and reg["permitted"] == [LONG]
    sec = ev(st, "SectorsRanked")[0]["payload"]["by_direction"][LONG]["qualifying"]
    assert sec[0]["sector"] == "IT" and sec[0]["source"].startswith("constituent_aggregation")
    t = only_trade(st)
    assert (t["underlying"], t["direction"], t["sector"]) == ("INFY", LONG, "IT")
    assert t["instrument_id"] == F.iid("INFY", "NOV") and t["requested_qty"] == F.LOT["INFY"] and t["lots"] == 1
    [entry] = st.orders("purpose='OPEN'")
    assert (entry["side"], entry["purpose"], entry["direction"]) == (BUY, OPEN, LONG)
    [f] = st.fills("purpose='OPEN'")
    q = FixtureData(data).quote(t["instrument_id"], clock.now())
    assert f["price"] == round_to_tick(q["ask"] + F.TICK, F.TICK, BUY)          # ask + 1 tick
    assert t["exit_plan"] if "exit_plan" in t else st.trade(t["trade_id"])["body"]["exit_plan"]
    run_to(e, clock, ist(D0, "15:20"))
    t = only_trade(st)
    assert t["status"] == "CLOSED" and t["exit"]["reason"] == "intraday_deadline"
    [x] = st.orders("purpose='CLOSE'")
    assert (x["side"], x["purpose"], x["direction"]) == (SELL, CLOSE, LONG)
    [xf] = st.fills("purpose='CLOSE'")
    q = FixtureData(data).quote(t["instrument_id"], parse_ts(xf["ts"]))
    assert xf["price"] == round_to_tick(q["bid"] - F.TICK, F.TICK, SELL)        # bid - 1 tick
    r = t["result"]
    assert r["entry_side"] == BUY and r["exit_side"] == SELL
    assert r["gross_pnl"] == pytest.approx((r["exit_avg"] - r["entry_avg"]) * r["closed_units"], abs=0.01)
    assert r["net_pnl"] == pytest.approx(r["gross_pnl"] - r["costs"], abs=0.01) and r["costs"] > 0
    assert reconciled(st, t["trade_id"])
    assert st.q("SELECT * FROM reservations") == []


# ====================================================================== S2

def test_s2_bearish_short_sells_to_open_and_buys_to_cover_at_target(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bearish())
    scan(e)
    sec = ev(st, "SectorsRanked")[0]["payload"]["by_direction"][SHORT]["qualifying"]
    assert sec[0]["sector"] == "Metal"
    t = only_trade(st)
    assert (t["underlying"], t["direction"]) == ("TATASTEEL", SHORT)
    [o] = st.orders("purpose='OPEN'")
    assert (o["side"], o["direction"]) == (SELL, SHORT)
    [f] = st.fills("purpose='OPEN'")
    q = FixtureData(data).quote(t["instrument_id"], clock.now())
    assert f["price"] == round_to_tick(q["bid"] - F.TICK, F.TICK, SELL)
    plan = st.trade(t["trade_id"])["body"]["exit_plan"]
    assert plan["stop_price"] > f["price"] > plan["target_price"]               # short: stop above, target below
    assert plan["evaluated_on"].startswith("ask")
    tgt = plan["target_price"]

    def crash(x):                     # ask falls below the target
        x["bid"], x["ask"] = round(tgt - 0.5, 2), round(tgt - 0.4, 2)
    sc.edit_quotes(t["instrument_id"], ist(D0, "11:00"), ist(D0, "15:30"), crash)
    run_to(e, clock, ist(D0, "11:02"))
    t = only_trade(st)
    assert t["status"] == "CLOSED" and t["exit"]["reason"] == "target"
    [x] = st.orders("purpose='CLOSE'")
    assert (x["side"], x["direction"]) == (BUY, SHORT)
    [xf] = st.fills("purpose='CLOSE'")
    assert xf["price"] == round_to_tick(tgt - 0.4 + F.TICK, F.TICK, BUY)       # ask + 1 tick
    r = t["result"]
    assert r["gross_pnl"] > 0
    assert r["gross_pnl"] == pytest.approx((r["entry_avg"] - r["exit_avg"]) * r["closed_units"], abs=0.01)


def test_s2_short_stop_uses_the_ask_side_and_loses(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bearish())
    scan(e)
    t = only_trade(st)
    stop = st.trade(t["trade_id"])["body"]["exit_plan"]["stop_price"]

    # bid still below the stop, ask above it: a short can only cover at the ask -> stop
    def squeeze(x):
        x["bid"], x["ask"] = round(stop - 0.2, 2), round(stop + 0.1, 2)
    sc.edit_quotes(t["instrument_id"], ist(D0, "11:30"), ist(D0, "15:30"), squeeze)
    run_to(e, clock, ist(D0, "11:32"))
    t = only_trade(st)
    assert t["exit"]["reason"] == "stop" and t["exit"]["trigger_price"] == stop
    assert t["result"]["gross_pnl"] < 0 and t["result"]["net_pnl"] < t["result"]["gross_pnl"]


def test_s2_long_target_uses_the_bid_side(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = only_trade(st)
    tgt = st.trade(t["trade_id"])["body"]["exit_plan"]["target_price"]

    def ask_only(x):                  # ask above target but bid below: not executable for a long
        x["bid"], x["ask"] = round(tgt - 0.3, 2), round(tgt + 0.2, 2)
    sc.edit_quotes(t["instrument_id"], ist(D0, "11:00"), ist(D0, "11:10"), ask_only)
    run_to(e, clock, ist(D0, "11:10"))
    assert only_trade(st)["status"] == "OPEN"


def test_pnl_signs_and_tick_rounding():
    assert gross_pnl(LONG, 100, 110, 10) == 100 and gross_pnl(SHORT, 100, 110, 10) == -100
    assert gross_pnl(SHORT, 110, 100, 10) == 100
    assert round_to_tick(100.01, 0.05, BUY) == 100.05 and round_to_tick(100.04, 0.05, SELL) == 100.0
    q = {"bid": 99.95, "ask": 100.0}
    assert quote_price(BUY, q, 0.05, 1) == 100.05 and quote_price(SELL, q, 0.05, 1) == 99.9


# ====================================================================== S3

def test_s3_sideways_blocks_new_entries(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.sideways())
    scan(e)
    assert ev(st, "RegimeClassified")[0]["payload"]["regime"] == "SIDEWAYS"
    stop = ev(st, "ScanStopped")[0]["payload"]
    assert stop["stage"] == "market_regime" and "SIDEWAYS" in stop["reason"]
    assert st.orders() == [] and st.trades() == []


def test_s3_stale_benchmark_is_unavailable_and_blocks(tmp_path):
    def stale(sc, d):
        d["bars"][F.BENCH]["1m"] = [b for b in d["bars"][F.BENCH]["1m"] if b["ts"] < f"{D0}T10:00"]
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=stale)
    scan(e)
    reg = ev(st, "RegimeClassified")[0]["payload"]
    assert reg["regime"] == "UNAVAILABLE" and reg["permitted"] == []
    assert any("stale" in r for r in reg["reasons"])
    assert st.orders() == []


def test_s3_sector_meeting_only_one_test_fails(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    rows = ev(st, "SectorsRanked")[0]["payload"]["by_direction"][LONG]["assessed"]
    ph = next(r for r in rows if r["sector"] == "Pharma")       # up, but less than NIFTY
    assert (ph["absolute_test"], ph["relative_test"], ph["result"]) == ("PASS", "FAIL", "FAIL")
    unmapped = ev(st, "SectorsRanked")[0]["payload"]["unmapped"]
    assert {"symbol": F.AMBIGUOUS, "reason": "ambiguous sector mapping"} in unmapped
    assert F.AMBIGUOUS not in [c["symbol"] for c in ev(st, "CandidatesRanked")[0]["payload"]["candidates"]]

    sc2, _, _, st2, e2 = make(tmp_path, F.bearish(), db="b.db")
    scan(e2)
    rows = ev(st2, "SectorsRanked")[0]["payload"]["by_direction"][SHORT]["assessed"]
    ph = next(r for r in rows if r["sector"] == "Pharma")       # down, but less than NIFTY: not "falling relative"
    assert (ph["absolute_test"], ph["relative_test"], ph["result"]) == ("PASS", "FAIL", "FAIL")
    it = next(r for r in rows if r["sector"] == "IT")
    assert it["result"] == "FAIL" and it["absolute_test"] == "FAIL"


def test_s3_monitoring_continues_when_entries_are_blocked(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    tid = only_trade(st)["trade_id"]
    data["bars"][F.BENCH]["1m"] = [b for b in data["bars"][F.BENCH]["1m"] if b["ts"] < f"{D0}T10:40"]
    set_pause(st, "global", True, "test", clock.now())
    run_to(e, clock, ist(D0, "11:00"))
    scan(e)
    assert ev(st, "RegimeClassified")[-1]["payload"]["regime"] == "UNAVAILABLE"
    run_to(e, clock, ist(D0, "15:20"))
    t = st.trade(tid)["body"]
    assert t["status"] == "CLOSED" and t["exit"]["reason"] == "intraday_deadline"


def test_pause_stops_entries_but_not_analysis(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    set_pause(st, "intraday", True, "test", clock.now())
    scan(e)
    assert ev(st, "CandidateVerified")
    assert "entries paused (intraday)" in ev(st, "ScanStopped")[0]["payload"]["reason"]
    assert st.orders() == []


# ====================================================================== S4

def test_s4_failed_verification_moves_to_runner_up(tmp_path):
    def no_flow(sc, d):
        for s in d["desk"]["INFY"]:
            s["metrics"]["money_flow_net_rs"] = -1.0
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=no_flow)
    scan(e)
    vr = ev(st, "VerificationResult")
    assert (vr[0]["payload"]["symbol"], vr[0]["payload"]["status"]) == ("INFY", "FAIL")
    assert (vr[1]["payload"]["symbol"], vr[1]["payload"]["status"]) == ("TCS", "PASS")
    assert only_trade(st)["underlying"] == "TCS"


def test_s4_stale_verification_is_unavailable(tmp_path):
    def stale(sc, d):
        d["desk"]["INFY"] = [s for s in d["desk"]["INFY"] if s["ts"] < f"{D0}T10:00"]
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=stale)
    scan(e)
    vr = ev(st, "VerificationResult")[0]["payload"]
    assert vr["status"] == "UNAVAILABLE" and "stale" in vr["reasons"][0]
    assert only_trade(st)["underlying"] == "TCS"


def test_s4_ineligible_contract_moves_to_runner_up(tmp_path):
    def no_tick(sc, d):
        for k in d["contracts"]:
            if k["underlying"] == "INFY":
                k["tick_size"] = None
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=no_tick)
    scan(e)
    rej = ev(st, "CandidateRejected")[0]["payload"]
    assert rej["symbol"] == "INFY" and "missing tick_size" in rej["reason"]
    assert only_trade(st)["underlying"] == "TCS"


def test_s4_ambiguous_contract_is_rejected(tmp_path):
    def dup(sc, d):
        k = next(k for k in d["contracts"] if k["underlying"] == "INFY" and "NOV" in k["instrument_id"])
        d["contracts"].append(dict(k, instrument_id="INFY-DUP"))
        for k in d["contracts"]:
            if k["underlying"] == "INFY" and "DEC" in k["instrument_id"]:
                k["status"] = "suspended"
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=dup)
    scan(e)
    rej = ev(st, "CandidateRejected")[0]["payload"]["reason"]
    assert "ambiguous" in rej and "suspended" in rej


def test_s4_expired_scan_cannot_enter(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    e.request_scan("intraday")
    for ag in e.a[:3]:                                   # regime, sectors, stocks at 10:30
        e.consume(ag, clock.now())
    clock.advance(minutes=10)                            # candidate_ttl is 240 s
    e.run_until_idle()
    stop = ev(st, "ScanStopped")[0]["payload"]
    assert "expired" in stop["reason"] and st.orders() == []


# ====================================================================== S5

def test_s5_intraday_exits_by_deadline(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    run_to(e, clock, ist(D0, "15:16"))
    t = only_trade(st)
    assert t["status"] == "CLOSED" and t["closed_at"] <= f"{D0}T15:16"
    assert t["exit"]["reason"] == "intraday_deadline"


def test_s5_missing_quote_at_deadline_is_an_unresolved_exception(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    tid = only_trade(st)["trade_id"]
    sc.drop_quotes(F.iid("INFY", "NOV"), ist(D0, "15:00"), ist(D0, "15:30"))
    run_to(e, clock, ist(D0, "15:29"))
    t = st.trade(tid)["body"]
    assert t["exit"]["reason"] == "intraday_deadline"           # intent recorded without a quote
    assert t["status"] == "EXITING" and t["mode"] == "intraday"
    assert t["unresolved"]["reason"].startswith("intraday_cutoff_exception")
    assert st.fills("purpose='CLOSE'") == []                    # no invented fill
    assert ev(st, "FillBlocked") and ev(st, "ExitUnresolved")
    row = next(r for r in journal(st) if r["trade_id"] == tid)
    assert row["class"] == "unresolved" and row["net_pnl"] is None
    assert summary(journal(st))["all"]["settled"] == 0


# ====================================================================== S6

def swing_cfg(**over):
    return cfg_with(profiles={"swing": over}) if over else F.demo_config()


def test_s6_swing_crosses_closure_restarts_and_exits_on_a_gap(tmp_path):
    sc = F.swing_bullish()
    sc, data, clock, st, e = make(tmp_path, sc, at="14:30", modes=("swing",))
    scan(e, "swing")
    t = only_trade(st)
    assert t["underlying"] == "INFY" and t["instrument_id"] == F.iid("INFY", "DEC")   # NOV too close to expiry
    assert t["pre_expiry_deadline"].startswith("2026-12-28")                          # session before 29 Dec expiry
    run_to(e, clock, ist(SW, "15:45"))
    assert only_trade(st)["status"] == "OPEN"
    # --- restart: a new engine on the same database, no config passed
    e2 = Engine(st, FixtureData(data), clock, None, env="fixture", modes=("swing",))
    e2.store.set_control("pause:swing", "1", clock.now(), "test")      # entries off: watch this position only
    run_to(e2, clock, ist(date_(2026, 11, 25), "12:00"))                  # Mon 23 trades, Tue 24 holiday
    t = only_trade(st)
    assert t["status"] == "OPEN"
    s = state(st, clock.now(), e2.data, e2)
    pos = s["positions"][0]
    assert pos["sessions_held"] == 2 and pos["overnight"] is True       # 23 and 25; 24 is not a session
    run_to(e2, clock, ist(date_(2026, 11, 26), "09:20"))
    t = only_trade(st)
    assert t["status"] == "CLOSED" and t["exit"]["reason"] == "target"
    fill = st.fills("purpose='CLOSE'")[0]
    assert fill["ts"].startswith("2026-11-26T09:15")                    # first eligible quote after the gap
    assert fill["price"] > t["exit"]["trigger_price"]                   # gap: fill differs from trigger
    assert t["result"]["exit_side"] == SELL


def date_(y, m, d):
    from datetime import date
    return date(y, m, d)


def test_s6_short_gaps_up_through_its_stop(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.swing_bearish(), at="14:30", modes=("swing",))
    scan(e, "swing")
    t = only_trade(st)
    assert (t["underlying"], t["direction"]) == ("TATASTEEL", SHORT)
    set_pause(st, "swing", True, "test", clock.now())
    run_to(e, clock, ist(date_(2026, 11, 23), "09:20"))
    t = only_trade(st)
    assert t["exit"]["reason"] == "stop"
    f = st.fills("purpose='CLOSE'")[0]
    assert f["side"] == BUY and f["ts"].startswith("2026-11-23T09:15")
    assert f["price"] > t["exit"]["trigger_price"] * 1.005              # filled well above the stop
    assert t["result"]["gross_pnl"] < 0
    assert t["result"]["gross_pnl"] == pytest.approx(
        (t["result"]["entry_avg"] - t["result"]["exit_avg"]) * t["result"]["closed_units"], abs=0.01)


def test_s6_pre_expiry_deadline_is_enforced(tmp_path):
    cfg = swing_cfg(exit_plan={"max_holding_sessions": None}, override={"enabled": False})
    sc, data, clock, st, e = make(tmp_path, F.swing_bullish(), at="14:30", cfg=cfg, modes=("swing",))
    scan(e, "swing")
    t = only_trade(st)
    # NOV expires on the 24 Nov holiday -> expiry session 23 Nov, buffer 1 -> deadline Fri 20 Nov 15:15
    assert t["instrument_id"] == F.iid("INFY", "NOV") and t["pre_expiry_deadline"] == "2026-11-20T15:15:00+05:30"
    set_pause(st, "swing", True, "test", clock.now())
    run_to(e, clock, ist(SW, "15:20"))
    t = only_trade(st)
    assert t["status"] == "CLOSED" and t["exit"]["reason"] == "pre_expiry_deadline"


def test_s6_unsimulatable_pre_expiry_exit_is_flagged_not_settled(tmp_path):
    cfg = swing_cfg(exit_plan={"max_holding_sessions": None}, override={"enabled": False})
    sc, data, clock, st, e = make(tmp_path, F.swing_bullish(), at="14:30", cfg=cfg, modes=("swing",))
    scan(e, "swing")
    tid = only_trade(st)["trade_id"]
    i = F.iid("INFY", "NOV")
    sc.drop_quotes(i, ist(SW, "14:40"), ist(date_(2026, 11, 30), "15:30"))
    set_pause(st, "swing", True, "test", clock.now())
    run_to(e, clock, ist(date_(2026, 11, 23), "15:46"))
    t = st.trade(tid)["body"]
    assert t["status"] == "UNRESOLVED" and t["unresolved"]["reason"].startswith("pre_expiry_exit_unfilled")
    assert st.fills("purpose='CLOSE'") == []
    assert all(o["status"] == "CANCELED" for o in st.orders("purpose='CLOSE'"))


def test_s6_max_holding_counts_sessions_after_entry():
    cal = Calendar()
    assert cal.sessions_between(SW, date_(2026, 11, 25)) == 2      # 23, 25 (24 Nov holiday)
    assert cal.sessions_between(SW, date_(2026, 11, 30)) == 5


# ====================================================================== S7

def test_s7_opposing_signal_cannot_create_a_second_position(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = only_trade(st)
    # a SHORT INFY signal from the swing profile arrives while INFY is held long intraday
    cand = dict(t["candidate"], direction=SHORT, rank=0)
    with st.tx() as c:
        c.execute("INSERT INTO scans(scan_id,mode,config_version,status,started,updated,body) VALUES(?,?,?,?,?,?,?)",
                  ("swing-x", "swing", e.config_version, "RUNNING", "", "", "{}"))
        st.emit(c, "CandidateVerified", "test", clock.now(), {"candidate": cand, "verification": {},
                "rank": 0, "expires_at": (clock.now() + timedelta(minutes=5)).isoformat()},
                scan_id="swing-x", mode="swing", config_version=e.config_version, direction=SHORT)
    cfg = e.cfg(e.config_version)
    cfg["profiles"]["swing"]["entry_window"] = ["09:15", "15:30"]
    e.run_until_idle()
    rej = ev(st, "CandidateRejected", scan_id="swing-x")[0]["payload"]["reason"]
    assert "already has an active intraday LONG" in rej and "no reversal" in rej
    assert len(st.trades()) == 1 and len(st.orders()) == 1


def test_s7_concurrent_profiles_share_capacity_without_duplicates(tmp_path):
    import copy
    cfg = F.demo_config()
    intra, swing = cfg["profiles"]["intraday"], cfg["profiles"]["swing"]
    for k in ("regime", "sector", "stock", "verification"):      # swing reads the same session data here
        swing[k] = copy.deepcopy(intra[k])
    swing["entry_window"] = ["09:15", "15:30"]
    intra["max_open_positions"] = 4
    sc, data, clock, st, e = make(tmp_path, F.bullish(), cfg=cfg, modes=("intraday", "swing"))
    e.schedule(clock.now())                               # both modes, same time, priority order
    e.run_until_idle()
    trs = st.trades()
    assert [t["mode"] for t in trs] == ["intraday", "swing"]
    assert trs[0]["underlying"] == "INFY" and trs[1]["underlying"] == "TCS"   # intraday priority wins INFY
    assert len({t["underlying"] for t in trs}) == len(trs)


def test_s7_threaded_workers_cannot_double_reserve(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    e.request_scan("intraday")
    for ag in e.a[:4]:
        e.consume(ag, clock.now())
    # four competing entry workers on separate connections
    engines = [Engine(Store(str(tmp_path / "t.db")), FixtureData(data), clock, None, worker_id=f"w{i}")
               for i in range(4)]
    barrier = threading.Barrier(4)

    def go(x):
        barrier.wait()
        x.consume(x.by_name["5-futures-entry"], clock.now())
    ths = [threading.Thread(target=go, args=(x,)) for x in engines]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(st.orders()) == 1 and len(st.q("SELECT * FROM reservations")) == 1


def test_s7_one_lot_over_a_limit_is_skipped_not_resized(tmp_path):
    cfg = cfg_with(profiles={"intraday": {"max_gross_notional_rs": 100000}})
    sc, data, clock, st, e = make(tmp_path, F.bullish(), cfg=cfg)
    scan(e)
    rej = ev(st, "CandidateRejected")
    assert rej and all("limit:" in r["payload"]["reason"] and "skipped" in r["payload"]["reason"] for r in rej)
    assert st.orders() == [] and st.trades() == []
    assert ev(st, "ScanStopped")


# ====================================================================== S8

def test_s8_duplicate_events_and_retries_do_not_duplicate_orders(tmp_path, monkeypatch):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    sid = e.request_scan("intraday")
    e.request_scan("intraday")                          # same scan id: ignored
    for ag in e.a[:4]:
        e.consume(ag, clock.now())
    verified = ev(st, "CandidateVerified")[0]
    with st.tx() as c:                                  # duplicate publish of the same event id
        st.emit(c, "CandidateVerified", "dup", clock.now(), verified["payload"], event_id=verified["event_id"],
                scan_id=sid, mode="intraday", config_version=e.config_version)
    entry = e.by_name["5-futures-entry"]
    calls = {"n": 0}
    real = entry.on_verified

    def flaky(c, evt, now):
        calls["n"] += 1
        if calls["n"] == 1:
            real(c, evt, now)
            raise RuntimeError("worker died after writing")   # whole step must roll back
        return real(c, evt, now)
    monkeypatch.setattr(entry, "on_verified", flaky)
    e.consume(entry, clock.now())
    assert st.orders() == [] and st.q("SELECT * FROM reservations") == []
    assert st.q("SELECT attempts FROM failures")[0]["attempts"] == 1
    e.run_until_idle()
    e.run_until_idle()
    assert len(st.orders("purpose='OPEN'")) == 1 and len(ev(st, "CandidateVerified")) == 1


def test_s8_partial_fills_late_fill_during_cancel_and_competing_exits(tmp_path):
    cfg = cfg_with(shared={"fill_policy": {"partial_fills": True, "granularity": "unit"}},
                   profiles={"intraday": {"contract": {"min_top_qty_lots": 0}}})   # allow a thin book

    def thin(sc, d):
        for q in d["quotes"][F.iid("INFY", "NOV")]:
            q["ask_qty"] = 150                          # less than one 400-unit lot on show
    sc, data, clock, st, e = make(tmp_path, F.bullish(), cfg=cfg, edit=thin)
    scan(e)
    t = only_trade(st)
    o = st.orders("purpose='OPEN'")[0]
    assert o["status"] == "PARTIAL" and o["filled_qty"] == 150
    assert len(ev(st, "PositionOpened")) == 1 and t["status"] == "OPEN"
    # force an exit while the entry is still partial; two exit workers race for it
    plan_stop = st.trade(t["trade_id"])["body"]["exit_plan"]["stop_price"]
    sc.edit_quotes(t["instrument_id"], clock.now() + timedelta(minutes=1), ist(D0, "15:30"),
                   lambda x: x.update(bid=round(plan_stop - 1, 2), ask=round(plan_stop - 0.9, 2), ask_qty=0))
    clock.advance(minutes=1)
    e.broker.step(clock.now())
    e.by_name["6-position-monitor"].poll(clock.now())
    engines = [Engine(Store(str(tmp_path / "t.db")), FixtureData(data), clock, None, worker_id=f"x{i}") for i in range(3)]
    b = threading.Barrier(3)

    def go(x):
        b.wait()
        x.consume(x.by_name["7-paper-exit"], clock.now())
    ths = [threading.Thread(target=go, args=(x,)) for x in engines]
    [h.start() for h in ths]
    [h.join() for h in ths]
    assert len(st.q("SELECT * FROM exit_claims")) == 1
    entry = st.orders("purpose='OPEN'")[0]
    assert entry["status"] == "CANCEL_PENDING"
    # the exchange reports a late fill for the rest of the entry during the cancel
    e.broker.fill(entry, 250, entry["body"].get("price", 1500.0), clock.now(), {"model": "late-fill"},
                  fill_id="late-1")
    e.broker.fill(entry, 250, 1500.0, clock.now(), {"model": "late-fill"}, fill_id="late-1")   # duplicate report
    run_to(e, clock, clock.now() + timedelta(minutes=5))
    t = st.trade(t["trade_id"])["body"]
    assert t["status"] == "CLOSED"
    o_, c_ = position_qty(st, t["trade_id"])
    assert o_ == c_ == 400                              # closed exactly what was bought
    closes = st.orders("purpose='CLOSE'")
    assert all(x["side"] == SELL for x in closes) and sum(x["filled_qty"] for x in closes) == 400
    assert t["result"]["closed_units"] == 400


def test_s8_close_order_can_never_flip_the_position(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = only_trade(st)
    e.store.set_control("x", "1", clock.now(), "t")
    with st.tx() as c:
        st.emit(c, "ExitIntent", "test", clock.now(), {"primary": "stop", "triggered": ["stop"], "details": {}},
                trade_id=t["trade_id"], mode="intraday", event_id=f"exit-intent:{t['trade_id']}")
    e.consume(e.by_name["7-paper-exit"], clock.now())
    close = st.orders("purpose='CLOSE'")[0]
    assert close["requested_qty"] == 400
    got = e.broker.fill(close, 10_000, 1500.0, clock.now(), {"model": "test"})
    assert got["qty"] == 400                            # capped at the open position
    assert e.broker.fill(st.orders("purpose='CLOSE'")[0], 5, 1500.0, clock.now(), {"model": "test"}) is None
    o_, c_ = position_qty(st, t["trade_id"])
    assert o_ == c_


# ====================================================================== S9

def test_s9_default_exit_runs_without_a_reply(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    assert ev(st, "OverrideRequested")
    run_to(e, clock, ist(D0, "15:20"))
    assert only_trade(st)["status"] == "CLOSED"
    a = st.q("SELECT * FROM approvals WHERE kind='override'")[0]
    assert a["status"] == "EXPIRED" and a["decided"]   # nobody replied; default plan did the work
    assert not st.q("SELECT * FROM audit WHERE action='override'")


def test_s9_valid_override_is_audited_and_used(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = st.trade(only_trade(st)["trade_id"])["body"]
    new_stop = round(t["entry_avg"] * 0.995, 2)
    ok, why = apply_override(st, e, t["trade_id"], {"stop_price": new_stop}, "basic:nisarg", clock.now())
    assert ok, why
    t = st.trade(t["trade_id"])["body"]
    assert t["exit_plan"]["stop_price"] == new_stop and len(t["plan_history"]) == 2
    assert st.q("SELECT * FROM audit WHERE action='override' AND outcome='applied'")
    sc.edit_quotes(t["instrument_id"], clock.now() + timedelta(minutes=1), ist(D0, "15:30"),
                   lambda x: x.update(bid=round(new_stop - 0.5, 2), ask=round(new_stop - 0.4, 2)))
    run_to(e, clock, clock.now() + timedelta(minutes=3))
    t = st.trade(t["trade_id"])["body"]
    assert t["exit"]["reason"] == "stop" and t["exit"]["trigger_price"] == new_stop


def test_s9_late_and_invalid_overrides_are_rejected(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = st.trade(only_trade(st)["trade_id"])["body"]
    ok, why = apply_override(st, e, t["trade_id"], {"stop_price": t["entry_avg"] * 1.2}, "op", clock.now())
    assert not ok and "wrong side" in why
    ok, why = apply_override(st, e, t["trade_id"], {"stop_price": t["entry_avg"] * 0.9}, "op", clock.now())
    assert not ok and "hard risk limit" in why
    ok, why = apply_override(st, e, t["trade_id"], {"max_holding_sessions": 3}, "op", clock.now())
    assert not ok and "only" in why
    run_to(e, clock, clock.now() + timedelta(minutes=6))       # window is 300 s
    ok, why = apply_override(st, e, t["trade_id"], {"stop_price": t["entry_avg"] * 0.995}, "op", clock.now())
    assert not ok and why.startswith("late")
    after = st.trade(t["trade_id"])["body"]
    assert after["exit_plan"]["stop"]["basis"] == "opening_range_edge" and len(after["plan_history"]) == 1   # default untouched
    assert len(st.q("SELECT * FROM audit WHERE action='override' AND outcome='rejected'")) == 4


def test_s9_override_racing_a_claimed_exit_cannot_undo_it(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = st.trade(only_trade(st)["trade_id"])["body"]
    with st.tx() as c:
        st.emit(c, "ExitIntent", "6-position-monitor", clock.now(), {"primary": "stop", "triggered": ["stop"],
                "details": {}}, trade_id=t["trade_id"], mode="intraday", event_id=f"exit-intent:{t['trade_id']}")
    results = {}
    b = threading.Barrier(2)

    def exit_worker():
        x = Engine(Store(str(tmp_path / "t.db")), FixtureData(data), clock, None, worker_id="exit")
        b.wait()
        x.consume(x.by_name["7-paper-exit"], clock.now())

    def operator():
        b.wait()
        results["ov"] = apply_override(Store(str(tmp_path / "t.db")), e, t["trade_id"],
                                       {"stop_price": round(t["entry_avg"] * 0.995, 2)}, "op", clock.now())
    ths = [threading.Thread(target=exit_worker), threading.Thread(target=operator)]
    [h.start() for h in ths]
    [h.join() for h in ths]
    t2 = st.trade(t["trade_id"])["body"]
    assert t2["status"] in ("EXITING", "CLOSED")       # the claim stands either way
    ok, why = apply_override(st, e, t["trade_id"], {"stop_price": round(t["entry_avg"] * 0.996, 2)}, "op", clock.now())
    assert not ok and ("claimed" in why or "EXITING" in why or "CLOSED" in why)


def test_s9_entry_approval_rechecks_after_delay_and_expires(tmp_path):
    cfg = cfg_with(profiles={"intraday": {"approval": {"entry_required": True, "entry_window_s": 120}}})
    sc, data, clock, st, e = make(tmp_path, F.bullish(), cfg=cfg)
    scan(e)
    assert st.orders() == [] and ev(st, "EntryApprovalRequested")
    aid = st.q("SELECT approval_id FROM approvals WHERE kind='entry'")[0]["approval_id"]
    clock.advance(minutes=1)
    ok, _ = decide_approval(st, aid, "APPROVE", "op", clock.now())
    e.run_until_idle()
    assert ok and len(st.orders("purpose='OPEN'")) == 1

    sc2, data2, clock2, st2, e2 = make(tmp_path, F.bullish(), cfg=cfg, db="exp.db")
    scan(e2)
    clock2.advance(minutes=3)                           # approval window 120 s passes with no reply
    e2.run_until_idle()
    a = st2.q("SELECT * FROM approvals WHERE kind='entry' ORDER BY created")
    assert a[0]["status"] == "EXPIRED"
    assert st2.trades("status='CANCELED'")
    assert all(o["purpose"] == "OPEN" for o in st2.orders()) and not any(
        o["trade_id"] == a[0]["trade_id"] for o in st2.orders())


# ====================================================================== S10

def test_s10_point_in_time_guard_refuses_look_ahead(tmp_path):
    sc = F.bullish()
    data = sc.build()

    class Leaky(FixtureData):
        def quote(self, iid, as_of):                    # returns tomorrow's quote
            return self.d["quotes"][iid][-1]
    pit = PointInTime(Leaky(data))
    with pytest.raises(LookAheadError):
        pit.quote(F.iid("INFY", "NOV"), ist(D0, "10:30"))
    fx = FixtureData(data)
    bars = fx.bars("INFY", ist(D0, "10:30"), "1m")
    assert parse_ts(bars[-1]["available_at"]) <= ist(D0, "10:30")
    assert bars[-1]["ts"] == f"{D0}T10:29:00+05:30"     # the 10:30 bar is not closed yet
    daily = fx.bars("INFY", ist(D0, "15:00"), "1d")
    assert daily[-1]["ts"][:10] < str(D0)               # today's daily bar only after the close


def test_s10_revised_snapshot_is_not_visible_before_its_revision(tmp_path):
    def revise(sc, d):
        d["desk"]["INFY"].append({"symbol": "INFY", "ts": f"{D0}T10:25:00+05:30", "available_at": f"{D0}T11:00:00+05:30",
                                  "source": "fixture desk", "revision": 2,
                                  "metrics": {"turnover_rs": 1.0, "money_flow_net_rs": -1.0, "oi_change_pct": -50},
                                  "kinds": {}})
    sc, data, clock, st, e = make(tmp_path, F.bullish(), edit=revise)
    fx = FixtureData(data)
    assert fx.desk_snapshot("INFY", ist(D0, "10:30"))["metrics"]["money_flow_net_rs"] > 0
    assert fx.desk_snapshot("INFY", ist(D0, "11:01"))["metrics"]["money_flow_net_rs"] > 0   # later ts wins
    rows = [s for s in data["desk"]["INFY"] if s["ts"] == f"{D0}T10:25:00+05:30"]
    from sf_data import _latest_revision, _visible
    assert _latest_revision(_visible(rows, ist(D0, "10:30")))[-1].get("revision") is None
    assert _latest_revision(_visible(rows, ist(D0, "11:00")))[-1]["revision"] == 2
    scan(e)
    assert ev(st, "VerificationResult")[0]["payload"]["status"] == "PASS"


def test_s10_full_replay_reconciles_every_record(tmp_path):
    from sf_cli import replay
    for name in ("bullish", "bearish"):
        sc = getattr(F, name)()
        sc, data, clock, st, e = make(tmp_path, sc, at="09:15", db=f"{name}.db")
        replay(e, clock, sc.sessions)
        rows = journal(st)
        assert rows and all(r["class"] == "settled" for r in rows)
        for tr in st.trades():
            t = tr["body"]
            fills = st.fills("trade_id=?", t["trade_id"])
            o = [f for f in fills if f["purpose"] == OPEN]
            c = [f for f in fills if f["purpose"] == CLOSE]
            assert sum(f["qty"] for f in o) == sum(f["qty"] for f in c) == t["requested_qty"]
            ea = sum(f["qty"] * f["price"] for f in o) / sum(f["qty"] for f in o)
            xa = sum(f["qty"] * f["price"] for f in c) / sum(f["qty"] for f in c)
            assert t["result"]["gross_pnl"] == pytest.approx(gross_pnl(t["direction"], ea, xa, t["requested_qty"]), abs=0.01)
            assert t["result"]["costs"] == pytest.approx(sum(f["body"]["costs"]["total"] for f in fills), abs=0.01)
            for f in fills:                             # every fill came from a quote visible at fill time
                assert parse_ts(f["body"]["evidence"]["quote_ts"]) <= parse_ts(f["ts"])
        s = summary(rows)["all"]
        assert s["net_pnl"] == pytest.approx(sum(r["net_pnl"] for r in rows), abs=0.01)
        assert st.q("SELECT * FROM reservations") == [] and st.q("SELECT * FROM failures") == []


def test_s10_bar_only_replay_fill_model(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish(), fill_mode="bars")
    scan(e)
    assert st.fills() == []                            # next bar not visible yet
    clock.advance(minutes=1)
    e.run_until_idle()
    [f] = st.fills()
    assert f["body"]["evidence"]["model"] == "next_bar_open/1"
    assert f["body"]["evidence"]["bar_ts"] == f"{D0}T10:30:00+05:30"
    assert bar_exit_check({"low": 95, "high": 106}, LONG, 96, 105) == ("stop", True)
    assert bar_exit_check({"low": 95, "high": 106}, LONG, 96, 105, "target_first") == ("target", True)
    assert bar_exit_check({"low": 99, "high": 106}, SHORT, 105, 90) == ("stop", False)


def test_config_versions_bind_to_trades(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    t = only_trade(st)
    v1 = t["config_version"]
    new = F.demo_config()
    new["profiles"]["intraday"]["exit_plan"]["stop"]["value"] = 0.5
    ok, v2 = put_config(st, new, "op", clock.now())
    assert ok and v2 != v1
    assert st.trade(t["trade_id"])["body"]["config_version"] == v1      # open trade keeps its version
    bad = F.demo_config()
    bad["profiles"]["intraday"]["entry_window"] = ["09:45", "15:20"]
    ok, err = put_config(st, bad, "op", clock.now())
    assert not ok and "cutoff" in err


def gated_config():
    cfg = F.demo_config()
    cfg["rules_validated"] = False
    cfg["live"]["entries_enabled"] = False
    return cfg


def test_live_paper_entries_are_disabled_until_configured(tmp_path):
    sc, data, clock, st, e = make(tmp_path, F.bullish(), env="paper", cfg=gated_config())
    scan(e)
    stop = ev(st, "ScanStopped")[0]["payload"]["reason"]
    assert "current-session entries disabled" in stop and "rules_validated" in stop
    assert st.orders() == []
    assert ev(st, "CandidateVerified")                  # analysis still ran


def test_dashboard_state_and_operator_auth(tmp_path, monkeypatch):
    pytest.importorskip("httpx")                      # FastAPI's TestClient needs it
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sf_api import make_router
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    app = FastAPI()
    app.include_router(make_router(lambda: st, lambda: e))
    cl = TestClient(app)
    s = cl.get("/api/futures/state").json()
    assert s["profiles"]["intraday"]["last_scan"]["regime"]["regime"] == "BULLISH"
    assert s["positions"][0]["direction"] == LONG and s["positions"][0]["timeline"]
    for k in ("DASH_USER", "DASH_PASS", "DASH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    r = cl.post("/api/futures/ops/pause", json={"scope": "global", "paused": True})
    assert r.status_code == 403                         # no auth configured -> read-only
    monkeypatch.setenv("DASH_USER", "op")
    monkeypatch.setenv("DASH_PASS", "pw")
    assert cl.post("/api/futures/ops/pause", json={"scope": "global", "paused": True}).status_code == 401
    r = cl.post("/api/futures/ops/pause", json={"scope": "global", "paused": True}, auth=("op", "pw"))
    assert r.status_code == 200 and st.paused("intraday")
    assert st.q("SELECT actor FROM audit WHERE action='set pause:global'")[0]["actor"] == "basic:op"


# ====================================================================== repo adapter

def test_repo_adapter_reads_the_collectors_layout(tmp_path):
    """RepoData against files written in the collector's exact formats
    (writer.SCHEMA Parquet under data/stocks_1m and data/bars_1m, an
    instruments.py master, sectors.py map). Values are synthetic."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    import writer
    from sf_data import RepoData
    from sf_engine import Engine
    day = D0
    root = tmp_path
    t0 = pd.Timestamp(f"{day} 09:15", tz="Asia/Kolkata").tz_convert("UTC")

    def part(tier, und, rows):
        d = root / "data" / tier / f"date={day}" / f"underlying={und}"
        d.mkdir(parents=True, exist_ok=True)
        cols = {f.name: [r.get(f.name) for r in rows] for f in writer.SCHEMA}
        pq.write_table(pa.table(cols, schema=writer.SCHEMA), d / "part-0.parquet")

    def bar(i, tok, sym, und, px, **kw):
        base = {"ts": (t0 + pd.Timedelta(minutes=i)).to_pydatetime(), "instrument_token": tok, "tradingsymbol": sym,
                "underlying": und, "expiry": None, "strike": None, "opt_type": None, "open": px, "high": px,
                "low": px, "close": px, "volume": 1000, "pv": px * 1000, "oi": None, "bid": None, "ask": None,
                "bid_qty": None, "ask_qty": None, "tick_count": 5, "is_interpolated": False}
        base.update(kw)
        return base
    from datetime import date as _d
    part("stocks_1m", "EQ", [bar(i, 1, "INFY", "INFY", 1500 + i) for i in range(90)])
    part("stocks_1m", "STKFUT", [bar(i, 11, "INFY26NOVFUT", "INFY", 1503 + i, oi=100000 + 100 * i, expiry=_d(2026, 11, 24),
                                     bid=1502.95 + i, ask=1503.05 + i, bid_qty=800, ask_qty=1200) for i in range(90)])
    part("bars_1m", "INDEX", [bar(i, 256265, "NIFTY 50", "INDEX", 24000 + i) for i in range(90)])
    (root / "data" / "instruments").mkdir(parents=True)
    master = {"date": str(day), "generated_at": f"{day}T02:30:00+00:00", "instruments": [
        {"instrument_token": 1, "tradingsymbol": "INFY", "name": "INFY", "kind": "stock", "lot_size": 400, "prev_close": 1490.0},
        {"instrument_token": 11, "tradingsymbol": "INFY26NOVFUT", "name": "INFY", "kind": "stock_fut", "expiry": "2026-11-24",
         "instrument_type": "FUT", "lot_size": 400, "tick_size": 0.1, "prev_close": 1492.0}]}
    (root / "data" / "instruments" / f"{day}.json").write_text(json.dumps(master))
    (root / "data" / "sectors.json").write_text(json.dumps({"date": str(day), "map": {"INFY": {"sector": "IT"}}}))
    r = RepoData(str(root))
    now = ist(day, "10:00")
    assert r.universe(now) == ["INFY"]
    bars = r.bars("INFY", now, "1m")
    assert bars[-1]["ts"] == f"{day}T09:59:00+05:30"            # 10:00 bar not closed at 10:00
    assert r.bars("NIFTY 50", now, "1m")[-1]["close"] == 24044
    [k] = r.contracts("INFY", now)
    assert (k["instrument_id"], k["tick_size"], k["lot_size"], k["instrument_type"]) == ("11", 0.1, 400, "FUTSTK")
    q = r.quote("11", now)
    assert q["bid"] == pytest.approx(1502.95 + 44) and q["ts"] == f"{day}T10:00:00+05:30"
    snap = r.desk_snapshot("INFY", now)
    assert snap["kinds"]["money_flow_net_rs"] == "estimate" and snap["metrics"]["oi_change_pct"] > 0
    h = r.health(now)
    assert h["feeds"]["futstk_master"] == "ok" and h["feeds"]["stock_bars"] == "ok"
    # a master without tick_size is reported, and the contract agent rejects it
    master["instruments"][1].pop("tick_size")
    (root / "data" / "instruments" / f"{day}.json").write_text(json.dumps(master))
    r2 = RepoData(str(root))
    assert r2.health(now)["feeds"]["futstk_master"] == "incomplete: no tick_size"
    # the live gate names exactly what is missing
    st = Store(str(tmp_path / "g.db"))
    e = Engine(st, r2, ReplayClock(now), gated_config(), env="paper")
    gate = e.live_gate()
    assert "entries_enabled is false" in gate and "futstk_master: incomplete" in gate


def test_snapshot_is_what_the_read_only_services_serve(tmp_path, monkeypatch):
    import sf_api
    sc, data, clock, st, e = make(tmp_path, F.bullish())
    scan(e)
    snap = tmp_path / "state.json"
    monkeypatch.setenv("SF_SNAPSHOT", str(snap))
    sf_api.write_snapshot(st, e)
    out = sf_api.publish()
    assert out["positions"][0]["underlying"] == "INFY" and "snapshot_age_s" in out
    assert "may be stopped" in out["notes"][0]          # fixture clock is in the past -> flagged stale
    monkeypatch.setenv("SF_SNAPSHOT", str(tmp_path / "missing.json"))
    assert sf_api.publish()["available"] is False


def test_enabled_config_still_waits_for_healthy_feeds(tmp_path):
    cfg = F.demo_config()
    assert cfg["rules_validated"] and cfg["live"]["entries_enabled"]      # shipped config: entries on
    sc, data, clock, st, e = make(tmp_path, F.bullish(), env="paper", cfg=cfg)
    gate = e.live_gate()
    assert gate and "feeds not ready" in gate and "entries_enabled" not in gate

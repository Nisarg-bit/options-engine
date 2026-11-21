"""Daily evaluation, champion/challenger governance and the audit tools
(sf_daily.py, sf_governance.py, sf_metrics.py, sf_audit.py, sf_experiments.py).

All evidence here is FIXTURE evidence: synthetic sessions (sf_fixtures.py) or
hand-made journal rows. Existing acceptance tests already cover long/short
sides and P&L (S1/S2), missing quotes (S5), shared limits across profiles (S7),
duplicate events (S8) and point-in-time data (S10) in test_stock_futures.py.
"""
import json
import os
from datetime import date, datetime, timedelta

import pytest

import sf_audit as AU
import sf_daily as DY
import sf_fixtures as F
import sf_governance as G
import sf_metrics as M
import sf_regime as R
from sf_core import IST, LONG, SHORT, ReplayClock, config_hash, deep_merge, gross_pnl, ist, load_config
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store

POL = G.load_policy()
DEPLOYED = load_config()          # repo: futures/config/default.json; server: futures_config.json


def row(mode="intraday", d=LONG, net=100.0, day="2026-11-19", cls="settled", units=100, tick=0.05):
    return {"mode": mode, "direction": d, "net_pnl": net, "gross_pnl": None if net is None else net + 50,
            "costs": 50.0, "class": cls,
            "opened_at": f"{day}T10:00:00+05:30", "closed_at": f"{day}T15:15:00+05:30",
            "entry_avg": 500.0, "closed_units": units, "tick_size": tick, "trade_id": f"t-{day}-{net}"}


# ------------------------------------------------------------------ correctness fix

def _bars(day, rows):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        t = ist(day, "09:15") + timedelta(minutes=i)
        out.append({"ts": t.isoformat(), "available_at": (t + timedelta(minutes=1)).isoformat(),
                    "open": o, "high": h, "low": l, "close": c, "volume": 10, "turnover": c * 10})
    return out


def test_first_bar_close_only_drops_the_chained_open():
    day = date(2026, 11, 20)
    # 09:15 bar as the collector writes it: open = yesterday's close 100, which never traded today
    rows = [(100, 100, 98.0, 98.5)] + [(98.5, 99.0, 98.2, 98.8)] * 14
    bars = _bars(day, rows)
    now = ist(day, "09:31")
    v1 = R.opening_range(bars, day, "09:15", 15, now)
    fix = R.opening_range(bars, day, "09:15", 15, now, first_bar="close_only")
    assert v1["or_high"] == "100" and v1["or_low"] == "98.0"          # the untraded 100 sets the high
    assert fix["or_high"] == "99.0" and fix["or_low"] == "98.2"
    assert v1["first_bar"] == "ohlc" and fix["first_bar"] == "close_only"
    with pytest.raises(ValueError):
        R.opening_range(bars, day, "09:15", 15, now, first_bar="nope")


def test_opening_range_never_reads_a_bar_not_yet_available():
    day = date(2026, 11, 20)
    bars = _bars(day, [(100, 101, 99, 100)] * 15)
    assert R.opening_range(bars, day, "09:15", 15, ist(day, "09:29"))["status"] == "NOT_READY"
    assert R.opening_range(bars, day, "09:15", 15, ist(day, "09:30"))["status"] == "READY"


# ------------------------------------------------------------------ metrics

def test_long_and_short_pnl_signs():
    assert gross_pnl(LONG, 100, 110, 10) == 100 and gross_pnl(SHORT, 100, 110, 10) == -100
    assert gross_pnl(SHORT, 110, 100, 10) == 100


def test_metrics_exclude_unresolved_and_report_undefined():
    rows = [row(net=200), row(net=300), row(cls="unresolved", net=-9999), row(cls="excluded", net=-5000)]
    m = M.metrics(rows)
    assert m["settled"] == 2 and m["net_pnl"] == 500 and m["unresolved"] == 1 and m["excluded"] == 1
    assert m["profit_factor"] is None and "no losing trade" in m["undefined"]["profit_factor"]
    assert m["ci80"] is None and m["avg_loss"] is None
    assert M.metrics([])["expectancy"] is None


def test_metrics_split_by_mode_and_direction():
    rows = [row(net=100), row(d=SHORT, net=-50), row(mode="swing", net=30)]
    b = M.by_mode_direction(rows)
    assert b["intraday:LONG"]["net_pnl"] == 100 and b["intraday:SHORT"]["net_pnl"] == -50
    assert b["swing:SHORT"]["settled"] == 0 and b["portfolio"]["net_pnl"] == 80


def test_cost_stress_removes_two_ticks_per_extra_tick_of_slippage():
    s = M.stressed([row(net=100, units=100, tick=0.05)], extra_ticks_per_side=1)
    assert s[0]["net_pnl"] == pytest.approx(100 - 2 * 0.05 * 100)


# ------------------------------------------------------------------ chronological evaluation

def test_chrono_split_is_ordered_with_embargo_and_burned_tests():
    s = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
    (tr, va, te), _ = G.chrono_split(list(reversed(s)), POL)
    assert max(tr) < min(va) and max(va) < min(te)                     # strictly chronological
    assert G.chrono_split(s, POL, embargo=6)[0] is None                # 20 - 12 embargo < 15: refused
    s32 = [date(2026, 1, 1) + timedelta(days=i) for i in range(32)]
    (tr2, va2, te2), _ = G.chrono_split(s32, POL, embargo=6)
    assert (min(va2) - max(tr2)).days > 6 and (min(te2) - max(va2)).days > 6   # no swing trade spans a boundary
    out, why = G.chrono_split(s, POL, burned=set(te))
    assert out is None and "burned" in why                              # a test window is never reused
    assert G.chrono_split(s[:5], POL)[0] is None


def test_open_swing_trade_at_a_window_end_is_not_settled():
    m = M.metrics([row(mode="swing", net=None, cls="open"), row(mode="swing", net=40)])
    assert m["settled"] == 1 and m["open"] == 1 and m["net_pnl"] == 40


# ------------------------------------------------------------------ search space and hard constraints

def test_search_space_rejects_lot_limit_and_cost_changes():
    c = json.loads(json.dumps(DEPLOYED))
    c["shared"]["max_open_positions"] = 10
    ok, probs = G.in_search_space(DEPLOYED, c, POL)
    assert not ok and "not in the approved search space" in probs[0]
    c2 = json.loads(json.dumps(DEPLOYED))
    c2["profiles"]["intraday"]["exit_plan"]["hard_max_stop_pct"] = 9.0
    assert not G.in_search_space(DEPLOYED, c2, POL)[0]
    c3 = json.loads(json.dumps(DEPLOYED))
    c3["profiles"]["intraday"]["exit_plan"]["hard_max_stop_pct"] = 2.0
    assert G.in_search_space(DEPLOYED, c3, POL) == (True, [])


def test_neighbours_stay_inside_the_space():
    ns = G.neighbours(DEPLOYED, POL)
    assert ns and all(G.in_search_space(DEPLOYED, c, POL)[0] for _, _, c in ns)


# ------------------------------------------------------------------ drift / deterioration / eligibility

def test_drift_needs_minimum_history_then_flags_outliers():
    assert G.drift([{"trades": 3}] * 3, {"trades": 3}, POL)["status"] == "INSUFFICIENT"
    hist = [{"trades": t, "eq_zero_volume_share": 0.03} for t in (3, 2, 4, 3, 3, 2, 4)]
    assert G.drift(hist, {"trades": 3, "eq_zero_volume_share": 0.03}, POL)["status"] == "OK"
    out = G.drift(hist, {"trades": 3, "eq_zero_volume_share": 0.5}, POL)
    assert out["status"] == "DRIFT" and "eq_zero_volume_share" in out["flagged"]


def test_deterioration_requires_minimum_trades():
    assert G.deterioration([row(net=-100)] * 5, "intraday", POL)["status"] == "INSUFFICIENT"
    assert G.deterioration([row(net=-100 - i) for i in range(25)], "intraday", POL)["status"] == "DETERIORATED"


def test_eligibility_gates():
    ok, why = G.eligible_for_experiments(2, {"intraday": 6, "swing": 0}, [], POL)
    assert not ok and len(why) == 2
    assert G.eligible_for_experiments(5, {"intraday": 25, "swing": 0}, [], POL)[0]
    assert G.eligible_for_experiments(5, {"intraday": 2, "swing": 0}, ["drift"], POL)[0]


# ------------------------------------------------------------------ promotion decisions

def _days(n, start=date(2026, 12, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_insufficient_evidence_never_promotes():
    days = _days(4)
    champ = [row(net=-100, day=str(d)) for d in days]
    chal = [row(net=500, day=str(d)) for d in days]
    a = G.assess_promotion(champ, chal, days, POL)
    assert a["decision"] == "CONTINUE_SHADOW"
    assert {"forward shadow sessions", "settled shadow trades"} <= set(a["unmet"])


def test_full_evidence_promotes_and_a_failed_challenger_is_rejected():
    days = _days(24)
    champ = [row(net=-50 + (i % 3) * 10, day=str(d)) for i, d in enumerate(days)]
    good = [row(net=400 + (i % 4) * 20, day=str(d)) for i, d in enumerate(days)]
    a = G.assess_promotion(champ, good, days, POL)
    assert a["decision"] == "PROMOTE", a["unmet"]
    bad = [row(net=-300, day=str(d)) for d in days]
    assert G.assess_promotion(champ, bad, days * 2, POL)["decision"] == "REJECT"
    flagged = G.assess_promotion(champ, good, days, POL, flags=["ledger_fail"])
    assert flagged["decision"] == "REJECT"                              # a ledger fault is disqualifying


def test_more_candidates_compared_demand_more_confidence():
    assert G.required_level(POL, 1) == pytest.approx(0.80)
    assert G.required_level(POL, 4) == pytest.approx(0.95)


def test_rollback_triggers():
    days = _days(6)
    champ = [row(net=-20000, day=str(d)) for d in days[:3]]
    rb = G.assess_rollback(champ, [], [str(d) for d in days[:3]], -5000, POL)
    assert rb["decision"] == "ROLLBACK" and rb["triggers"]
    calm = G.assess_rollback([row(net=100, day=str(d)) for d in days], [row(net=90, day=str(d)) for d in days],
                             [str(d) for d in days], -5000, POL)
    assert calm["decision"] == "KEEP"


# ------------------------------------------------------------------ atomic activation and pinning

def _store_with_open_trade(tmp_path):
    sc = F.bullish()
    data = sc.build()
    clock = ReplayClock(ist(sc.sessions[0], "10:30"))
    store = Store(str(tmp_path / "champ.db"))
    e = Engine(store, FixtureData(data), clock, F.demo_config(), env="fixture", modes=("intraday",))
    e.request_scan("intraday")
    e.run_until_idle()
    return store, e, clock


def test_job_lock_prevents_two_jobs(tmp_path):
    p = str(tmp_path / "x.lock")
    with DY.job_lock(p):
        with pytest.raises(RuntimeError):
            with DY.job_lock(p):
                pass


# ------------------------------------------------------------------ the daily job (fixture sessions)

@pytest.fixture
def daily_env(tmp_path, monkeypatch):
    for k, sub in (("EVAL", "eval"), ("SHADOW", "shadow"), ("CONFIGS", "configs")):
        monkeypatch.setattr(DY, k, str(tmp_path / sub))
    monkeypatch.setenv("SF_LIBRARY", str(tmp_path / "library.db"))
    import sf_library as L
    monkeypatch.setattr(L, "DB", str(tmp_path / "library.db"))
    monkeypatch.setattr(DY, "AUDIT", str(tmp_path / "eval" / "audit.jsonl"))
    monkeypatch.setattr(DY, "alert", lambda text: None)
    monkeypatch.setattr(AU, "data_quality", lambda day: ({"EQ": {"zero_volume_share": 0.03,
                                                                  "first_bar_open_at_extreme_share": 0.0}}, []))
    state = {"feeds": True}
    monkeypatch.setattr(DY, "feeds_complete", lambda day, pol, root=None: (state["feeds"], [] if state["feeds"]
                                                                              else ["EQ: 120/370 minute parts"]))
    sc = F.bullish()
    data = sc.build()
    day = sc.sessions[0]
    clock = ReplayClock(ist(day, "09:15"))
    db = str(tmp_path / "champ.db")
    store = Store(db)
    e = Engine(store, FixtureData(data), clock, F.demo_config(), env="fixture", modes=("intraday",))
    from sf_cli import replay
    replay(e, clock, [day])
    return {"db": db, "day": day, "store": store, "state": state, "tmp": tmp_path}


def test_daily_job_no_change_decision_and_idempotence(daily_env):
    env = daily_env
    now = ist(env["day"], "16:10")
    rec, code = DY.run_day(env["day"], champion_db=env["db"], now=now)
    assert code == 0 and rec["status"] == "DONE"
    assert rec["decision"].startswith("CHECKED")                       # no strategy change without approval
    assert all(f["status"] == "OK" for f in rec["steps"]["reconcile"]["champion"])
    assert all(c["status"] == "OK" for c in rec["steps"]["compliance"])
    again, code2 = DY.run_day(env["day"], champion_db=env["db"], now=now)
    assert code2 == 0 and again["finished_at"] == rec["finished_at"]  # second run is a no-op
    assert os.path.exists(os.path.join(DY.EVAL, "latest.json"))


def test_daily_job_defers_on_late_feeds_then_recovers(daily_env):
    env = daily_env
    env["state"]["feeds"] = False
    rec, code = DY.run_day(env["day"], champion_db=env["db"], now=ist(env["day"], "16:10"))
    assert code == DY.DEFERRED and rec["status"] == "DEFERRED" and rec["attempts"] == 1
    rec, code = DY.run_day(env["day"], champion_db=env["db"], now=ist(env["day"], "16:30"))
    assert rec["attempts"] == 2
    env["state"]["feeds"] = True                                      # data arrived: the retry completes
    rec, code = DY.run_day(env["day"], champion_db=env["db"], now=ist(env["day"], "16:50"))
    assert code == 0 and rec["status"] == "DONE"


def test_daily_job_skips_holidays(daily_env):
    env = daily_env
    rec, code = DY.run_day(date(2026, 11, 24), champion_db=env["db"], now=ist(date(2026, 11, 24), "16:10"))
    assert code == 0 and rec["status"] == "SKIPPED"


# ------------------------------------------------------------------ audit and reproducibility

def test_audit_ledger_recomputes_fixture_trades(tmp_path):
    store, e, clock = _store_with_open_trade(tmp_path)
    from sf_cli import replay
    replay(e, clock, [clock.now().date()])
    with AU.ro(str(tmp_path / "champ.db")) as c:
        rows, findings = AU.ledger(c)
    assert rows and all(f["status"] == "OK" for f in findings), findings


def test_experiment_runs_are_reproducible(tmp_path, monkeypatch):
    import sf_experiments as X
    sc = F.bullish()
    data = sc.build()
    monkeypatch.setattr(X, "REGISTRY", str(tmp_path / "reg.jsonl"))

    def fake_replay(cfg, sessions, db, modes=("intraday", "swing"), root=None):
        clock = ReplayClock(ist(sessions[0], "09:15"))
        store = Store(db)
        e = Engine(store, FixtureData(data), clock, cfg, env="replay", modes=("intraday",))
        from sf_cli import replay
        import sf_api
        replay(e, clock, sessions)
        return sf_api.journal(store), {}, []
    monkeypatch.setattr(X, "replay", fake_replay)
    a = X.run("a", F.demo_config(), sc.sessions, "repro", db_dir=str(tmp_path / "a"))
    b = X.run("b", F.demo_config(), sc.sessions, "repro", db_dir=str(tmp_path / "b"))
    assert a["status"] == b["status"] == "DONE"
    assert a["metrics"] == b["metrics"] and a["config_hash"] == b["config_hash"] == config_hash(F.demo_config())
    assert len(X.registry(str(tmp_path / "reg.jsonl"))) == 2           # every run is kept


def test_daily_job_never_activates_and_counts_the_research_cadence(daily_env):
    """FIXTURE: the daily job checks and reports; a strategy change needs approval."""
    import sf_library as L
    env = daily_env
    store = env["store"]
    before = (store.config()[0], store.control("active_strategies"))
    rec, _ = DY.run_day(env["day"], champion_db=env["db"], now=ist(env["day"], "16:10"))
    assert (store.config()[0], store.control("active_strategies")) == before
    assert rec["steps"]["research"]["cadence"]["since_last_cycle"] == 1
    assert rec["steps"]["promotion"]["policy"].startswith("every activation needs your approval")


def test_daily_job_pauses_entries_on_a_ledger_failure(daily_env, monkeypatch):
    """FIXTURE: an operational failure pauses NEW entries automatically (exits continue)."""
    env = daily_env
    monkeypatch.setattr(AU, "ledger", lambda c: ([], [{"check": "over-close", "status": "FAIL", "detail": "fixture"}]))
    rec, _ = DY.run_day(env["day"], champion_db=env["db"], now=ist(env["day"], "16:10"))
    assert rec["steps"]["promotion"]["auto_pause"] and env["store"].control("pause:global") == "1"
    assert rec["decision"].startswith("ENTRIES PAUSED")
    assert env["store"].events("type='EntriesPausedAutomatically'")

"""Strategy library, research agent, approvals and the shared evaluator.

All evidence here is FIXTURE evidence (sf_fixtures synthetic sessions, the
research fixture file, fake replay results); no network request is made.
Numbers refer to the acceptance criteria in the feature brief (21 Nov 2026).
"""
import copy
import json
import os
from datetime import date, datetime, timedelta

import pytest

import sf_approval as AP
import sf_fixtures as F
import sf_indicators as I
import sf_library as L
import sf_research as RS
import sf_strategy as SS
from sf_core import IST, ReplayClock, ist
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store

POL = L.load_policy()
BASE = F.demo_config()
TPL, DIFFS = SS.templates_from_config(BASE)
INTRA, SWING = TPL


@pytest.fixture
def lib(tmp_path):
    return L.Library(str(tmp_path / "library.db"))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "trading.db"))
    s.put_config(BASE, datetime.now(IST), "test")
    return s


def bars(closes, day=date(2026, 11, 17), vol=100, start="09:15"):
    t0 = ist(day, start)
    out = []
    for i, c in enumerate(closes):
        t = t0 + timedelta(minutes=i)
        out.append({"ts": t.isoformat(), "available_at": (t + timedelta(minutes=1)).isoformat(), "open": c,
                    "high": c + 0.5, "low": c - 0.5, "close": c, "volume": vol, "turnover": c * vol})
    return out


# ------------------------------------------------------------------ evaluator semantics (11, 12)

def test_crossover_and_missing_values_never_pass():
    s = {"left": {"ind": "price", "params": {"field": "close"}}, "op": "cross_above", "right": {"value": 101}}
    ser = SS.Series(bars([100, 100.5, 101.5, 102]))
    assert SS.eval_node(s, ser, 0) == "UNAVAILABLE"          # no previous bar
    assert SS.eval_node(s, ser, 2) == "PASS"                 # 100.5 <= 101 then 101.5 > 101
    assert SS.eval_node(s, ser, 3) == "FAIL"                 # already above: not a cross
    sma = {"left": {"ind": "sma", "params": {"period": 10}}, "op": ">", "right": {"value": 0}}
    assert SS.eval_node(sma, ser, 3) == "UNAVAILABLE"        # warm-up: missing never passes
    grp = {"any": [sma, {"left": {"ind": "price"}, "op": ">", "right": {"value": 50}}]}
    assert SS.eval_node(grp, ser, 3) == "PASS"


def test_indicator_conventions():
    xs = [float(x) for x in range(1, 31)]
    assert I.sma_list(xs, 5)[4] == 3.0 and I.sma_list(xs, 5)[3] is None
    e = I.ema_list(xs, 5)
    assert e[4] == 3.0 and e[5] == pytest.approx(3.0 + (6 - 3.0) / 3)
    b = bars([100] * 14 + [101, 102])
    assert I.compute("rsi", b, {"period": 14})[-1] == 100.0
    vw = I.compute("vwap", bars([100, 102]))
    assert vw[-1] == pytest.approx(101.0)
    assert len(I.REGISTRY) >= 25


def test_resample_uses_complete_buckets_only():
    b = bars([100 + i for i in range(12)])
    r = SS.resample(b, 5)
    assert len(r) == 2 and r[0]["close"] == 104 and r[1]["open"] == 105     # the 2-minute tail is dropped


def test_validation_messages_are_actionable():
    bad = copy.deepcopy(INTRA)
    bad["entry"]["LONG"]["all"][0]["left"] = {"ind": "nope"}
    v = SS.validate(bad)
    assert not v["ok"] and any("unknown indicator 'nope'" in e for e in v["errors"])
    sw = copy.deepcopy(SWING)
    sw["entry"]["LONG"]["all"].append({"left": {"ind": "oi_change_pct"}, "op": ">", "right": {"value": 0}})
    v = SS.validate(sw, available=RS.available("swing"))
    assert not v["ok"] and any("data not available for: oi" in e for e in v["errors"])
    sw2 = copy.deepcopy(SWING)
    sw2["entry"]["LONG"]["all"].append({"left": {"ind": "or_high"}, "op": ">", "right": {"value": 0}})
    assert any("needs intraday bars" in e for e in SS.validate(sw2)["errors"])
    slow = copy.deepcopy(INTRA)
    slow["timeframe"] = "15m"
    slow["entry"]["LONG"]["all"].append({"left": {"ind": "sma", "params": {"period": 200}}, "op": ">", "right": {"value": 0}})
    assert any("warm-up" in w for w in SS.validate(slow)["warnings"])


def test_templates_match_the_deployed_rules_and_list_differences():
    assert INTRA["gates"]["market"] == BASE["profiles"]["intraday"]["regime"]
    assert SWING["gates"]["market"]["threshold"] == BASE["profiles"]["swing"]["regime"]["threshold"]
    assert SS.validate(INTRA)["ok"] and SS.validate(SWING)["ok"]
    assert DIFFS[INTRA["name"]] and "09:46" in DIFFS[INTRA["name"]][0]
    txt = SS.describe(INTRA).lower()
    assert "vwap" in txt and "opening-range high" in txt and "fibonacci" in txt


def test_builder_strategy_executes_through_the_same_evaluator(tmp_path):
    """11: the template, saved in the library, replays through the engine's
    'conditions' screener (the evaluator used by builder previews)."""
    sc = F.bullish()
    d = sc.sessions[0]
    lib = L.Library(str(tmp_path / "l.db"))
    sid, vid = lib.create(copy.deepcopy(INTRA), "builder", {"kind": "user"})
    cfg = SS.compile_config(lib.body(vid), BASE, {"strategy_id": sid, "version_id": vid})
    st = Store(str(tmp_path / "t.db"))
    clock = ReplayClock(ist(d, "09:15"))
    e = Engine(st, FixtureData(sc.build()), clock, cfg, env="fixture", modes=("intraday",))
    from sf_cli import replay
    replay(e, clock, [d])
    trs = [t["body"] for t in st.trades()]
    assert trs and all(t["strategy_ref"]["version_id"] == vid and t["lots"] == 1 for t in trs)
    first = trs[0]
    data = FixtureData(sc.build())
    at = datetime.fromisoformat(first["created_at"])
    b = [x for x in data.bars(first["underlying"], at, "1m")]
    bench = data.bars("NIFTY 50", at, "1m")
    assert SS.evaluate(lib.body(vid), "LONG", b, bench)["status"] == "PASS"   # same verdict as the engine


# ------------------------------------------------------------------ library lifecycle (13)

def test_versions_are_immutable_and_edits_of_active_become_drafts(lib):
    sid, v1 = lib.create(copy.deepcopy(INTRA), "u", {"kind": "user"})
    b = copy.deepcopy(INTRA)
    b["top_n"] = 3
    sid2, v2 = lib.edit(sid, b, "u")
    assert sid2 == sid and v2 != v1 and lib.body(v1)["top_n"] == INTRA["top_n"]
    with lib.tx() as c:
        lib._force_lifecycle(c, sid, "ACTIVE", "test")
    b["top_n"] = 4
    sid3, v3 = lib.edit(sid, b, "u")
    assert sid3 != sid and lib.strategy(sid3)["lifecycle"] == "DRAFT"           # a draft copy, not a live edit
    assert lib.strategy(sid)["head_version"] == v2
    with pytest.raises(PermissionError):
        lib.set_lifecycle(sid3, "ACTIVE", "u")                                  # only approved activation
    lib.set_lifecycle(sid3, "ARCHIVED", "u")
    with pytest.raises(ValueError):
        lib.edit(sid3, b, "u")
    nsid, nv = lib.restore_as_new(sid3, v3, "u")
    assert lib.strategy(nsid)["lifecycle"] == "DRAFT" and len(lib.versions(sid3)) == 2


def test_import_templates_is_idempotent(lib):
    a = L.import_templates(lib, BASE)
    b = L.import_templates(lib, BASE)
    assert [x[2] for x in a] == [True, True] and [x[2] for x in b] == [False, False]
    assert lib.strategy("tpl-intraday-v1")["lifecycle"] == "ACTIVE"


# ------------------------------------------------------------------ cadence and jobs (1, 2, 14)

def test_every_third_session_queues_research_exactly_once(lib):
    days = [date(2026, 11, d) for d in (12, 13, 16, 17, 18, 19)]
    jobs = [RS.on_session(lib, d, POL) for d in days]
    queued = [j for j in jobs if j]
    assert [j["idem_key"] for j in queued] == ["cycle:2026-11-16", "cycle:2026-11-19"]
    assert RS.on_session(lib, days[-1], POL) is None                            # same session again: not counted
    assert len(lib.jobs()) == 2
    assert lib.get("cadence")["next_cycle_in_sessions"] == 3


def test_research_now_never_duplicates_and_keeps_the_cadence(lib):
    RS.on_session(lib, date(2026, 11, 12), POL)
    before = lib.get("cadence")
    j1, c1 = RS.research_now(lib)
    j2, c2 = RS.research_now(lib)
    assert c1 and not c2 and j1["id"] == j2["id"]
    assert lib.get("cadence") == before
    claimed = lib.claim("research_cycle", "w1")
    assert lib.claim("research_cycle", "w2") is None                            # one running at a time
    lib.finish(claimed["id"], True, {})
    j3, c3 = RS.research_now(lib)
    assert c3 and j3["id"] != j1["id"]


def test_restart_recovers_stale_jobs_and_retries_failures(lib, tmp_path):
    j, _ = RS.research_now(lib)
    c = lib.claim("research_cycle", "dead-worker")
    with lib.tx() as cx:                                                        # the worker died an hour ago
        cx.execute("UPDATE jobs SET heartbeat=? WHERE id=?", ((datetime.now(IST) - timedelta(hours=1)).isoformat(), c["id"]))
    lib2 = L.Library(lib.path)                                                  # a new process
    again = lib2.claim("research_cycle", "new-worker")
    assert again["id"] == j["id"] and again["attempts"] == 2
    assert lib2.finish(again["id"], False, error="boom") == "QUEUED"            # retried
    lib2.claim("research_cycle", "w")
    assert lib2.finish(again["id"], False, error="boom") == "FAILED"            # bounded


# ------------------------------------------------------------------ research cycle (3, 4, 5, 8)

def fake_runner(trades_per_session=2, net=500.0):
    def run(name, cfg, sessions, purpose, window="research"):
        rows = []
        for s in sessions:
            for k in range(trades_per_session):
                rows.append({"mode": cfg["strategy_ref"]["mode"], "direction": "LONG", "class": "settled",
                             "net_pnl": net if k % 2 == 0 else -net / 2, "gross_pnl": net, "costs": 50.0,
                             "opened_at": f"{s}T10:00:00+05:30", "closed_at": f"{s}T15:15:00+05:30",
                             "entry_avg": 100.0, "closed_units": 100, "trade_id": f"{name}-{s}-{k}"})
        import sf_metrics as M
        return {"name": name, "status": "DONE", "trades": rows, "metrics": M.by_mode_direction(rows),
                "stressed_metrics": M.by_mode_direction(M.stressed(rows)), "config_hash": "fx",
                "data": {"data_sha256": "fixture"}, "code": {"code_sha256": "fixture"}, "costs": cfg["shared"]["costs"],
                "fill_policy": cfg["shared"]["fill_policy"], "recorded_at": "2026-11-21T10:00:00+0530"}
    return run


@pytest.fixture
def cycle_env(lib, store, monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "complete_sessions", lambda mode, root=None: [
        {"session": s, "complete": True, "parts": {}} for s in ("2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20")])
    monkeypatch.setattr(RS, "SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("SF_SEARCH_PROVIDER", "fixture")
    sent = []
    return {"lib": lib, "store": store, "sent": sent}


def test_cycle_turns_sourced_ideas_into_strategies_or_documented_rejections(cycle_env):
    lib, store = cycle_env["lib"], cycle_env["store"]
    job, _ = RS.research_now(lib)
    job = lib.claim("research_cycle", "w")
    out = RS.run_cycle(lib, job, base_cfg=BASE, store=store, runner=fake_runner(), notify=cycle_env["sent"].append,
                       policy=POL)
    assert out["discovery"]["fixture"] is True and out["discovery"]["ideas"] == 3
    names = {c["name"]: c for c in out["candidates"]}
    vwap = names["FIXTURE VWAP pullback continuation (intraday)"]
    assert vwap.get("version_id") and lib.strategy(vwap["strategy_id"])["lifecycle"] == "SHADOW"   # 4: new -> shadow
    tuned = [c for c in out["candidates"] if c.get("kind") == "tuned" and c.get("version_id")]
    assert tuned and lib.strategy(tuned[0]["strategy_id"])["lifecycle"] == "SHADOW"              # 4: tuned -> shadow
    nofilter = names["FIXTURE breakout without any index filter"]
    assert nofilter.get("rejected") and "requires a market/sector gate" in nofilter["rejected"][-1]  # 3: documented
    props = [s for s in lib.suggestions() if s["kind"] == "proposal"]
    assert props and props[0]["evidence"]["proposal_only"] and props[0]["lock_changes"][0]["lock"] == "market"
    src = lib.sources()
    assert any(s["url"] == "https://example.org/fixture/vwap-pullback" and s["fixture"] == 1 for s in src)
    sg = lib.q1("SELECT * FROM suggestions WHERE version_id=?", vwap["version_id"])
    ev = json.loads(sg["evidence"])                                                              # 5: evidence shown
    assert ev["recent"]["sessions"] == ["2026-11-18", "2026-11-19", "2026-11-20"]
    assert ev["replay"]["metrics"]["portfolio"]["settled"] == 8 and ev["baseline"]["metrics"]
    assert ev["provenance"]["source_url"].startswith("https://example.org/fixture") and ev["diff"]
    assert ev["forward"]["sessions"] == 0 and ev["label"] in ("EARLY", "INSUFFICIENT")
    assert "no chronological train/validation/test split" in ev["limitations"][0]
    assert any("FIXTURE" not in m and "suggestion" in m for m in cycle_env["sent"])


def test_duplicates_are_detected_by_structure(cycle_env):
    lib, store = cycle_env["lib"], cycle_env["store"]
    for _ in range(2):
        job, _ = RS.research_now(lib)
        job = lib.claim("research_cycle", "w")
        out = RS.run_cycle(lib, job, base_cfg=BASE, store=store, runner=fake_runner(), policy=POL)
        lib.finish(job["id"], True, out)
    assert any(c.get("duplicate_of") for c in out["candidates"])


def test_dismissal_keeps_research_running_and_suppresses_repeat_notifications(cycle_env):
    lib, store = cycle_env["lib"], cycle_env["store"]
    job, _ = RS.research_now(lib)
    job = lib.claim("research_cycle", "w")
    RS.run_cycle(lib, job, base_cfg=BASE, store=store, runner=fake_runner(), policy=POL)
    lib.finish(job["id"], True, {})
    sg = [s for s in lib.suggestions("OPEN") if s["kind"] == "new"][0]
    lib.decide(sg["id"], "DISMISSED", "user", "not my style", "never")
    _, notify = lib.upsert_suggestion(sg["strategy_id"], sg["version_id"], sg["kind"], sg["title"],
                                      dict(sg["evidence"], summary_key={"new": 1}), [], "c2")
    assert notify is False and lib.suggestion(sg["id"])["status"] == "DISMISSED"
    j2, created = RS.research_now(lib)                                                           # 8: still researching
    assert created


# ------------------------------------------------------------------ approval and activation (6, 7, 9, 10, 13)

def _suggest(lib, body, kind="tuned"):
    sid, vid = lib.create(body, "agent", {"kind": "agent"})
    pol_active = INTRA if body["mode"] == "intraday" else SWING
    lc = SS.lock_diff(body, pol_active, POL)
    sg, _ = lib.upsert_suggestion(sid, vid, kind, body["name"], {"label": "EARLY", "summary_key": {"v": vid}}, lc, "c1")
    return sid, vid, lib.suggestion(sg)


def test_nothing_activates_without_an_approval_of_that_exact_version(lib, store):
    b = copy.deepcopy(INTRA)
    b["name"], b["top_n"] = "candidate", 3
    sid, vid, sg = _suggest(lib, b)
    with pytest.raises(AP.ActivationRefused):
        AP.activate(lib, store, "ap-missing", BASE, datetime.now(IST), "x")
    with pytest.raises(AP.ActivationRefused):                                                     # stale evidence
        AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "user", "not-the-hash")
    aid = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "user", sg["evidence_hash"])
    lib.upsert_suggestion(sid, vid, "tuned", "candidate", {"label": "EARLY", "summary_key": {"more": 1}}, [], "c2")
    with pytest.raises(AP.ActivationRefused, match="approve again"):                              # evidence moved on
        AP.activate(lib, store, aid, BASE, datetime.now(IST), "user")
    assert store.control("active_strategies") is None


def test_replace_alongside_and_idempotent_atomic_activation(lib, store):
    b = copy.deepcopy(INTRA)
    b["name"], b["top_n"] = "candidate A", 3
    sid, vid, sg = _suggest(lib, b)
    aid = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "user", sg["evidence_hash"])
    res = AP.activate(lib, store, aid, BASE, datetime.now(IST), "user")
    act = json.loads(store.control("active_strategies"))
    intr = [a for a in act if a["mode"] == "intraday"]
    assert len(intr) == 1 and intr[0]["version_id"] == vid                                        # replaced
    assert any(a["mode"] == "swing" and a.get("legacy") for a in act)                             # swing untouched
    assert AP.activate(lib, store, aid, BASE, datetime.now(IST), "user")["idempotent"]
    assert lib.strategy(sid)["lifecycle"] == "ACTIVE" and lib.suggestion(sg["id"])["status"] == "REPLACED"
    c = copy.deepcopy(INTRA)
    c["name"], c["top_n"] = "candidate B", 2
    sid2, vid2, sg2 = _suggest(lib, c)
    aid2 = AP.record_approval(lib, sg2["id"], "ALONGSIDE", "intraday", "user", sg2["evidence_hash"])
    AP.activate(lib, store, aid2, BASE, datetime.now(IST), "user")
    act = [a for a in json.loads(store.control("active_strategies")) if a["mode"] == "intraday"]
    assert [a["version_id"] for a in act] == [vid, vid2] and act[0]["priority"] < act[1]["priority"]
    ev = [e for e in store.events("type='StrategyActivated'")]
    assert len(ev) == 2 and ev[0]["payload"]["before"] and ev[0]["payload"]["after"]               # auditable


def test_continue_and_dismiss(lib):
    b = copy.deepcopy(INTRA)
    b["name"], b["top_n"] = "c", 2
    sid, vid, sg = _suggest(lib, b)
    lib.decide(sg["id"], "CONTINUE", "user")
    assert lib.suggestion(sg["id"])["status"] == "CONTINUE"
    lib.decide(sg["id"], "DISMISSED", "user", "too few trades", "on_new_evidence")
    _, notify = lib.upsert_suggestion(sid, vid, "tuned", "c", {"label": "EARLY", "summary_key": {"x": 2}}, [], "c3")
    assert notify and lib.suggestion(sg["id"])["status"] == "OPEN"                                 # new evidence reopens


def test_locked_gate_change_needs_an_explicit_lock_approval(lib, store):
    b = copy.deepcopy(INTRA)
    b["name"] = "wider fib"
    b["gates"]["market"]["fib"]["high"] = 0.5
    sid, vid, sg = _suggest(lib, b, "proposal")
    assert sg["lock_changes"][0]["lock"] == "market"
    assert sg["lock_changes"][0]["paths"][0]["path"] == "gates.market.fib.high"
    aid = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "user", sg["evidence_hash"])
    with pytest.raises(AP.ActivationRefused, match="market"):
        AP.activate(lib, store, aid, BASE, datetime.now(IST), "user")
    aid2 = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "user", sg["evidence_hash"],
                              lock_changes_approved=["market"])
    assert AP.activate(lib, store, aid2, BASE, datetime.now(IST), "user")["applied"]


def test_one_lot_and_shared_limits_hold_across_competing_strategies(lib, tmp_path):
    """10: two active strategies with the same rules compete for the same stocks:
    one position per underlying, one lot each, capacity rejections recorded."""
    sc = F.bullish()
    d = sc.sessions[0]
    st = Store(str(tmp_path / "t.db"))
    base = copy.deepcopy(BASE)
    base["shared"]["max_open_positions"] = 2
    st.put_config(base, ist(d, "09:00"), "t")
    ids = []
    for name, top in (("first", 5), ("second", 4)):
        b = copy.deepcopy(INTRA)
        b["name"], b["top_n"] = name, top
        sid, vid, sg = _suggest(lib, b)
        aid = AP.record_approval(lib, sg["id"], "REPLACE" if name == "first" else "ALONGSIDE", "intraday", "u",
                                 sg["evidence_hash"])
        AP.activate(lib, st, aid, base, ist(d, "09:00"), "u")
        ids.append(vid)
    clock = ReplayClock(ist(d, "09:15"))
    e = Engine(st, FixtureData(sc.build()), clock, None, env="fixture", modes=("intraday",))
    from sf_cli import replay
    replay(e, clock, [d])
    trs = [t["body"] for t in st.trades() if t["body"].get("opened_at")]
    unders = [t["underlying"] for t in trs]
    assert len(unders) == len(set(unders))                                                        # no stacking
    assert all(t["lots"] == 1 and t["entry_filled_qty"] == t["lot_size"] for t in trs)
    assert len({t["opened_at"][:16] for t in trs}) and max(
        sum(1 for t in trs if t["opened_at"] <= x["opened_at"] and (t.get("closed_at") or "9") > x["opened_at"])
        for x in trs) <= 2                                                                         # shared cap of 2
    rej = [x["payload"]["reason"] for x in st.events("type='CandidateRejected'")]
    assert any("limit" in r or "already has an active" in r for r in rej)                         # recorded
    assert {t["strategy_ref"]["version_id"] for t in trs} <= set(ids)                             # attributed


def test_deactivation_keeps_open_positions_and_their_exit_plans(lib, tmp_path):
    sc = F.bullish()
    d = sc.sessions[0]
    st = Store(str(tmp_path / "t.db"))
    st.put_config(BASE, ist(d, "09:00"), "t")
    b = copy.deepcopy(INTRA)
    b["name"] = "to disable"
    sid, vid, sg = _suggest(lib, b)
    aid = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "u", sg["evidence_hash"])
    AP.activate(lib, st, aid, BASE, ist(d, "09:00"), "u")
    clock = ReplayClock(ist(d, "09:15"))
    e = Engine(st, FixtureData(sc.build()), clock, None, env="fixture", modes=("intraday",))
    while clock.now() < ist(d, "11:00"):
        e.tick(clock.now())
        clock.advance(minutes=1)
    open_ = [t["body"] for t in st.trades("status='OPEN'")]
    assert open_
    plan = open_[0]["exit_plan"]
    AP.deactivate(lib, st, sid, clock.now(), "u")
    assert lib.strategy(sid)["lifecycle"] == "DISABLED"
    while clock.now() <= ist(d, "15:30"):
        e.tick(clock.now())
        clock.advance(minutes=1)
    t = st.trade(open_[0]["trade_id"])["body"]
    assert t["status"] == "CLOSED" and t["exit_plan"]["target_price"] == plan["target_price"]      # managed to exit
    later = [x for x in st.trades() if x["body"]["created_at"] > iso_(ist(d, "11:01"))]
    assert not later                                                                               # no new entries
    assert lib.versions(sid) and lib.events(strategy_id=sid)                                       # history kept


def iso_(t):
    return t.isoformat()


def test_approvals_and_activations_survive_a_restart(lib, store):
    b = copy.deepcopy(INTRA)
    b["name"], b["top_n"] = "persist", 2
    sid, vid, sg = _suggest(lib, b)
    aid = AP.record_approval(lib, sg["id"], "REPLACE", "intraday", "u", sg["evidence_hash"])
    AP.activate(lib, store, aid, BASE, datetime.now(IST), "u")
    lib2 = L.Library(lib.path)
    st2 = Store(store.path)
    assert lib2.q1("SELECT * FROM approvals WHERE id=?", aid)["used_at"]
    assert lib2.q1("SELECT * FROM activations WHERE approval_id=?", aid)
    assert any(a.get("version_id") == vid for a in json.loads(st2.control("active_strategies")))
    assert AP.activate(lib2, st2, aid, BASE, datetime.now(IST), "u")["idempotent"]


def test_hard_constraints_cannot_come_from_a_strategy(lib, store):
    b = copy.deepcopy(INTRA)
    b["name"] = "sneaky"
    b["shared"] = {"max_open_positions": 99}                     # not part of the schema: ignored by compile
    cfg = SS.compile_config(b, BASE)
    assert cfg["shared"] == BASE["shared"]
    assert all(p.get("lots", 1) == 1 for p in [cfg])


def test_untrusted_idea_fields_are_data_only():
    """Retrieved text never becomes code: unknown indicators and parameters are dropped or rejected by validation."""
    idea = json.load(open(RS.FIXTURE))["ideas"][0]
    idea = copy.deepcopy(idea)
    idea["long_conditions"][0]["left_indicator"] = "__import__('os').system('rm -rf /')"
    body, notes, locks = RS.compile_idea(idea, INTRA)
    v = SS.validate(body)
    assert not v["ok"] and any("unknown indicator" in e for e in v["errors"])


def test_provider_status_is_explicit(monkeypatch):
    monkeypatch.setenv("SF_SEARCH_PROVIDER", "none")
    st = RS.provider_status(POL)
    assert st["search"]["configured"] is False and "not configured" in st["search"]["detail"]
    monkeypatch.setenv("SF_SEARCH_PROVIDER", "fixture")
    assert RS.provider_status(POL)["search"]["fixture"] is True


def test_research_waits_for_the_close_on_trading_days():
    from sf_core import ist as _ist
    assert RS.market_hours_block(_ist(date(2026, 11, 20), "11:00"), POL) is True      # Friday session
    assert RS.market_hours_block(_ist(date(2026, 11, 20), "16:00"), POL) is False
    assert RS.market_hours_block(_ist(date(2026, 11, 21), "11:00"), POL) is False     # Saturday


def test_published_snapshot_has_only_firebase_safe_keys():
    import sf_api
    bad = {"a.b": {"c$d": [{"e/f": 1, "g#h": 2, "": 3}]}, "ok": {"x[y]": 4}}
    out = sf_api.firebase_safe(bad)

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert all(k and not any(ch in k for ch in ".$#[]/") for k in keys(out))
    assert out["a·b"]["c_d"][0]["e_f"] == 1


def test_a_strategy_replays_only_its_own_mode(tmp_path, monkeypatch):
    """A swing strategy's replay must not count the intraday profile's trades (21 Nov 2026 bug)."""
    import sf_experiments as X
    seen = {}

    def fake_replay(cfg, sessions, db, modes=("intraday", "swing"), root=None):
        seen["modes"] = modes
        return [{"mode": "intraday", "direction": "LONG", "class": "settled", "net_pnl": 999.0,
                 "opened_at": "2026-11-19T10:00:00+05:30", "closed_at": "2026-11-19T15:15:00+05:30"}], {}, []
    monkeypatch.setattr(X, "replay", fake_replay)
    monkeypatch.setattr(X, "REGISTRY", str(tmp_path / "r.jsonl"))
    cfg = SS.compile_config(SWING, BASE, {"strategy_id": "s", "version_id": "v"})
    e = X.run("swing-only", cfg, [date(2026, 11, 19)], "test", db_dir=str(tmp_path))
    assert seen["modes"] == ("swing",) and e["metrics"]["portfolio"]["settled"] == 0
    assert cfg["profiles"]["intraday"]["paused"] is True


def test_engine_skips_a_paused_profile(tmp_path):
    sc = F.bullish()
    d = sc.sessions[0]
    cfg = SS.compile_config(SWING, BASE)                     # intraday profile paused in this config
    st = Store(str(tmp_path / "t.db"))
    e = Engine(st, FixtureData(sc.build()), ReplayClock(ist(d, "10:30")), cfg, env="fixture",
               modes=("intraday", "swing"))
    assert all(not x.startswith("intraday") for x in e.schedule(ist(d, "10:30")))

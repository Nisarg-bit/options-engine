"""Read model for the dashboard, operator controls, and the FastAPI router.

Public:   GET  /api/futures/state             everything the Stock Futures page draws
Operator: POST /api/futures/ops/pause         {"scope": "global"|"intraday"|"swing", "paused": bool}
          POST /api/futures/ops/approval      {"approval_id", "decision": "APPROVE"|"REJECT"}
          POST /api/futures/ops/override      {"trade_id", "edits": {"stop_price"|"target_price"|"max_holding_sessions": v}}
          POST /api/futures/ops/config        a full config document -> new version (future trades only)

Operator routes need the service's existing credentials (DASH_USER/DASH_PASS
Basic auth, or DASH_TOKEN) to be CONFIGURED and supplied. With neither set the
dashboard is public and these routes refuse (403): public views stay read-only.
The Firebase-hosted page only ever reads /live/futures; it cannot write.
"""
import base64
import json
import os
import secrets
from datetime import date, datetime, timedelta

from sf_core import LONG, SHORT, MODES, iso, ist, parse_ts, validate_config, IST, gross_pnl, config_hash

try:                        # repo layout and the server's flat folder both have project_paths at the top
    import project_paths
    _ROOT = project_paths.ROOT
except ImportError:
    _ROOT = os.environ.get("OPTIONS_ENGINE_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DEFAULT = os.path.join(_ROOT, "data", "futures", "futures.db")


# ====================================================================== view

def _brief(ev):
    p = ev["payload"]
    t = ev["type"]
    if t == "RegimeClassified":
        return f"{p['regime']} -> permitted {', '.join(p['permitted']) or 'none'}"
    if t == "SectorsRanked":
        return "; ".join(f"{d}: " + (", ".join(f"{q['rank']}.{q['sector']} {q['return_pct']:+.2f}% (rel {q['relative_pct']:+.2f})"
                                               for q in v["qualifying"]) or "none qualify")
                         for d, v in p["by_direction"].items())
    if t == "CandidatesRanked":
        return ", ".join(f"{c['rank'] + 1}.{c['symbol']} {c['direction']} {c['score']:+.2f}" for c in p["candidates"]) or "no candidates"
    if t == "VerificationResult":
        return f"{p['symbol']} {p['status']}: {'; '.join(p.get('reasons', []))}"
    if t == "CandidateVerified":
        return f"{p['candidate']['symbol']} verified"
    if t == "CandidateRejected":
        return f"{p['symbol']}: {p['reason']}"
    if t in ("EntryOrderSubmitted", "ExitOrderSubmitted"):
        return f"{p['side']} {p.get('units')} units ({p['purpose']}) {p.get('tradingsymbol', '')} {p.get('reason', '')}".strip()
    if t == "OrderFilled":
        return f"{p['side']} {p['qty']} @ {p['price']} ({p['purpose']}, {p['evidence'].get('model')})"
    if t == "FillBlocked":
        return f"{p['side']} {p['purpose']} not filled: {p['reason']}"
    if t == "ExitIntent":
        return f"exit: {p['primary']} (also: {', '.join(x for x in p['triggered'] if x != p['primary']) or '-'})"
    if t == "PositionClosed":
        r = p.get("result") or {}
        return f"{p['status']} {p['reason']}: net Rs {r.get('net_pnl')}" if r else f"{p['status']} {p['reason']}"
    if t == "ScanStopped":
        return p["reason"]
    if t in ("OverrideApplied", "OverrideRejected"):
        return p.get("reason") or json.dumps(p.get("edits"))
    if t == "ExitUnresolved":
        return p["reason"]
    return ""


def timeline(store, scan_id=None, trade_id=None, limit=200):
    if trade_id:
        t = store.trade(trade_id)
        scan_id = t["scan_id"] if t else scan_id
        evs = store.events("trade_id=? OR scan_id=?", trade_id, scan_id)
    else:
        evs = store.events("scan_id=?", scan_id)
    return [{"seq": e["seq"], "type": e["type"], "agent": e["producer"], "at": e["event_time"],
             "direction": e["direction"], "instrument_id": e["instrument_id"], "summary": _brief(e),
             "evidence": e["payload"]} for e in evs[-limit:]]


def journal(store):
    rows = []
    for tr in store.trades():
        t = tr["body"]
        r = t.get("result") or {}
        exc = t.get("unresolved")
        if t["status"] == "CLOSED":
            cls = "excluded" if exc else "settled"
        elif t["status"] in ("EXITING", "UNRESOLVED") and (exc or t["status"] == "UNRESOLVED"):
            cls = "unresolved"
        elif t["status"] in ("CANCELED", "REJECTED"):
            cls = "excluded"
        else:
            cls = "open"
        rows.append({"trade_id": t["trade_id"], "mode": t["mode"], "direction": t["direction"],
                     "underlying": t["underlying"], "sector": t.get("sector"), "tradingsymbol": t.get("tradingsymbol"),
                     "instrument_id": t.get("instrument_id"), "expiry": t.get("expiry"), "lots": t.get("lots"),
                     "units": t.get("requested_qty"), "entry_side": r.get("entry_side"), "exit_side": r.get("exit_side"),
                     "status": t["status"], "class": cls, "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
                     "entry_avg": r.get("entry_avg"), "exit_avg": r.get("exit_avg"), "trigger_price": r.get("trigger_price"),
                     "closed_units": r.get("closed_units"), "gross_pnl": r.get("gross_pnl"), "costs": r.get("costs"),
                     "net_pnl": r.get("net_pnl"), "exit_reason": (t.get("exit") or {}).get("reason") or t.get("cancel_reason"),
                     "exception": exc and exc["reason"], "config_version": t["config_version"], "label": t.get("label")})
    return rows


def summary(rows):
    def agg(sel):
        s = [r for r in sel if r["class"] == "settled"]
        eq, peak, dd = 0.0, 0.0, 0.0
        for r in sorted(s, key=lambda r: r["closed_at"] or ""):
            eq += r["net_pnl"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        return {"settled": len(s), "excluded": sum(r["class"] == "excluded" for r in sel),
                "unresolved": sum(r["class"] == "unresolved" for r in sel), "open": sum(r["class"] == "open" for r in sel),
                "gross_pnl": round(sum(r["gross_pnl"] for r in s), 2), "costs": round(sum(r["costs"] for r in s), 2),
                "net_pnl": round(sum(r["net_pnl"] for r in s), 2), "wins": sum(r["net_pnl"] > 0 for r in s),
                "max_drawdown": round(dd, 2),
                "excluded_net_pnl": round(sum(r["net_pnl"] or 0 for r in sel if r["class"] == "excluded"), 2)}
    out = {"all": agg(rows)}
    for m in MODES:
        for d in (LONG, SHORT):
            out[f"{m}:{d}"] = agg([r for r in rows if r["mode"] == m and r["direction"] == d])
    return out


def state(store, now, data=None, engine=None):
    """The full read model. Pure read: safe to call from the publisher."""
    v, cfg = store.config()
    profiles = {}
    for m in MODES:
        p = (cfg or {}).get("profiles", {}).get(m, {})
        last = store.q1("SELECT * FROM scans WHERE mode=? ORDER BY started DESC LIMIT 1", m)
        scan = None
        if last:
            evs = {e["type"]: e for e in store.events("scan_id=?", last["scan_id"])}
            reg = evs.get("RegimeClassified")
            sec = evs.get("SectorsRanked")
            can = evs.get("CandidatesRanked")
            stop = evs.get("ScanStopped")
            ver = [e["payload"] for e in store.events("scan_id=? AND type='VerificationResult'", last["scan_id"])]
            rej = [e["payload"] for e in store.events("scan_id=? AND type='CandidateRejected'", last["scan_id"])]
            entered = store.trades("scan_id=?", last["scan_id"])
            scan = {"scan_id": last["scan_id"], "status": last["status"], "started": last["started"],
                    "config_version": last["config_version"],
                    "regime": reg and reg["payload"], "sectors": sec and sec["payload"],
                    "candidates": can and can["payload"], "verification": ver, "rejections": rej,
                    "contract": entered[-1]["body"].get("contract") if entered else None,
                    "trade_id": entered[-1]["trade_id"] if entered else None,
                    "no_trade_reason": stop and stop["payload"]["reason"],
                    "timeline": timeline(store, scan_id=last["scan_id"])}
        profiles[m] = {"paused": store.paused(m), "regime": store.control(f"regime:{m}"),
                       "benchmark": p.get("benchmark"), "cadence_s": p.get("scan_cadence_s"),
                       "entry_window": p.get("entry_window"), "exit_deadline": p.get("exit_deadline"),
                       "direction_map": p.get("direction_map"), "last_scan": scan}
    positions = []
    for tr in store.trades("status IN ('PENDING_APPROVAL','PENDING_ENTRY','OPEN','EXITING','UNRESOLVED')"):
        t = tr["body"]
        plan = t.get("exit_plan") or {}
        held = None
        if engine and t["mode"] == "swing" and t.get("entry_session"):
            held = engine.cal.sessions_between(date.fromisoformat(t["entry_session"]), now.date())
        overnight = bool(t.get("entry_session")) and t["entry_session"] != str(now.date())
        positions.append({"trade_id": t["trade_id"], "mode": t["mode"], "direction": t["direction"],
                          "underlying": t["underlying"], "tradingsymbol": t.get("tradingsymbol"),
                          "instrument_id": t.get("instrument_id"), "expiry": t.get("expiry"), "status": t["status"],
                          "lots": t.get("lots"), "units": t.get("requested_qty"),
                          "entry_filled_units": t.get("entry_filled_qty"), "entry_avg": t.get("entry_avg"),
                          "mark": t.get("mark"), "mark_side": t.get("mark_side"), "mtm": t.get("mtm"),
                          "stop_price": plan.get("stop_price"), "target_price": plan.get("target_price"),
                          "plan_version": len(t.get("plan_history", [])), "plan": plan,
                          "intraday_deadline": plan.get("intraday_deadline"),
                          "pre_expiry_deadline": plan.get("pre_expiry_deadline"),
                          "sessions_held": held, "max_holding_sessions": plan.get("max_holding_sessions"),
                          "overnight": overnight and t["mode"] == "swing", "data_flag": t.get("data_flag"),
                          "exit": t.get("exit"), "unresolved": t.get("unresolved"), "label": t.get("label"),
                          "timeline": timeline(store, trade_id=t["trade_id"])})
    orders = [dict(o["body"], status=o["status"], filled_qty=o["filled_qty"]) for o in store.orders()][-100:]
    fills = [f["body"] for f in store.fills()][-100:]
    approvals = store.q("SELECT * FROM approvals WHERE status='PENDING' ORDER BY deadline")
    rows = journal(store)
    health = None
    if data is not None:
        try:
            health = data.health(now)
        except Exception as e:  # noqa: BLE001
            health = {"error": f"{type(e).__name__}: {e}"}
    gate = engine.live_gate() if engine else None
    return {"generated": iso(now), "session": str(now.date()), "config_version": v,
            "config_label": (cfg or {}).get("label"), "rules_validated": (cfg or {}).get("rules_validated"),
            "label": engine.label if engine else "PAPER", "live_entry_gate": gate,
            "paused_global": store.control("pause:global") == "1",
            "profiles": profiles, "positions": positions, "orders": orders, "fills": fills,
            "approvals": approvals, "journal": rows, "summary": summary(rows), "feeds": health,
            "dead_letters": store.q("SELECT * FROM failures WHERE attempts>=5"),
            "daily_summary": (store.events("type='DailySummary'")[-1:] or [{"payload": None}])[0]["payload"],
            "notes": ["Paper trading and replay only. No order reaches a broker.",
                      "Equity spot prices select candidates; the futures contract's own quotes open, mark and close.",
                      "Money flow is an estimate from price x volume; order blocks are a chart pattern.",
                      "Not investment advice."]}


# ====================================================================== operator actions

def apply_override(store, engine, trade_id, edits, actor, now):
    """Validate and apply an exit-plan override atomically. Returns (ok, reason).

    Runs under the write lock: it re-reads the exit claim inside the same
    transaction, so an override can never land after (and undo) a claimed exit."""
    allowed_keys = {"stop_price", "target_price", "max_holding_sessions"}
    with store.tx() as c:
        tr = store.trade(trade_id)
        reason = None
        if not tr:
            reason = "unknown trade"
        else:
            t = tr["body"]
            a = c.execute("SELECT * FROM approvals WHERE trade_id=? AND kind='override'", (trade_id,)).fetchone()
            cfg = engine.cfg(t["config_version"])
            prof = cfg["profiles"][t["mode"]]
            allowed = set(json.loads(a["body"])["allowed"]) if a else set()
            names = {"stop": "stop_price", "target": "target_price", "max_holding_sessions": "max_holding_sessions"}
            permitted = {names[x] for x in allowed}
            plan = t.get("exit_plan") or {}
            if c.execute("SELECT 1 FROM exit_claims WHERE trade_id=?", (trade_id,)).fetchone():
                reason = "exit already claimed; an override cannot undo it"
            elif t["status"] != "OPEN":
                reason = f"trade is {t['status']}"
            elif not a or a["status"] not in ("PENDING", "APPLIED", "EXPIRED"):
                reason = "no override window for this trade"
            elif now > parse_ts(a["deadline"]):
                reason = f"late: response deadline {a['deadline']} has passed; default plan stays active"
            elif not edits or set(edits) - allowed_keys or set(edits) - permitted:
                reason = f"only {sorted(permitted)} may be edited"
            else:
                d, entry = t["direction"], t["entry_avg"]
                hard = plan.get("hard_max_stop_pct")
                mark = t.get("mark") or entry
                for k, val in edits.items():
                    if not isinstance(val, (int, float)) or val <= 0:
                        reason = f"{k} must be a positive number"
                    elif k == "stop_price":
                        if (d == LONG and val >= mark) or (d == SHORT and val <= mark):
                            reason = f"stop {val} is on the wrong side of the current mark {mark} for a {d}"
                        elif hard and abs(val - entry) / entry * 100 > hard:
                            reason = f"stop {val} is beyond the hard risk limit of {hard}% from entry {entry}"
                    elif k == "target_price":
                        if (d == LONG and val <= mark) or (d == SHORT and val >= mark):
                            reason = f"target {val} is on the wrong side of the current mark {mark} for a {d}"
                    elif k == "max_holding_sessions":
                        if t["mode"] != "swing" or int(val) != val:
                            reason = "max_holding_sessions applies to swing, as a whole number"
                        elif plan.get("pre_expiry_deadline"):
                            dl = parse_ts(plan["pre_expiry_deadline"]).date()
                            room = engine.cal.sessions_between(date.fromisoformat(t["entry_session"]), dl)
                            if val > room:
                                reason = f"{val} sessions runs past the pre-expiry deadline ({room} sessions available)"
                    if reason:
                        break
        if reason:
            store.audit(c, now, actor, "override", trade_id, "rejected", {"edits": edits, "reason": reason})
            if tr:
                t = tr["body"]
                store.emit(c, "OverrideRejected", "operator", now, {"edits": edits, "reason": reason, "actor": actor},
                           trade_id=trade_id, mode=t["mode"], direction=t["direction"], scan_id=t["scan_id"],
                           config_version=t["config_version"], instrument_id=t.get("instrument_id"))
            return False, reason
        new = dict(plan)
        for k, val in edits.items():
            new[k] = val
        if "stop_price" in edits:
            new["stop"] = {"basis": "price", "value": edits["stop_price"]}
        if "target_price" in edits:
            new["target"] = {"basis": "price", "value": edits["target_price"]}
        t["exit_plan"] = new
        t.setdefault("plan_history", []).append({"version": len(t["plan_history"]) + 1, "at": iso(now),
                                                 "by": actor, "edits": edits, "plan": new})
        store.put_trade(c, t, now)
        c.execute("UPDATE approvals SET status='APPLIED', decided=? WHERE approval_id=?", (iso(now), a["approval_id"]))
        store.audit(c, now, actor, "override", trade_id, "applied", {"edits": edits})
        store.emit(c, "OverrideApplied", "operator", now, {"edits": edits, "actor": actor,
                   "plan_version": len(t["plan_history"]), "plan": new},
                   trade_id=trade_id, mode=t["mode"], direction=t["direction"], scan_id=t["scan_id"],
                   config_version=t["config_version"], instrument_id=t.get("instrument_id"))
        return True, None


def decide_approval(store, approval_id, decision, actor, now):
    if decision not in ("APPROVE", "REJECT"):
        return False, "decision must be APPROVE or REJECT"
    a = store.q1("SELECT * FROM approvals WHERE approval_id=?", approval_id)
    if not a or a["kind"] != "entry":
        return False, "unknown entry approval"
    t = store.trade(a["trade_id"])["body"]
    with store.tx() as c:
        store.audit(c, now, actor, "entry-approval", a["trade_id"], decision, {"approval_id": approval_id})
        store.emit(c, "ApprovalDecided", "operator", now, {"approval_id": approval_id, "decision": decision,
                   "actor": actor}, trade_id=a["trade_id"], scan_id=a["scan_id"], mode=t["mode"],
                   direction=t["direction"], config_version=t["config_version"])
    return True, None


def set_pause(store, scope, paused, actor, now):
    if scope not in ("global",) + MODES:
        return False, "scope must be global, intraday or swing"
    store.set_control(f"pause:{scope}", "1" if paused else "0", now, actor)
    return True, None


def put_config(store, cfg, actor, now):
    errs = validate_config(cfg)
    if errs:
        return False, "; ".join(errs)
    v = store.put_config(cfg, now, actor)
    store.activate_config(v, now, actor)
    with store.tx() as c:
        store.audit(c, now, actor, "config", None, "applied", {"version": v})
    return True, v


# ====================================================================== router

def _operator(request):
    """Actor name if the request carries the service's configured credentials."""
    user, pw, token = (os.environ.get(k, "").strip() for k in ("DASH_USER", "DASH_PASS", "DASH_TOKEN"))
    if not pw and not token:
        return None, "operator controls are disabled: configure DASH_USER/DASH_PASS or DASH_TOKEN on the server"
    h = request.headers.get("authorization", "")
    if pw and h.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(h.split(None, 1)[1]).decode().partition(":")
        except Exception:  # noqa: BLE001
            u, p = "", ""
        if secrets.compare_digest(u, user) & secrets.compare_digest(p, pw):
            return f"basic:{u}", None
    sup = request.headers.get("x-dash-token", "")
    if token and sup and secrets.compare_digest(sup, token):
        return "token", None
    return None, "unauthorised"


def make_router(get_store=None, get_engine=None):
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    router = APIRouter()
    _cache = {}

    def store():
        if get_store:
            return get_store()
        if "s" not in _cache:
            from sf_store import Store
            os.makedirs(os.path.dirname(DB_DEFAULT), exist_ok=True)
            _cache["s"] = Store(os.environ.get("SF_DB", DB_DEFAULT))
        return _cache["s"]

    def engine():
        if get_engine:
            return get_engine()
        # Read/validate side of the engine for this API process (the worker runs
        # separately). Built once a config is stored; it never starts threads.
        if "e" not in _cache and store().config()[1] is not None:
            from sf_engine import Engine
            from sf_data import RepoData
            from sf_core import SystemClock
            root = _ROOT
            _cache["e"] = Engine(store(), RepoData(root), SystemClock(), None, env="paper")
        return _cache.get("e")

    def now():
        e = engine()
        return e.clock.now() if e else datetime.now(IST)

    @router.get("/api/futures/state")
    def get_state():
        if not get_store:                       # server: read the worker's snapshot, never the DB
            return read_snapshot()
        st = store()
        if st.config()[1] is None:
            return {"available": False, "reason": "stock-futures worker has not run yet (no config stored)"}
        e = engine()
        return state(st, now(), e.data if e else None, e)

    async def guarded(request, fn):
        actor, err = _operator(request)
        if not actor:
            return JSONResponse({"ok": False, "error": err}, status_code=403 if "disabled" in err else 401)
        body = await request.json()
        ok, res = fn(body, actor)
        return JSONResponse({"ok": ok, "result" if ok else "error": res}, status_code=200 if ok else 400)

    @router.post("/api/futures/ops/pause")
    async def pause(request: Request):
        return await guarded(request, lambda b, a: set_pause(store(), b.get("scope"), bool(b.get("paused")), a, now()))

    @router.post("/api/futures/ops/approval")
    async def approval(request: Request):
        return await guarded(request, lambda b, a: decide_approval(store(), b.get("approval_id"), b.get("decision"), a, now()))

    @router.post("/api/futures/ops/override")
    async def override(request: Request):
        def fn(b, a):
            e = engine()
            if not e:
                return False, "no engine attached to this API process"
            return apply_override(store(), e, b.get("trade_id"), b.get("edits") or {}, a, now())
        return await guarded(request, fn)

    @router.post("/api/futures/ops/config")
    async def config(request: Request):
        # Closed (21 Nov 2026): this stored a config AND made it active, bypassing
        # approval. Strategy changes go through suggestions -> approval -> activation.
        return JSONResponse({"ok": False, "error": "direct config activation is disabled: create or edit a strategy "
                             "in the library and activate it through an approval"}, status_code=410)

    # ------------------------------------------------------------ strategy library / research
    import sf_library as L
    import sf_strategy as SS

    def lib():
        if "lib" not in _cache:
            _cache["lib"] = L.Library()
        return _cache["lib"]

    def base_cfg():
        from sf_core import load_config
        return load_config()

    @router.get("/api/futures/research/overview")
    def research_overview():
        return research_state(lib(), store())

    @router.get("/api/futures/indicators")
    def indicators():
        import sf_indicators as I
        return {"indicators": I.catalog(), "data": I.DATA, "operators": list(SS.OPS),
                "timeframes": list(SS.TIMEFRAMES), "stop_bases": list(SS.STOP_BASES),
                "target_bases": list(SS.TARGET_BASES)}

    @router.get("/api/futures/strategies/{sid}")
    def strategy_detail(sid: str):
        return strategy_view(lib(), store(), sid)

    @router.get("/api/futures/versions/{vid}")
    def version_body(vid: str):
        b = lib().body(vid)
        return {"version_id": vid, "body": b, "description": SS.describe(b) if b else None}

    @router.get("/api/futures/diff")
    def version_diff(a: str, b: str):
        return {"a": a, "b": b, "diff": SS.diff(lib().body(a), lib().body(b))}

    @router.post("/api/futures/strategy/validate")
    async def validate_body(request: Request):
        import sf_research as RS
        b = await request.json()
        pol = L.load_policy()
        act = _active_body(lib(), store(), b.get("mode"))
        v = SS.validate(b, available=RS.available(b.get("mode")), policy=pol, active=act)
        return {"validation": v, "description": SS.describe(b) if b.get("entry") else None,
                "structure_hash": SS.structure_hash(b)}

    @router.post("/api/futures/strategy/preview")
    async def preview(request: Request):
        return await guarded(request, lambda b, a: (True, preview_markers(b.get("body"), b.get("symbol"), b.get("day"))))

    @router.post("/api/futures/strategies")
    async def create_strategy(request: Request):
        def fn(b, a):
            sid, vid = lib().create(b["body"], a, {"kind": "user", "via": b.get("via", "builder")})
            return True, {"strategy_id": sid, "version_id": vid}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/edit")
    async def edit_strategy(sid: str, request: Request):
        def fn(b, a):
            nsid, vid = lib().edit(sid, b["body"], a, b.get("note") or "edited in the builder")
            return True, {"strategy_id": nsid, "version_id": vid, "copied": nsid != sid}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/duplicate")
    async def duplicate_strategy(sid: str, request: Request):
        def fn(b, a):
            nsid, vid = lib().duplicate(sid, a, b.get("name"), b.get("version_id"))
            return True, {"strategy_id": nsid, "version_id": vid}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/rename")
    async def rename_strategy(sid: str, request: Request):
        def fn(b, a):
            nsid, vid = lib().rename(sid, b["name"], a)
            return True, {"strategy_id": nsid, "version_id": vid}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/lifecycle")
    async def lifecycle(sid: str, request: Request):
        def fn(b, a):
            to = b.get("to")
            st = lib().strategy(sid)
            if st and st["lifecycle"] == "ACTIVE" and to in ("DISABLED", "ARCHIVED"):
                import sf_approval as AP
                return True, AP.deactivate(lib(), store(), sid, now(), a, to)
            try:
                lib().set_lifecycle(sid, to, a, b.get("reason") or "")
            except (ValueError, PermissionError) as e:
                return False, str(e)
            return True, {"lifecycle": to}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/restore")
    async def restore(sid: str, request: Request):
        def fn(b, a):
            nsid, vid = lib().restore_as_new(sid, b["version_id"], a)
            return True, {"strategy_id": nsid, "version_id": vid}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/replay")
    async def replay_strategy(sid: str, request: Request):
        def fn(b, a):
            vid = b.get("version_id") or lib().strategy(sid)["head_version"]
            job, created = lib().enqueue("replay", f"replay:{vid}:{','.join(b.get('sessions') or [])}",
                                         {"strategy_id": sid, "version_id": vid, "sessions": b.get("sessions"),
                                          "actor": a})
            return True, {"job": job["id"], "created": created, "status": job["status"]}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/shadow")
    async def shadow_strategy(sid: str, request: Request):
        def fn(b, a):
            try:
                lib().set_lifecycle(sid, "SHADOW", a, "shadow test requested")
            except ValueError as e:
                return False, str(e)
            st = lib().strategy(sid)
            lib().upsert_suggestion(sid, st["head_version"], "user", st["name"],
                                    {"label": "INSUFFICIENT", "forward": {"sessions": 0, "settled": 0,
                                                                          "since": iso(now()), "frozen_at": iso(now())},
                                     "description": SS.describe(lib().body(st["head_version"])),
                                     "summary_key": {"shadow_started": iso(now())}}, [], "user")
            return True, {"lifecycle": "SHADOW"}
        return await guarded(request, fn)

    @router.post("/api/futures/strategies/{sid}/request-activation")
    async def request_activation(sid: str, request: Request):
        def fn(b, a):
            st = lib().strategy(sid)
            body = lib().body(st["head_version"])
            pol = L.load_policy()
            act = _active_body(lib(), store(), body["mode"])
            lc = SS.lock_diff(body, act, pol) if act else []
            sg, _ = lib().upsert_suggestion(sid, st["head_version"], "user", st["name"],
                                            {"label": st["evidence"], "description": SS.describe(body),
                                             "diff": SS.diff(act, body) if act else [], "lock_changes": lc,
                                             "summary_key": {"requested_by": a, "version": st["head_version"]}},
                                            lc, "user")
            return True, {"suggestion": sg}
        return await guarded(request, fn)

    @router.post("/api/futures/suggestions/{sgid}/decide")
    async def decide(sgid: str, request: Request):
        def fn(b, a):
            import sf_approval as AP
            import sf_research as RS
            act = (b.get("action") or "").upper()
            s = lib().suggestion(sgid)
            if s is None:
                return False, "no such suggestion"
            if act in ("CONTINUE", "DISMISS"):
                lib().decide(sgid, "CONTINUE" if act == "CONTINUE" else "DISMISSED", a, b.get("note"),
                             b.get("reconsider") or "on_new_evidence")
                return True, {"status": "CONTINUE" if act == "CONTINUE" else "DISMISSED"}
            if act == "EDIT_COPY":
                nsid, vid = lib().duplicate(s["strategy_id"], a, None, s["version_id"])
                return True, {"strategy_id": nsid, "version_id": vid}
            if act not in ("REPLACE", "ALONGSIDE"):
                return False, "action must be REPLACE, ALONGSIDE, CONTINUE, DISMISS or EDIT_COPY"
            try:
                body = lib().body(s["version_id"])
                aid = AP.record_approval(lib(), sgid, act, b.get("mode") or body["mode"], a,
                                         b.get("evidence_hash"), b.get("lock_changes_approved") or [],
                                         b.get("priority"), b.get("note"))
                res = AP.activate(lib(), store(), aid, base_cfg(), now(), a, RS.available(body["mode"]))
                return True, dict(res, approval=aid)
            except AP.ActivationRefused as e:
                return False, str(e)
        return await guarded(request, fn)

    @router.post("/api/futures/research/now")
    async def research_now_route(request: Request):
        def fn(b, a):
            import sf_research as RS
            job, created = RS.research_now(lib(), a)
            return True, {"job": job["id"], "created": created, "status": job["status"]}
        return await guarded(request, fn)

    return router


def _active_body(lib, store, mode):
    """The profile's current primary strategy as a strategy body (legacy v1 -> its template)."""
    import sf_approval as AP
    import sf_strategy as SS
    if not mode or store is None:
        return None
    act = AP.current_active(store) or AP.seed_legacy(store)
    p = AP.primary(act, mode)
    if not p:
        return None
    if p.get("version_id"):
        return lib.body(p["version_id"])
    cfg = store.config(p["config_version"])[1]
    return next((t for t in SS.templates_from_config(cfg)[0] if t["mode"] == mode), None) if cfg else None


def research_state(lib, store, limit=40):
    """Everything the Research / Library / Approvals views read (no strategy bodies)."""
    import sf_approval as AP
    import sf_library as L
    import sf_research as RS
    pol = L.load_policy()
    active = (AP.current_active(store) or AP.seed_legacy(store)) if store is not None and store.config()[1] else []
    sug = lib.suggestions()[:limit]
    for x in sug:
        ev = x["evidence"] or {}
        x["evidence"] = {k: ev.get(k) for k in ("label", "kind", "description", "diff", "lock_changes", "notes",
                                                "provenance", "recent", "replay", "forward", "baseline", "limitations",
                                                "data", "proposal_only", "lock_requests", "errors", "learned_components")}
    return {"available": True, "providers": RS.provider_status(pol), "policy": {k: pol[k] for k in (
                "locks", "lock_notes", "open_areas", "hard_constraints", "permissions", "cadence", "budgets", "evidence")},
            "cadence": lib.get("cadence"), "last_cycle": lib.get("last_cycle"), "jobs": lib.jobs(15),
            "active": active, "strategies": lib.strategies(), "suggestions": sug,
            "approvals": lib.q("SELECT * FROM approvals ORDER BY at DESC LIMIT 30"),
            "activations": lib.q("SELECT id, approval_id, version_id, action, mode, at, actor, config_version "
                                 "FROM activations ORDER BY at DESC LIMIT 30"),
            "sources": lib.sources()[:30], "events": lib.events(60)}


def strategy_view(lib, store, sid):
    import sf_library as L
    import sf_research as RS
    import sf_strategy as SS
    st = lib.strategy(sid)
    if not st:
        return {"error": "no such strategy"}
    body = lib.body(st["head_version"])
    act = _active_body(lib, store, body.get("mode"))
    return {"strategy": st, "body": body, "versions": lib.versions(sid), "description": SS.describe(body),
            "validation": SS.validate(body, available=RS.available(body.get("mode")), policy=L.load_policy(), active=act),
            "diff_vs_active": SS.diff(act, body) if act else [], "events": lib.events(50, strategy_id=sid),
            "suggestion": lib.q1("SELECT id, status, evidence_hash FROM suggestions WHERE version_id=?", st["head_version"])}


def preview_markers(body, symbol, day, data=None):
    """Builder chart: the strategy evaluated bar by bar on one symbol and session
    (completed bars only), with overlays and every bar where an entry rule passed."""
    import sf_strategy as SS
    from datetime import date as _d
    from sf_core import ist
    v = SS.validate(body or {})
    if not v["ok"]:
        return {"error": "fix the validation errors first", "validation": v}
    if data is None:
        from sf_data import RepoData
        data = RepoData(_ROOT)
    d = _d.fromisoformat(day)
    at = ist(d, "15:31")
    daily = body.get("timeframe") == "1d"
    bars = data.bars(symbol, at, "1d" if daily else "1m")
    bench = data.bars("NIFTY 50", at, "1d" if daily else "1m")
    if not bars:
        return {"error": f"no recorded bars for {symbol} on {day}"}
    out = SS.markers(body, bars, bench)
    out.update(symbol=symbol, day=day, warmup_bars=v["warmup_bars"])
    return out


SNAPSHOT = os.path.join(os.path.dirname(DB_DEFAULT), "state.json")


def write_snapshot(store, engine, path=None):
    """The worker writes the read model as JSON (atomic replace) so the API and
    publisher -- both sandboxed read-only on the server (ProtectSystem=strict) --
    never have to open the SQLite file, which in WAL mode needs write access."""
    path = path or os.environ.get("SF_SNAPSHOT", SNAPSHOT)
    out = state(store, engine.clock.now(), engine.data, engine)
    try:
        out["strategy_health"] = strategy_health(store, engine.clock.now())
        import sf_library as L
        out["research"] = research_state(L.Library(), store, limit=25)
    except Exception as e:  # noqa: BLE001 -- the health view never stops the snapshot
        out["strategy_health"] = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    for pos in out["positions"]:
        pos["timeline"] = pos["timeline"][-40:]
    for p in out["profiles"].values():
        if p.get("last_scan"):
            p["last_scan"]["timeline"] = p["last_scan"]["timeline"][-60:]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, default=str)
    os.replace(tmp, path)
    return out


def strategy_health(store, now):
    """What the Strategy Health view shows: versions, the latest daily evaluation,
    challengers and their requirements, decision history. Read-only."""
    import glob
    import sf_daily
    import sf_governance as G
    from sf_core import Calendar
    v, cfg = store.config()
    lkg = store.control("last_known_good")
    versions = []
    for r in store.q("SELECT version, hash, created, actor FROM configs ORDER BY version"):
        body = store.config(r["version"])[1] or {}
        versions.append({"version": r["version"], "hash": r["hash"], "created": r["created"], "actor": r["actor"],
                         "label": body.get("label"), "active": r["version"] == v,
                         "last_known_good": str(r["version"]) == lkg})
    latest = sf_daily._read_json(os.path.join(sf_daily.EVAL, "latest.json")) or {}
    hist = []
    for p in sorted(glob.glob(os.path.join(sf_daily.EVAL, "20*.json")))[-30:]:
        x = sf_daily._read_json(p) or {}
        hist.append({"session": x.get("session"), "status": x.get("status"), "decision": x.get("decision"),
                     "reason": x.get("reason"), "active_version": (x.get("active") or {}).get("version")})
    cal = Calendar((cfg or {}).get("calendar"))
    nxt = now.date() if cal.is_session(now.date()) and now < ist(now.date(), "16:10") else cal.next_session(now.date())
    pol = G.load_policy()
    profiles = {}
    for m in MODES:
        p = (cfg or {}).get("profiles", {}).get(m, {})
        profiles[m] = {"kind": "rule-based (fixed rules, versioned parameters; no learned model)",
                       "plan_version": (p.get("exit_plan") or {}).get("plan_version"),
                       "regime": (p.get("regime") or {}).get("method"),
                       "screen": (p.get("stock") or {}).get("method", "return thresholds"),
                       "first_bar": ((p.get("regime") or {}).get("opening_range") or {}).get("first_bar")}
    steps = latest.get("steps") or {}
    return {"available": True, "active_version": v, "config_hash": config_hash(cfg) if cfg else None,
            "label": (cfg or {}).get("label"), "last_known_good": int(lkg) if lkg else None,
            "auto_promotion": False, "approval_required": True,
            "entries_paused": {m: store.paused(m) for m in MODES}, "profiles": profiles,
            "next_run": iso(ist(nxt, "16:10")), "latest": {
                "session": latest.get("session"), "status": latest.get("status"), "decision": latest.get("decision"),
                "finished_at": latest.get("finished_at"), "governance": latest.get("governance"),
                "reconcile": steps.get("reconcile"), "data": {k: (steps.get("data") or {}).get(k)
                                                              for k in ("coverage", "findings", "quarantine")},
                "metrics": steps.get("metrics"), "compliance": steps.get("compliance"),
                "drift": steps.get("drift"), "experiments": steps.get("experiments"),
                "promotion": steps.get("promotion")},
            "history": hist, "versions": versions, "challengers": _shadow_list(),
            "policy": {k: pol[k] for k in ("version", "note", "promotion", "rollback", "experiments")},
            "search_space": pol["search_space"]}


def read_snapshot(path=None, stale_after_s=180):
    path = path or os.environ.get("SF_SNAPSHOT", SNAPSHOT)
    try:
        with open(path) as f:
            out = json.load(f)
    except (OSError, ValueError):
        return {"available": False, "reason": "stock-futures worker has not written a snapshot yet"}
    age = (datetime.now(IST) - parse_ts(out["generated"])).total_seconds()
    out["snapshot_age_s"] = int(age)
    if age > stale_after_s:
        out.setdefault("notes", []).insert(0, f"Snapshot is {int(age // 60)} min old: the stock-futures worker may be stopped.")
    return out


def publish(path=None):
    """What publisher.py writes to /live/futures (read-only on the site)."""
    return read_snapshot(path)


def _shadow_list():
    """Library strategies currently in forward shadow (for the Health view)."""
    try:
        import sf_library as L
        lib = L.Library()
        return [{"id": s["id"], "status": "shadow", "kind": (s["provenance"] or {}).get("kind"), "label": s["name"],
                 "reason": (s["provenance"] or {}).get("method") or (s["provenance"] or {}).get("via"),
                 "added_on": (s["updated"] or "")[:10], "version_id": s["head_version"], "evidence": s["evidence"]}
                for s in lib.strategies(False) if s["lifecycle"] == "SHADOW"]
    except Exception:  # noqa: BLE001
        return []

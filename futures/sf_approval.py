"""Approval-gated activation of strategy versions into the active paper portfolio.

Nothing becomes active without a recorded approval of the EXACT version and the
EXACT evidence snapshot the user saw. Activation re-checks, at activation time:
  * the approval exists, is unused, and its version/evidence hashes still match
    (a changed strategy or new evidence needs a fresh approval);
  * the version validates and its data is available now;
  * every locked area that differs from the profile's current active strategy
    is listed in the approval's lock_changes_approved;
  * hard execution constraints come from the portfolio base config (one lot,
    shared limits) and cannot be changed by the strategy.
Then, atomically in the trading store, it writes the compiled config and the new
`active_strategies` list (REPLACE a profile's strategy, or run ALONGSIDE with a
visible priority). Re-running the same approval is a no-op (idempotent).
Existing positions keep their entry config_version and exit plan.

Legacy: before the first activation the store runs ONE config (v1) for both
modes through the original agents. The first activation seeds active_strategies
with that legacy config per mode, so v1 keeps running exactly as before unless
the approval replaces it.
"""
import json
import uuid

import sf_strategy as SS
from sf_core import config_hash, iso


class ActivationRefused(Exception):
    pass


def record_approval(lib, suggestion_id, action, mode, actor, evidence_hash, lock_changes_approved=(),
                    priority=None, note=None):
    """The user's decision for one suggestion: REPLACE or ALONGSIDE."""
    if action not in ("REPLACE", "ALONGSIDE"):
        raise ValueError("action must be REPLACE or ALONGSIDE")
    s = lib.suggestion(suggestion_id)
    if s is None:
        raise KeyError(suggestion_id)
    if s["evidence_hash"] != evidence_hash:
        raise ActivationRefused("the evidence changed after you opened it: review the new evidence and approve again")
    aid = f"ap-{uuid.uuid4().hex[:10]}"
    from sf_library import now_iso
    with lib.tx() as c:
        c.execute("INSERT INTO approvals(id,suggestion_id,version_id,evidence_hash,action,mode,priority,"
                  "lock_changes_approved,actor,at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, suggestion_id, s["version_id"], evidence_hash, action, mode, priority,
                   json.dumps(sorted(lock_changes_approved)), actor, now_iso(), note))
        lib.emit(c, "ApprovalRecorded", {"approval": aid, "action": action, "mode": mode, "actor": actor,
                                         "lock_changes_approved": sorted(lock_changes_approved)},
                 strategy_id=s["strategy_id"], version_id=s["version_id"])
    return aid


def current_active(store):
    raw = store.control("active_strategies")
    return json.loads(raw) if raw else None


def seed_legacy(store, lib=None):
    """The list representing what runs today: the single legacy config per mode."""
    v, cfg = store.config()
    out = []
    for i, m in enumerate(("intraday", "swing")):
        out.append({"strategy_id": f"tpl-{m}-v1", "version_id": None, "config_version": v, "mode": m,
                    "priority": 10 + i, "name": f"deployed v1 ({m})", "legacy": True})
    return out


def primary(active, mode):
    xs = sorted([a for a in active if a["mode"] == mode], key=lambda a: a.get("priority", 99))
    return xs[0] if xs else None


def activate(lib, store, approval_id, base_cfg, now, actor, available=None, policy=None):
    from sf_library import load_policy, now_iso
    policy = policy or load_policy()
    done = lib.q1("SELECT * FROM activations WHERE approval_id=?", approval_id)
    if done:
        return {"applied": False, "idempotent": True, "activation": done}
    ap = lib.q1("SELECT * FROM approvals WHERE id=?", approval_id)
    if not ap:
        raise ActivationRefused("no recorded approval")
    if ap["used_at"]:
        raise ActivationRefused("this approval was already used")
    sug = lib.suggestion(ap["suggestion_id"])
    if not sug or sug["version_id"] != ap["version_id"] or sug["evidence_hash"] != ap["evidence_hash"]:
        _refuse(lib, ap, "the strategy version or its evidence changed after approval: approve again")
    body = lib.body(ap["version_id"])
    if body is None or SS.version_hash(body) != ap["version_id"]:
        _refuse(lib, ap, "the approved version is missing or does not match its hash")
    if body["mode"] != ap["mode"]:
        _refuse(lib, ap, f"the strategy is {body['mode']}, the approval is for {ap['mode']}")
    st = lib.strategy(sug["strategy_id"])
    if st["lifecycle"] in ("ARCHIVED",):
        _refuse(lib, ap, "the strategy is archived")
    val = SS.validate(body, available=available)
    if not val["ok"]:
        _refuse(lib, ap, "the version is not valid now: " + "; ".join(val["errors"][:5]))
    active = current_active(store) or seed_legacy(store)
    prim = primary(active, ap["mode"])
    ref_body = None
    if prim:
        ref_body = lib.body(prim["version_id"]) if prim.get("version_id") else \
            next((t for t in SS.templates_from_config(store.config(prim["config_version"])[1])[0]
                  if t["mode"] == ap["mode"]), None)
    lock_changes = SS.lock_diff(body, ref_body, policy) if ref_body else []
    approved = set(json.loads(ap["lock_changes_approved"] or "[]"))
    missing = [lc["lock"] for lc in lock_changes if lc["lock"] not in approved]
    if missing:
        _refuse(lib, ap, f"locked area(s) changed without an approved lock change: {', '.join(missing)}")
    cfg = SS.compile_config(body, base_cfg, {"strategy_id": sug["strategy_id"], "version_id": ap["version_id"]})
    for k in ("shared",):
        if json.dumps(cfg[k], sort_keys=True) != json.dumps(base_cfg[k], sort_keys=True):
            _refuse(lib, ap, "hard execution constraints differ from the portfolio's")
    h = config_hash(cfg)
    with store.tx() as c:
        r = c.execute("SELECT version FROM configs WHERE hash=?", (h,)).fetchone()
        if r:
            v = r[0]
        else:
            c.execute("INSERT INTO configs(hash,created,actor,body) VALUES(?,?,?,?)",
                      (h, iso(now), actor, json.dumps(cfg, sort_keys=True)))
            v = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        raw = c.execute("SELECT value FROM control WHERE key='active_strategies'").fetchone()
        before = json.loads(raw[0]) if raw else seed_legacy(store)
        entry = {"strategy_id": sug["strategy_id"], "version_id": ap["version_id"], "config_version": v,
                 "mode": ap["mode"], "name": body.get("name"), "approval_id": approval_id}
        if ap["action"] == "REPLACE":
            pr = prim["priority"] if prim else 10
            after = [a for a in before if a["mode"] != ap["mode"] or a.get("priority") != pr] if prim else list(before)
            after = [a for a in after if a["strategy_id"] != sug["strategy_id"]]
            entry["priority"] = pr
        else:
            after = [a for a in before if a["strategy_id"] != sug["strategy_id"]]
            entry["priority"] = ap["priority"] if ap["priority"] is not None else \
                max([a.get("priority", 10) for a in after if a["mode"] == ap["mode"]] or [10]) + 1
        after.append(entry)
        after.sort(key=lambda a: (a["mode"], a.get("priority", 99)))
        c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES('active_strategies',?,?,?)",
                  (json.dumps(after), iso(now), actor))
        store.emit(c, "StrategyActivated", actor, now, {"approval": approval_id, "action": ap["action"],
                   "mode": ap["mode"], "version_id": ap["version_id"], "config_version": v,
                   "before": before, "after": after}, mode=ap["mode"], config_version=v,
                   event_id=f"activate:{approval_id}")
    removed = [a for a in before if a not in after and not a.get("legacy")]
    with lib.tx() as c:
        c.execute("INSERT OR IGNORE INTO activations(id,approval_id,version_id,action,mode,from_active,to_active,at,actor,"
                  "config_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (f"act-{uuid.uuid4().hex[:10]}", approval_id, ap["version_id"], ap["action"], ap["mode"],
                   json.dumps(before), json.dumps(after), now_iso(), actor, v))
        c.execute("UPDATE approvals SET used_at=? WHERE id=?", (now_iso(), approval_id))
        c.execute("UPDATE suggestions SET status=?, decided_at=?, decided_by=?, updated=? WHERE id=?",
                  ("REPLACED" if ap["action"] == "REPLACE" else "ALONGSIDE", now_iso(), actor, now_iso(), sug["id"]))
        lib._force_lifecycle(c, sug["strategy_id"], "ACTIVE", f"activated ({ap['action']}) by {actor}")
        for a in removed:
            lib._force_lifecycle(c, a["strategy_id"], "DISABLED", f"replaced by {sug['strategy_id']}")
        if ap["action"] == "REPLACE" and prim and prim.get("legacy"):
            lib._force_lifecycle(c, prim["strategy_id"], "DISABLED", f"replaced by {sug['strategy_id']}")
        lib.emit(c, "StrategyActivated", {"approval": approval_id, "action": ap["action"], "mode": ap["mode"],
                                          "config_version": v, "after": after},
                 strategy_id=sug["strategy_id"], version_id=ap["version_id"])
    return {"applied": True, "config_version": v, "active": after}


def _refuse(lib, ap, why):
    with lib.tx() as c:
        lib.emit(c, "ActivationRefused", {"approval": ap["id"], "reason": why}, version_id=ap.get("version_id"))
    raise ActivationRefused(why)


def deactivate(lib, store, strategy_id, now, actor, to="DISABLED"):
    """Remove a strategy from active use (no new entries). Open positions keep
    being managed by their pinned config. No approval needed to STOP trading."""
    with store.tx() as c:
        raw = c.execute("SELECT value FROM control WHERE key='active_strategies'").fetchone()
        before = json.loads(raw[0]) if raw else seed_legacy(store)
        after = [a for a in before if a["strategy_id"] != strategy_id]
        if after == before:
            return {"applied": False, "reason": "not active"}
        c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES('active_strategies',?,?,?)",
                  (json.dumps(after), iso(now), actor))
        store.emit(c, "StrategyDeactivated", actor, now, {"strategy_id": strategy_id, "before": before, "after": after},
                   event_id=f"deactivate:{strategy_id}:{iso(now)}")
    with lib.tx() as c:
        if lib.q1("SELECT 1 FROM strategies WHERE id=?", strategy_id):
            lib._force_lifecycle(c, strategy_id, to, f"removed from active use by {actor}")
        lib.emit(c, "StrategyDeactivated", {"actor": actor, "after": after}, strategy_id=strategy_id)
    return {"applied": True, "active": after}

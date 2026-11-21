"""Daily evaluation and controlled improvement of the stock-futures paper system.

Runs once per completed exchange session (systemd options-futures-eval.timer,
16:10 IST, Mon-Fri; the job itself checks the XBOM calendar). Steps:

   1 reconcile   orders, fills, positions and the ledger (champion + every shadow)
   2 validate    the session's data; quarantine symbols with bad or missing bars
   3 measure     performance by mode/direction and data-quality metrics
   4 comply      actual trades vs the active specification (windows, one lot,
                 sides, stop/target formulas, deadlines)
   5 drift       robust_z/1 over session features; expectancy_ci/1 per mode
   6 research    count the session toward the research cadence (sf_research.py
                 queues a cycle every N completed sessions; idempotent)
   7 operate     pause new entries automatically on hard-risk / ledger failure
                 (monitoring and exits continue). NO strategy is ever activated,
                 promoted or rolled back here: that needs your approval
                 (sf_approval.py).
   8 publish     data/futures/eval/<session>.json, latest.json and audit.jsonl

Idempotent: a session already evaluated is skipped (unless --force). A file
lock prevents two jobs (and so two promotions) at once. Missing or incomplete
feeds DEFER the job (exit code 75; systemd retries every 20 minutes, at most 6
times); a holiday records SKIPPED. A crash leaves no partial record: the record
is written atomically at the end; the research cadence is keyed by session, so
a rerun can never count a session twice.

    python sf_daily.py run [--day YYYY-MM-DD] [--force] [--no-experiments]
    python sf_daily.py status
"""
import argparse
import contextlib
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import project_paths
import sf_api
import sf_audit
import sf_governance as G
import sf_metrics as M
from sf_core import IST, Calendar, LONG, config_hash, ist, iso, load_config, parse_ts
from sf_store import Store

ROOT = project_paths.ROOT
FUT = os.path.join(ROOT, "data", "futures")
EVAL = os.path.join(FUT, "eval")
SHADOW = os.path.join(FUT, "shadow")
CONFIGS = os.path.join(FUT, "configs")
CHALLENGERS = os.path.join(FUT, "challengers.json")
AUDIT = os.path.join(EVAL, "audit.jsonl")
DEFERRED = 75


# ------------------------------------------------------------------ small helpers

def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def audit(event, **kw):
    os.makedirs(EVAL, exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(event=event, at=iso(datetime.now(IST)), **kw), sort_keys=True, default=str) + "\n")


@contextlib.contextmanager
def job_lock(path=None):
    """One evaluation (and so one promotion) at a time, across processes."""
    path = path or os.path.join(EVAL, ".lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:                              # Windows (tests, manual runs)
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("another sf_daily job holds the lock")
        yield
    finally:
        f.close()


def feeds_complete(day, policy, root=None):
    import glob
    fc = policy["data_quality"]["feeds_complete"]
    base = os.path.join(root or ROOT, "data", "stocks_1m", f"date={day}")
    missing = []
    for tier in fc["tiers"]:
        n = len(glob.glob(os.path.join(base, f"underlying={tier}", "*.parquet")))
        if n < fc["min_parts"]:
            missing.append(f"{tier}: {n}/{fc['min_parts']} minute parts")
    return not missing, missing


def compliance(store, day):
    """Trades entered on `day` checked against the config version they used."""
    from sf_agents import plan_levels
    out = []
    for tr in store.trades():
        t = tr["body"]
        if (t.get("opened_at") or t.get("created_at") or "")[:10] != str(day):
            continue
        v, cfg = store.config(t["config_version"])
        prof = cfg["profiles"][t["mode"]]
        tid = t["trade_id"]
        a, b = prof["entry_window"]
        at = (t.get("created_at") or "")[11:16]
        out.append(_chk("entry inside window", a <= at <= b, f"{tid}: entry {at} vs {a}-{b}"))
        units = (t.get("result") or {}).get("entry_filled_units") or t.get("entry_filled_qty")
        out.append(_chk("one lot", t.get("lots") == 1 and (units in (None, t.get("lot_size"))),
                        f"{tid}: {units} units, lot {t.get('lot_size')}"))
        plan = t.get("exit_plan") or {}
        if plan and t.get("entry_avg"):
            sp, tp = plan_levels(t["direction"], t["entry_avg"], prof["exit_plan"], t.get("stop_risk_pct"))
            ok = (sp is None or abs(sp - plan.get("stop_price", sp)) < 1e-6) and \
                 (tp is None or abs(tp - plan.get("target_price", tp)) < 1e-6)
            out.append(_chk("stop/target formula", ok, f"{tid}: plan {plan.get('stop_price')}/{plan.get('target_price')} "
                                                       f"vs recomputed {sp}/{tp}"))
            cap = prof["exit_plan"].get("hard_max_stop_pct")
            if t.get("stop_risk_pct") is not None and cap:
                out.append(_chk("stop within hard cap", t["stop_risk_pct"] * 100 <= cap + 1e-9,
                                f"{tid}: {t['stop_risk_pct'] * 100:.2f}% vs {cap}%"))
        if t["mode"] == "intraday" and t.get("closed_at"):
            dl = parse_ts(plan.get("intraday_deadline")) + timedelta(seconds=plan.get("cutoff_grace_s", 600)) \
                if plan.get("intraday_deadline") else None
            out.append(_chk("intraday flat by the deadline", dl is None or parse_ts(t["closed_at"]) <= dl,
                            f"{tid}: closed {t['closed_at'][11:19]}"))
        ex = (t.get("exit") or {})
        if ex.get("reason") in ("stop", "target") and ex.get("observed_price") is not None:
            lvl, obs, d = ex.get("trigger_price"), ex["observed_price"], t["direction"]
            crossed = (obs <= lvl if (ex["reason"] == "stop") == (d == LONG) else obs >= lvl)
            out.append(_chk("exit level actually crossed", crossed, f"{tid}: {ex['reason']} {lvl} observed {obs}"))
    if not out:
        out.append(_chk("trades checked", True, f"no trades entered on {day}"))
    return out


def _chk(name, ok, detail):
    return {"check": name, "status": "OK" if ok else "FAIL", "detail": detail}


def features(funnel_day, rows_day, data_cover):
    intr = funnel_day.get("intraday") or {}
    scans = intr.get("scans") or 0
    rej = intr.get("rejected") or {}
    eq = (data_cover or {}).get("EQ") or {}
    permitted = sum(v for k, v in (intr.get("regimes") or {}).items() if k in ("BULLISH", "BEARISH"))
    return {"eq_zero_volume_share": eq.get("zero_volume_share"),
            "eq_first_bar_defect_share": eq.get("first_bar_open_at_extreme_share"),
            "candidates_per_scan": round(sum(rej.values()) / scans, 3) if scans else None,
            "permitted_scan_share": round(permitted / scans, 3) if scans else None,
            "stop_cap_rejection_share": round(rej.get("opening-range stop", 0) / max(sum(rej.values()), 1), 3),
            "trades": len(rows_day)}


def quarantine(data_cover, findings):
    """Symbols/tiers whose data failed validation for the session."""
    q = []
    for f in findings:
        if f["status"] == "FAIL" and "first-bar defect" not in f["check"]:
            q.append(f["check"])
    return q


# ------------------------------------------------------------------ the job

def run_day(day=None, force=False, experiments=True, policy=None, champion_db=None, now=None):
    policy = policy or G.load_policy()
    now = now or datetime.now(IST)
    champion_db = champion_db or os.environ.get("SF_DB", sf_api.DB_DEFAULT)
    store = Store(champion_db)
    v_active, cfg_active = store.config()
    cal = Calendar((cfg_active or {}).get("calendar"))
    day = day or (now.date() if cal.is_session(now.date()) and now >= ist(now.date(), "15:45")
                  else cal.prev_session(now.date()))
    rec_path = os.path.join(EVAL, f"{day}.json")
    prior = _read_json(rec_path)
    if prior and prior.get("status") in ("DONE", "SKIPPED") and not force:
        return prior, 0
    if not cal.is_session(day):
        rec = {"session": str(day), "status": "SKIPPED", "reason": "not an exchange session", "at": iso(now)}
        _atomic_json(rec_path, rec)
        return rec, 0
    ok, missing = feeds_complete(day, policy)
    if not ok:
        rec = {"session": str(day), "status": "DEFERRED", "reason": "feeds incomplete: " + "; ".join(missing),
               "at": iso(now), "attempts": (prior or {}).get("attempts", 0) + 1}
        _atomic_json(rec_path, rec)
        audit("deferred", session=str(day), reason=rec["reason"])
        return rec, DEFERRED

    rec = {"session": str(day), "status": "RUNNING", "at": iso(now), "governance": policy["version"],
           "active": {"version": v_active, "config_hash": config_hash(cfg_active) if cfg_active else None,
                      "label": (cfg_active or {}).get("label")}, "steps": {}}
    # 1 reconcile
    with sf_audit.ro(champion_db) as c:
        ledger_rows, ledger_findings = sf_audit.ledger(c)
        fun_day = sf_audit.funnel(c, day)
    shadow = {}
    try:
        import sf_library as L
        lib = L.Library()
        for st in lib.strategies(False):
            db = os.path.join(SHADOW, f"{st['head_version']}.db")
            if st["lifecycle"] != "SHADOW" or not os.path.exists(db):
                continue
            with sf_audit.ro(db) as c:
                _, f = sf_audit.ledger(c)
            shadow[st["head_version"]] = {"ledger_findings": f, "rows": sf_api.journal(Store(db))}
    except Exception as e:  # noqa: BLE001 -- the library is optional for the daily checks
        lib = None
        print(f"[sf_daily] library unavailable: {type(e).__name__}: {e}")
    rec["steps"]["reconcile"] = {"champion": ledger_findings,
                                 "shadow": {k: v["ledger_findings"] for k, v in shadow.items()},
                                 "unresolved": [r["trade_id"] for r in ledger_rows if r.get("unresolved")]}
    # 2 validate
    cover, dfind = sf_audit.data_quality(day)
    rec["steps"]["data"] = {"coverage": cover, "findings": dfind, "quarantine": quarantine(cover, dfind)}
    # 3 measure
    rows = sf_api.journal(store)
    for r in rows:
        r["quarantined"] = bool(rec["steps"]["data"]["quarantine"]) and (r.get("opened_at") or "")[:10] == str(day)
    clean = [r for r in rows if not r["quarantined"]]
    rows_day = [r for r in rows if (r.get("opened_at") or "")[:10] == str(day)]
    rec["steps"]["metrics"] = {"session": M.by_mode_direction(rows_day), "cumulative": M.by_mode_direction(clean),
                               "quarantined_trades": [r["trade_id"] for r in rows if r["quarantined"]]}
    # 4 comply
    rec["steps"]["compliance"] = compliance(store, day)
    # 5 drift / deterioration
    hist = [x["steps"]["features"] for x in history(before=day) if x.get("steps", {}).get("features")]
    feats = features(fun_day, rows_day, cover)
    rec["steps"]["features"] = feats
    det = {m: G.deterioration(clean, m, policy) for m in ("intraday", "swing")}
    rec["steps"]["drift"] = {"data": G.drift(hist, feats, policy), "performance": det}
    flags = (["drift"] if rec["steps"]["drift"]["data"]["status"] == "DRIFT" else []) + \
            (["deterioration"] if any(v["status"] == "DETERIORATED" for v in det.values()) else [])
    # 6 research cadence (every N completed sessions; never gated on promotion thresholds)
    rec["steps"]["research"] = {}
    if lib is not None and experiments:
        try:
            import sf_research as RS
            job = RS.on_session(lib, day)
            rec["steps"]["research"] = {"queued": job["id"] if job else None, "cadence": lib.get("cadence")}
        except Exception as e:  # noqa: BLE001
            rec["steps"]["research"] = {"error": f"{type(e).__name__}: {e}"}
    rec["steps"]["experiments"] = {"eligible": False, "reasons": ["replaced by the research agent (sf_research.py)"],
                                   "flags": flags, "ran": False}
    # 7 operational checks (no strategy activation here)
    rec["steps"]["promotion"] = assess_all(store, cfg_active, v_active, shadow, rows, policy, now,
                                           ledger_findings, rec["steps"]["compliance"])
    rec["decision"] = summarise(rec)
    rec["status"] = "DONE"
    rec["finished_at"] = iso(datetime.now(IST))
    _atomic_json(rec_path, rec)
    _atomic_json(os.path.join(EVAL, "latest.json"), rec)
    audit("evaluated", session=str(day), decision=rec["decision"])
    return rec, 0


def history(before=None):
    import glob
    out = []
    for p in sorted(glob.glob(os.path.join(EVAL, "20*.json"))):
        d = os.path.basename(p)[:10]
        if before and d >= str(before):
            continue
        x = _read_json(p)
        if x:
            out.append(x)
    return out


def assess_all(store, cfg_active, v_active, shadow, champ_rows, policy, now, champ_ledger, champ_compliance):
    """Operational checks only. Strategy changes need explicit approval
    (sf_approval.py); this job never activates, promotes or rolls back a strategy.
    It MAY pause new entries automatically for hard-risk or ledger failures
    (monitoring and exits continue) and says so loudly."""
    out = {"policy": "every activation needs your approval of the exact version (sf_approval.py)",
           "auto_pause": None, "shadow": {k: {"ledger_ok": all(f["status"] == "OK" for f in v["ledger_findings"]),
                                             "trades": len(v["rows"])} for k, v in shadow.items()}}
    day = now.date()
    loss = sum(float(r.get("net_pnl") or 0) for r in champ_rows
               if r.get("class") == "settled" and (r.get("closed_at") or "")[:10] == str(day))
    limit = cfg_active["shared"]["daily_loss_limit_rs"] if cfg_active else None
    why = []
    if any(f["status"] == "FAIL" for f in champ_ledger):
        why.append("ledger reconciliation failed: " + "; ".join(f["detail"] for f in champ_ledger if f["status"] == "FAIL")[:300])
    if limit and loss <= -limit:
        why.append(f"daily loss Rs {loss:,.0f} reached the Rs {limit:,.0f} limit")
    if why and store.control("pause:global") != "1":
        with store.tx() as c:
            c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES('pause:global','1',?,?)",
                      (iso(now), "sf_daily:auto-pause"))
            store.emit(c, "EntriesPausedAutomatically", "sf_daily", now, {"reasons": why},
                       event_id=f"auto-pause:{iso(now)}")
        out["auto_pause"] = why
        alert("Stock futures: NEW ENTRIES PAUSED automatically (monitoring and exits continue): " + "; ".join(why))
    elif store.paused("intraday") or store.paused("swing"):
        alert("Stock futures: new entries are PAUSED (" + ", ".join(
            m for m in ("global", "intraday", "swing") if store.control(f"pause:{m}") == "1") + ")")
    return out


def summarise(rec):
    p = rec["steps"]["promotion"]
    if p.get("auto_pause"):
        return "ENTRIES PAUSED (" + "; ".join(p["auto_pause"]) + ")"
    r = rec["steps"].get("research") or {}
    if r.get("queued"):
        return f"CHECKED; research cycle queued ({r['queued']})"
    return "CHECKED (no strategy change: changes need your approval)"


def alert(text):
    try:
        import notify
        notify.send(text)
    except Exception as e:  # noqa: BLE001 -- an alert must never break the job
        print(f"[sf_daily] alert not sent ({type(e).__name__}): {text}")


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--day")
    r.add_argument("--force", action="store_true")
    r.add_argument("--no-experiments", action="store_true")
    r.add_argument("--db")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps({"latest": _read_json(os.path.join(EVAL, "latest.json"), {}).get("decision")},
                         indent=1, default=str))
        return 0
    try:
        with job_lock():
            rec, code = run_day(date.fromisoformat(a.day) if a.day else None, a.force, not a.no_experiments,
                                champion_db=a.db)
    except RuntimeError as e:
        print(str(e))
        return 1
    print(json.dumps({k: rec.get(k) for k in ("session", "status", "reason", "decision")}, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())

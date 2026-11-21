"""Reproducible replays and a durable experiment registry.

A run pins: the code (git commit + sha256 of every futures/*.py), the config
(full JSON + hash), the data snapshot (sha256 over the names, sizes and mtimes
of every input file of the replayed sessions), the cost/fill model (part of
the config) and the sessions. Each run is appended to

    data/futures/experiments/registry.jsonl

including failed and rejected candidates. Nothing is ever rewritten in place.

    python sf_experiments.py replay --config PATH --sessions 2026-11-19,2026-11-20 --name baseline-v1
    python sf_experiments.py list
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date

import project_paths

HERE = os.path.dirname(os.path.abspath(__file__))
REG_DIR = os.path.join(project_paths.ROOT, "data", "futures", "experiments")
REGISTRY = os.path.join(REG_DIR, "registry.jsonl")


def code_fingerprint():
    files = sorted(glob.glob(os.path.join(HERE, "sf_*.py")))
    h = hashlib.sha256()
    for f in files:
        h.update(os.path.basename(f).encode())
        h.update(open(f, "rb").read())
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True,
                                text=True, timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=HERE, capture_output=True,
                                    text=True, timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {"code_sha256": h.hexdigest()[:16], "git_commit": commit, "git_dirty": dirty, "files": len(files)}


def data_fingerprint(sessions, root=None):
    root = root or project_paths.ROOT
    pats = []
    for d in sessions:
        pats += [os.path.join(root, "data", "stocks_1m", f"date={d}", "*", "*.parquet"),
                 os.path.join(root, "data", "bars_1m", f"date={d}", "underlying=INDEX", "*.parquet"),
                 os.path.join(root, "data", "instruments", f"{d}.json")]
    pats.append(os.path.join(root, "data", "sectors.json"))
    files = sorted(f for p in pats for f in glob.glob(p))
    h = hashlib.sha256()
    for f in files:
        st = os.stat(f)
        h.update(f"{os.path.relpath(f, root)}|{st.st_size}|{int(st.st_mtime)}".encode())
    return {"data_sha256": h.hexdigest()[:16], "files": len(files), "sessions": [str(d) for d in sessions]}


def replay(cfg, sessions, db, modes=("intraday", "swing"), root=None):
    """Replay `sessions` (one engine per session, store reused) and return the
    journal rows plus scan-funnel counts. Paper only; no network, no Telegram."""
    from sf_core import ReplayClock, ist
    from sf_data import RepoData
    from sf_engine import Engine
    from sf_store import Store
    import sf_api
    import sf_cli
    import sf_audit
    if os.path.exists(db):
        os.remove(db)
    from sf_data import SNAP_TTL_S
    data = RepoData(root or project_paths.ROOT, lag_s=SNAP_TTL_S)     # live worst-case data latency
    store = Store(db)
    clock = ReplayClock(ist(sessions[0], "09:15"))
    e = Engine(store, data, clock, cfg, env="replay", modes=modes)
    sf_cli.replay(e, clock, sessions)
    rows = sf_api.journal(store)
    for r in rows:                       # tick size for the cost-stress test
        t = store.trade(r["trade_id"])["body"]
        r["tick_size"] = t.get("tick_size")
    with sf_audit.ro(db) as c:
        fun = sf_audit.funnel(c)
        _, findings = sf_audit.ledger(c)
    return rows, fun, findings


def record(entry, path=None):
    path = path or REGISTRY
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = dict(entry, recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return entry


def registry(path=None):
    try:
        return [json.loads(x) for x in open(path or REGISTRY, encoding="utf-8") if x.strip()]
    except FileNotFoundError:
        return []


def run(name, cfg, sessions, purpose, db_dir=None, root=None, parent=None, window="baseline"):
    """Replay, measure and record one candidate. Returns the registry entry."""
    import sf_metrics as M
    from sf_core import config_hash
    db_dir = db_dir or os.path.join(REG_DIR, "dbs")
    os.makedirs(db_dir, exist_ok=True)
    t0 = time.time()
    entry = {"name": name, "purpose": purpose, "window": window, "parent": parent,
             "config_hash": config_hash(cfg), "config": cfg, "code": code_fingerprint(),
             "data": data_fingerprint(sessions, root), "sessions": [str(d) for d in sessions],
             "costs": cfg["shared"]["costs"], "fill_policy": cfg["shared"]["fill_policy"], "seed": 7,
             "replay_data_lag_s": __import__("sf_data").SNAP_TTL_S}
    try:
        # A strategy runs in ITS mode only: replaying both modes counted the other
        # profile's legacy trades as the strategy's (found 21 Nov 2026).
        mode = (cfg.get("strategy_ref") or {}).get("mode")
        modes = (mode,) if mode else ("intraday", "swing")
        rows, fun, findings = replay(cfg, sessions, os.path.join(db_dir, f"{name}.db"), modes=modes, root=root)
        rows = [r for r in rows if r.get("mode") in modes]
        entry.update(status="DONE", trades=rows, funnel=fun, ledger_findings=findings,
                     metrics=M.by_mode_direction(rows), stressed_metrics=M.by_mode_direction(M.stressed(rows)))
    except Exception as e:  # noqa: BLE001 -- a failed candidate is still recorded
        entry.update(status="FAILED", error=f"{type(e).__name__}: {e}")
    entry["seconds"] = round(time.time() - t0, 1)
    return record(entry)


def main():
    from sf_core import load_config
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--config")
    r.add_argument("--sessions", required=True)
    r.add_argument("--name", required=True)
    r.add_argument("--purpose", default="manual replay")
    r.add_argument("--window", default="baseline")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "list":
        for e in registry():
            p = (e.get("metrics") or {}).get("portfolio") or {}
            print(f"{e['recorded_at']}  {e['name']:<28} {e['status']:<7} {e['window']:<10} "
                  f"sessions {len(e['sessions'])} trades {p.get('settled')} net {p.get('net_pnl')}  {e['purpose']}")
        return
    sessions = [date.fromisoformat(x) for x in a.sessions.split(",")]
    e = run(a.name, load_config(a.config), sessions, a.purpose, window=a.window)
    print(json.dumps({k: e.get(k) for k in ("name", "status", "error", "seconds", "config_hash", "data")}, default=str))
    print(json.dumps((e.get("metrics") or {}).get("portfolio"), default=str))


if __name__ == "__main__":
    sys.exit(main())

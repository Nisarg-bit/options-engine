"""Command line for the stock-futures paper system.

    python futures/sf_cli.py demo   --scenario bullish --mode intraday
    python futures/sf_cli.py demo   --scenario swing-bearish --mode swing
    python futures/sf_cli.py replay --source repo --date 2026-11-12 --mode intraday
    python futures/sf_cli.py worker --source repo --mode both
    python futures/sf_cli.py status
    python futures/sf_cli.py pause  --scope global --on      (and --off)

demo    fixture scenario on an injected clock (synthetic data, labelled FIXTURE)
replay  the repo's own collected data for one past session, injected clock,
        point-in-time reads (labelled REPLAY)
worker  the live paper worker: one thread per agent, real clock, the repo's
        data. Current-session entries stay disabled until the config's
        live.entries_enabled and rules_validated are true and every required
        feed is healthy; analysis, monitoring and exits still run.
"""
import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
_UP = os.path.dirname(HERE)
for p in (_UP, os.path.join(_UP, "pipeline"), os.path.join(_UP, "quant"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
import project_paths                                                     # noqa: E402  (repo or flat server folder)
ROOT = project_paths.ROOT

from sf_core import ReplayClock, SystemClock, ist, load_config, MODES   # noqa: E402
from sf_store import Store                                               # noqa: E402
from sf_data import FixtureData, RepoData                                # noqa: E402
from sf_engine import Engine                                             # noqa: E402
import sf_api                                                            # noqa: E402

SCENARIOS = {"bullish": "bullish", "bearish": "bearish", "sideways": "sideways",
             "swing-bullish": "swing_bullish", "swing-bearish": "swing_bearish"}


def replay(engine, clock, sessions, step_minutes=1, after_close=True):
    """Drive the engine through whole sessions on the injected clock."""
    for d in sessions:
        t, end = ist(d, "09:15"), ist(d, "15:30")
        if clock.now() < t:
            clock.set(t)
        while clock.now() <= end:
            engine.tick(clock.now())
            clock.advance(minutes=step_minutes)
        if after_close:
            clock.set(ist(d, "15:45"))
            engine.tick(clock.now())


def print_run(store, engine, now):
    s = sf_api.state(store, now, engine.data, engine)
    print(f"\n== {s['label']} == config v{s['config_version']}: {s['config_label']}")
    for m, p in s["profiles"].items():
        sc = p["last_scan"]
        if not sc:
            continue
        reg = (sc.get("regime") or {})
        print(f"\n[{m}] last scan {sc['scan_id']} status {sc['status']} regime {reg.get('regime')} "
              f"permitted {reg.get('permitted')}")
        if sc.get("no_trade_reason"):
            print(f"  no trade: {sc['no_trade_reason']}")
    for tr in store.trades():
        print(f"\n--- trade {tr['trade_id']} ({tr['mode']} {tr['direction']}) status {tr['status']}")
        for ev in sf_api.timeline(store, trade_id=tr["trade_id"]):
            print(f"  {ev['at'][11:16]} {ev['agent']:<20} {ev['type']:<22} {ev['summary']}")
    print("\njournal:")
    for r in s["journal"]:
        print(f"  {r['trade_id']:<38} {r['mode']:<8} {r['direction']:<5} {r['entry_side'] or '-':<4}->{r['exit_side'] or '-':<4} "
              f"{r['class']:<10} gross {r['gross_pnl']} costs {r['costs']} net {r['net_pnl']} ({r['exit_reason']})")
    print("summary:", json.dumps(s["summary"]["all"]))
    return s


def cmd_demo(a):
    import sf_fixtures as F
    sc = getattr(F, SCENARIOS[a.scenario])()
    data = sc.build()
    db = a.db or os.path.join(ROOT, "data", "futures", f"demo-{a.scenario}.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    if os.path.exists(db) and not a.keep:
        os.remove(db)
    clock = ReplayClock(ist(sc.sessions[0], "09:15"))
    store = Store(db)
    modes = MODES if a.mode == "both" else (a.mode,)
    e = Engine(store, FixtureData(data), clock, F.demo_config(), env="fixture", modes=modes)
    if a.until:                       # stop mid-session to capture open positions for the page demo
        until = ist(sc.sessions[0], a.until)
        while clock.now() <= until:
            e.tick(clock.now())
            clock.advance(minutes=1)
    else:
        replay(e, clock, sc.sessions)
    s = print_run(store, e, clock.now())
    if a.out:
        with open(a.out, "w") as f:
            json.dump(s, f, indent=1, default=str)
        print("wrote", a.out)
    print("db:", db)


def cmd_replay(a):
    if a.source != "repo":
        sys.exit("replay --source repo only; use `demo` for fixtures")
    day = date.fromisoformat(a.date)
    data = RepoData(ROOT)
    db = a.db or os.path.join(ROOT, "data", "futures", f"replay-{a.date}.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    if os.path.exists(db) and not a.keep:
        os.remove(db)
    clock = ReplayClock(ist(day, "09:15"))
    store = Store(db)
    cfg = load_config(a.config)
    modes = MODES if a.mode == "both" else (a.mode,)
    e = Engine(store, data, clock, cfg, env="replay", modes=modes)
    print("feeds:", json.dumps(data.health(ist(day, "15:30")), indent=1))
    replay(e, clock, [day])
    print_run(store, e, clock.now())


def cmd_worker(a):
    data = RepoData(ROOT) if a.source == "repo" else None
    if data is None:
        sys.exit("worker --source repo (fixtures run through `demo`)")
    db = a.db or os.environ.get("SF_DB", sf_api.DB_DEFAULT)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    store = Store(db)
    # Always load the file: an edited config becomes a new version (future trades only);
    # an unchanged one matches its stored hash and nothing changes.
    cfg = load_config(a.config)
    modes = MODES if a.mode == "both" else (a.mode,)
    import sf_report
    from datetime import timedelta
    started = SystemClock().now()
    # Alerts go through pipeline/notify.py (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID in .env;
    # without them it just prints). Events from before this start are not re-sent.
    notifier = sf_report.Notifier(store, since=started - timedelta(minutes=2))
    e = Engine(store, data, SystemClock(), cfg, env="paper", modes=modes, notifier=notifier)
    gate = e.live_gate()
    with store.tx() as c:
        store.emit(c, "WorkerStarted", "worker", started, {"worker": e.worker_id, "gate": gate,
                   "modes": list(modes), "config_version": e.config_version})
    print(f"stock-futures worker {e.worker_id}: db {db}, modes {modes}, config v{e.config_version}")
    print("entries:", gate or "ENABLED (paper)")
    e.start(poll_s=a.poll)
    try:
        while True:
            try:
                sf_api.write_snapshot(store, e)
            except Exception as ex:  # noqa: BLE001 -- the snapshot is for viewers; trading continues
                print(f"[sf] snapshot failed: {type(ex).__name__}: {ex}")
            time.sleep(15)
    except KeyboardInterrupt:
        print("stopping; state is durable, restart resumes open positions and pending exits")
        e.stop()


def cmd_status(a):
    store = Store(a.db or os.environ.get("SF_DB", sf_api.DB_DEFAULT))
    from datetime import datetime
    from sf_core import IST
    s = sf_api.state(store, datetime.now(IST))
    print(json.dumps({k: s[k] for k in ("config_version", "paused_global", "summary")}, indent=1, default=str))
    for p in s["positions"]:
        print(p["trade_id"], p["status"], p["direction"], p["tradingsymbol"], "unresolved:", p["unresolved"])


def cmd_pause(a):
    from datetime import datetime
    from sf_core import IST
    store = Store(a.db or os.environ.get("SF_DB", sf_api.DB_DEFAULT))
    ok, err = sf_api.set_pause(store, a.scope, a.on, f"cli:{os.environ.get('USERNAME') or os.environ.get('USER') or '?'}",
                               datetime.now(IST))
    print("ok" if ok else err)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--scenario", choices=sorted(SCENARIOS), default="bullish")
    d.add_argument("--mode", choices=("intraday", "swing", "both"), default="intraday")
    d.add_argument("--db")
    d.add_argument("--out")
    d.add_argument("--until", help="HH:MM on the first session: stop there (page demo snapshot)")
    d.add_argument("--keep", action="store_true")
    r = sub.add_parser("replay")
    r.add_argument("--source", default="repo")
    r.add_argument("--date", required=True)
    r.add_argument("--mode", choices=("intraday", "swing", "both"), default="intraday")
    r.add_argument("--config")
    r.add_argument("--db")
    r.add_argument("--keep", action="store_true")
    w = sub.add_parser("worker")
    w.add_argument("--source", default="repo")
    w.add_argument("--mode", choices=("intraday", "swing", "both"), default="both")
    w.add_argument("--config")
    w.add_argument("--db")
    w.add_argument("--poll", type=float, default=2.0)
    s = sub.add_parser("status")
    s.add_argument("--db")
    p = sub.add_parser("pause")
    p.add_argument("--scope", choices=("global", "intraday", "swing"), required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", dest="on", action="store_false")
    p.add_argument("--db")
    a = ap.parse_args(argv)
    {"demo": cmd_demo, "replay": cmd_replay, "worker": cmd_worker, "status": cmd_status, "pause": cmd_pause}[a.cmd](a)


if __name__ == "__main__":
    main()

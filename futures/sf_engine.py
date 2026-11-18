"""Runs the seven agents, the paper broker and the scan scheduler.

Two ways to drive it, same agents and store:
  run_until_idle(now)  deterministic, single-threaded: used by replay and
                       tests with an injected clock. Order per pass is
                       broker -> exits -> monitor -> exit reconciliation ->
                       approvals -> the scan chain, so monitoring and exits
                       always go first.
  start()/stop()       one thread per agent plus broker and scheduler, each
                       with its own SQLite connection, for the live paper
                       worker. The dashboard can be closed; nothing here
                       depends on it.
Competing modes: scans are requested in shared.mode_priority order and every
agent takes its inputs in event order, so for the same scan time the
higher-priority mode reaches the entry agent (and the reservation table)
first. The reservation table's primary key settles any remaining race.
"""
import os
import socket
import threading
import time
from datetime import timedelta

import sf_agents as A
from sf_broker import PaperBroker
from sf_core import Calendar, SystemClock, iso, parse_ts, validate_config, MODES
from sf_data import PointInTime


class Engine:
    def __init__(self, store, data, clock=None, cfg=None, env="fixture", fill_mode="quotes",
                 worker_id=None, actor="system", modes=MODES):
        self.store = store
        self.modes = tuple(modes)
        self.data = PointInTime(data)
        self.clock = clock or SystemClock()
        self.env = env                       # fixture | replay | paper (current session)
        self.fill_mode = fill_mode           # quotes | bars
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{id(self) % 10000}"
        self.label = {"fixture": "FIXTURE DEMO - paper", "replay": "REPLAY - paper",
                      "paper": "PAPER - current session"}.get(env, env)
        if cfg is not None:
            errs = validate_config(cfg)
            if errs:
                raise ValueError("invalid config: " + "; ".join(errs))
            self.config_version = store.put_config(cfg, self.clock.now(), actor)
        else:
            self.config_version, cfg = store.config()
            if cfg is None:
                raise ValueError("no config stored; pass cfg=")
        self._cfg = {}
        self.cal = Calendar(cfg.get("calendar"))
        self.broker = PaperBroker(self)
        self.a = [A.MarketRegime(self), A.SectorRanking(self), A.StockScreening(self),
                  A.Verification(self), A.FuturesEntry(self), A.PositionMonitor(self), A.PaperExit(self)]
        self.by_name = {x.name: x for x in self.a}
        self._stop = threading.Event()
        self._threads = []

    # ------------------------------------------------------------ config
    def cfg(self, version):
        if version not in self._cfg:
            v, body = self.store.config(version)
            self._cfg[version] = body
        return self._cfg[version]

    def cfg_now(self):
        v = int(self.store.control("active_config") or self.config_version)
        return self.cfg(v)

    def active_version(self):
        return int(self.store.control("active_config") or self.config_version)

    def live_gate(self):
        """Why current-session paper entries are blocked, or None."""
        if self.env in ("fixture", "replay"):
            return None
        cfg = self.cfg_now()
        why = []
        if not cfg["live"].get("entries_enabled"):
            why.append("live.entries_enabled is false")
        if not cfg.get("rules_validated"):
            why.append("strategy rules not validated (rules_validated=false; demo rules in use)")
        why += validate_config(cfg)
        h = self.data.health(self.clock.now())
        bad = [f"{k}: {v}" for k, v in h["feeds"].items()
               if k in cfg["live"]["required_feeds"] and v != "ok"]
        if bad:
            why.append("feeds not ready: " + ", ".join(bad))
        return ("current-session entries disabled: " + "; ".join(why)) if why else None

    def day_pnl(self, now):
        day = str(now.date())
        net = 0.0
        for t in self.store.trades("status='CLOSED' AND substr(closed,1,10)=?", day):
            net += t["body"]["result"]["net_pnl"]
        for t in self.store.trades("status IN ('OPEN','EXITING')"):
            net += t["body"].get("mtm") or 0.0
        return net

    # ------------------------------------------------------------ scans
    def request_scan(self, mode, now=None):
        now = now or self.clock.now()
        v = self.active_version()
        scan_id = f"{mode}-{now.strftime('%Y%m%d-%H%M%S')}"
        with self.store.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO scans(scan_id,mode,config_version,status,started,updated,body) "
                            "VALUES(?,?,?,?,?,?,?)", (scan_id, mode, v, "RUNNING", iso(now), iso(now), "{}"))
            if cur.rowcount == 1:
                self.store.emit(c, "ScanRequested", "scheduler", now, {"as_of": iso(now)}, mode=mode,
                                scan_id=scan_id, config_version=v, event_id=f"{scan_id}:requested")
                c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES(?,?,?,?)",
                          (f"last_scan:{mode}", iso(now), iso(now), "scheduler"))
        return scan_id

    def schedule(self, now):
        cfg = self.cfg_now()
        out = []
        if not self.cal.in_session(now):
            return out
        for mode in cfg["shared"]["mode_priority"]:
            if mode not in self.modes:
                continue
            last = self.store.control(f"last_scan:{mode}")
            cad = cfg["profiles"][mode]["scan_cadence_s"]
            if last is None or (now - parse_ts(last)).total_seconds() >= cad:
                out.append(self.request_scan(mode, now))
        return out

    # ------------------------------------------------------------ driving
    def consume(self, agent, now, limit=200):
        n = 0
        for ev in self.store.pending(agent.name, agent.consumes, limit):
            try:
                with self.store.tx() as c:
                    if not self.store.claim(c, agent.name, ev["event_id"], now):
                        continue
                    agent.handle(c, ev, now)
                n += 1
            except Exception as e:  # noqa: BLE001 -- rolled back; retried, then dead-lettered
                self.store.record_failure(agent.name, ev["event_id"], f"{type(e).__name__}: {e}")
                if os.environ.get("SF_RAISE"):
                    raise
        return n

    def step(self, now=None):
        now = now or self.clock.now()
        exit_a, mon, entry = self.by_name["7-paper-exit"], self.by_name["6-position-monitor"], self.by_name["5-futures-entry"]
        work = 0
        self.broker.step(now)
        work += self.consume(exit_a, now)
        mon.poll(now)
        work += self.consume(exit_a, now)
        exit_a.poll(now)
        entry.poll(now)
        for ag in self.a[:5]:
            work += self.consume(ag, now)
        return work

    def run_until_idle(self, now=None, max_passes=30):
        now = now or self.clock.now()
        for _ in range(max_passes):
            before = self.store.q1("SELECT COUNT(*) n FROM events")["n"] + self.store.q1("SELECT COUNT(*) n FROM fills")["n"]
            self.step(now)
            after = self.store.q1("SELECT COUNT(*) n FROM events")["n"] + self.store.q1("SELECT COUNT(*) n FROM fills")["n"]
            if after == before:
                return
        return

    def tick(self, now=None):
        now = now or self.clock.now()
        self.schedule(now)
        self.run_until_idle(now)

    # ------------------------------------------------------------ threads
    def start(self, poll_s=2.0):
        def loop(fn, every):
            while not self._stop.is_set():
                try:
                    fn(self.clock.now())
                except Exception as e:  # noqa: BLE001
                    print(f"[sf] {fn.__qualname__}: {type(e).__name__}: {e}")
                self._stop.wait(every)
        jobs = [(self.schedule, poll_s), (self.broker.step, poll_s)]
        for ag in self.a:
            if ag.consumes:
                jobs.append((lambda now, ag=ag: self.consume(ag, now), poll_s / 2))
            jobs.append((ag.poll, poll_s))
        for fn, every in jobs:
            th = threading.Thread(target=loop, args=(fn, every), daemon=True,
                                  name=f"sf-{getattr(fn, '__qualname__', 'job')}")
            th.start()
            self._threads.append(th)

    def stop(self, timeout=5):
        self._stop.set()
        for th in self._threads:
            th.join(timeout)

"""Durable state and the event log, in one SQLite file.

Why SQLite: the repo has no queue or database (state is JSON files and
Parquet), the server is a single small box, and the stock-futures workers
need atomic "change state + publish event + mark input consumed" steps that
survive a restart. SQLite in WAL mode gives that with no new service.

Guarantees the agents rely on:
  * events.event_id is UNIQUE -- a duplicate publish is a no-op;
  * consumed(consumer, event_id) is the PRIMARY KEY -- a consumer handles an
    event at most once, and the mark is written in the SAME transaction as
    the handler's effects, so a crash mid-handler leaves neither;
  * reservations.underlying is the PRIMARY KEY -- at most one active entry or
    position per underlying across both modes and directions, enforced by the
    database rather than by a check-then-act race;
  * exit_claims.trade_id is the PRIMARY KEY -- an exit is claimed once;
  * orders.client_order_id is the PRIMARY KEY -- stable ids make a retried
    submission idempotent.
Every write goes through tx(), which takes the write lock up front
(BEGIN IMMEDIATE), so competing workers serialise instead of interleaving.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from sf_core import SCHEMA_VERSION, config_hash, iso

DDL = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL, schema_version INTEGER NOT NULL,
  scan_id TEXT, trade_id TEXT, mode TEXT, config_version INTEGER,
  direction TEXT, producer TEXT NOT NULL, instrument_id TEXT,
  event_time TEXT NOT NULL, processed_time TEXT NOT NULL,
  payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ev_type ON events(type, seq);
CREATE INDEX IF NOT EXISTS ev_trade ON events(trade_id);
CREATE INDEX IF NOT EXISTS ev_scan ON events(scan_id);
CREATE TABLE IF NOT EXISTS consumed(consumer TEXT, event_id TEXT, at TEXT,
  PRIMARY KEY(consumer, event_id));
CREATE TABLE IF NOT EXISTS failures(consumer TEXT, event_id TEXT, attempts INTEGER,
  last_error TEXT, PRIMARY KEY(consumer, event_id));
CREATE TABLE IF NOT EXISTS configs(version INTEGER PRIMARY KEY AUTOINCREMENT,
  hash TEXT UNIQUE, created TEXT, actor TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, value TEXT, updated TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS scans(scan_id TEXT PRIMARY KEY, mode TEXT, config_version INTEGER,
  status TEXT, started TEXT, updated TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS reservations(underlying TEXT PRIMARY KEY, trade_id TEXT UNIQUE,
  mode TEXT, direction TEXT, notional REAL, margin REAL, created TEXT);
CREATE TABLE IF NOT EXISTS trades(trade_id TEXT PRIMARY KEY, scan_id TEXT, mode TEXT,
  direction TEXT, underlying TEXT, instrument_id TEXT, status TEXT, config_version INTEGER,
  opened TEXT, closed TEXT, updated TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS orders(client_order_id TEXT PRIMARY KEY, trade_id TEXT,
  instrument_id TEXT, side TEXT, purpose TEXT, direction TEXT, status TEXT,
  requested_qty INTEGER, filled_qty INTEGER, created TEXT, updated TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, client_order_id TEXT, trade_id TEXT,
  side TEXT, purpose TEXT, qty INTEGER, price REAL, ts TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, kind TEXT, trade_id TEXT,
  scan_id TEXT, status TEXT, deadline TEXT, created TEXT, decided TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS exit_claims(trade_id TEXT PRIMARY KEY, reason TEXT, claimed_by TEXT,
  at TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT,
  action TEXT, trade_id TEXT, outcome TEXT, body TEXT);
"""

MAX_ATTEMPTS = 5


class Store:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self.con().executescript(DDL)

    # ------------------------------------------------------------ plumbing
    def con(self):
        c = getattr(self._local, "con", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            if self.path != ":memory:":
                c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.con = c
        return c

    @contextmanager
    def tx(self):
        c = self.con()
        if c.in_transaction:            # nested: join the outer transaction
            yield c
            return
        for attempt in range(50):
            try:
                c.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.02)
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def q(self, sql, *a):
        return [dict(r) for r in self.con().execute(sql, a).fetchall()]

    def q1(self, sql, *a):
        r = self.con().execute(sql, a).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ events
    def emit(self, c, type_, producer, now, payload, mode=None, scan_id=None, trade_id=None,
             direction=None, instrument_id=None, config_version=None, event_id=None,
             event_time=None):
        """Append an event inside the caller's transaction. Returns the id.
        A deterministic event_id makes the publish idempotent."""
        eid = event_id or uuid.uuid4().hex
        c.execute("""INSERT OR IGNORE INTO events(event_id,type,schema_version,scan_id,trade_id,mode,
                     config_version,direction,producer,instrument_id,event_time,processed_time,payload)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (eid, type_, SCHEMA_VERSION, scan_id, trade_id, mode, config_version, direction,
                   producer, instrument_id, iso(event_time or now), iso(now),
                   json.dumps(payload, default=str, sort_keys=True)))
        return eid

    def pending(self, consumer, types, limit=200):
        ph = ",".join("?" * len(types))
        rows = self.q(f"""SELECT e.* FROM events e WHERE e.type IN ({ph})
                          AND NOT EXISTS (SELECT 1 FROM consumed k WHERE k.consumer=? AND k.event_id=e.event_id)
                          AND COALESCE((SELECT attempts FROM failures f WHERE f.consumer=? AND f.event_id=e.event_id),0) < ?
                          ORDER BY e.seq LIMIT ?""", *types, consumer, consumer, MAX_ATTEMPTS, limit)
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def claim(self, c, consumer, event_id, now):
        """True if this consumer had not handled the event; marks it handled."""
        cur = c.execute("INSERT OR IGNORE INTO consumed(consumer,event_id,at) VALUES(?,?,?)",
                        (consumer, event_id, iso(now)))
        return cur.rowcount == 1

    def record_failure(self, consumer, event_id, err):
        with self.tx() as c:
            c.execute("""INSERT INTO failures(consumer,event_id,attempts,last_error) VALUES(?,?,1,?)
                         ON CONFLICT(consumer,event_id) DO UPDATE SET attempts=attempts+1, last_error=excluded.last_error""",
                      (consumer, event_id, err[:500]))

    def events(self, where="1=1", *a):
        rows = self.q(f"SELECT * FROM events WHERE {where} ORDER BY seq", *a)
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    # ------------------------------------------------------------ config
    def put_config(self, cfg, now, actor="system"):
        h = config_hash(cfg)
        with self.tx() as c:
            r = c.execute("SELECT version FROM configs WHERE hash=?", (h,)).fetchone()
            if r:
                return r[0]
            c.execute("INSERT INTO configs(hash,created,actor,body) VALUES(?,?,?,?)",
                      (h, iso(now), actor, json.dumps(cfg, sort_keys=True)))
            v = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES('active_config',?,?,?)",
                      (str(v), iso(now), actor))
            return v

    def activate_config(self, version, now, actor):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES('active_config',?,?,?)",
                      (str(version), iso(now), actor))

    def config(self, version=None):
        if version is None:
            version = int(self.control("active_config") or 0)
        r = self.q1("SELECT * FROM configs WHERE version=?", version)
        return (version, json.loads(r["body"])) if r else (None, None)

    # ------------------------------------------------------------ control
    def control(self, key, default=None):
        r = self.q1("SELECT value FROM control WHERE key=?", key)
        return r["value"] if r else default

    def set_control(self, key, value, now, actor):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES(?,?,?,?)",
                      (key, str(value), iso(now), actor))
            c.execute("INSERT INTO audit(at,actor,action,trade_id,outcome,body) VALUES(?,?,?,?,?,?)",
                      (iso(now), actor, f"set {key}", None, "applied", json.dumps({"value": value})))

    def paused(self, mode):
        return self.control("pause:global") == "1" or self.control(f"pause:{mode}") == "1"

    # ------------------------------------------------------------ records
    def trade(self, trade_id):
        r = self.q1("SELECT * FROM trades WHERE trade_id=?", trade_id)
        if r:
            r["body"] = json.loads(r["body"])
        return r

    def trades(self, where="1=1", *a):
        rows = self.q(f"SELECT * FROM trades WHERE {where} ORDER BY opened, trade_id", *a)
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def put_trade(self, c, t, now):
        c.execute("""INSERT INTO trades(trade_id,scan_id,mode,direction,underlying,instrument_id,status,
                     config_version,opened,closed,updated,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(trade_id) DO UPDATE SET status=excluded.status, instrument_id=excluded.instrument_id,
                     opened=excluded.opened, closed=excluded.closed, updated=excluded.updated, body=excluded.body""",
                  (t["trade_id"], t.get("scan_id"), t["mode"], t["direction"], t["underlying"],
                   t.get("instrument_id"), t["status"], t["config_version"], t.get("opened_at"),
                   t.get("closed_at"), iso(now), json.dumps(t, default=str, sort_keys=True)))

    def orders(self, where="1=1", *a):
        rows = self.q(f"SELECT * FROM orders WHERE {where} ORDER BY created, client_order_id", *a)
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def put_order(self, c, o, now):
        c.execute("""INSERT INTO orders(client_order_id,trade_id,instrument_id,side,purpose,direction,status,
                     requested_qty,filled_qty,created,updated,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(client_order_id) DO UPDATE SET status=excluded.status,
                     filled_qty=excluded.filled_qty, updated=excluded.updated, body=excluded.body""",
                  (o["client_order_id"], o["trade_id"], o["instrument_id"], o["side"], o["purpose"],
                   o["direction"], o["status"], o["requested_qty"], o["filled_qty"], o["created"],
                   iso(now), json.dumps(o, default=str, sort_keys=True)))

    def fills(self, where="1=1", *a):
        rows = self.q(f"SELECT * FROM fills WHERE {where} ORDER BY ts, fill_id", *a)
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def audit(self, c, now, actor, action, trade_id, outcome, body):
        c.execute("INSERT INTO audit(at,actor,action,trade_id,outcome,body) VALUES(?,?,?,?,?,?)",
                  (iso(now), actor, action, trade_id, outcome, json.dumps(body, default=str)))

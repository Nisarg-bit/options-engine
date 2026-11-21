"""Strategy library, approvals and durable research jobs (data/futures/library.db).

Tables
  strategies   id, name, description, tags, mode, lifecycle, evidence, provenance, created, updated
  versions     version_id (content hash, immutable), strategy_id, n, body, created, actor, parent, note
  suggestions  id, strategy_id, version_id, kind, status, evidence, evidence_hash, lock_changes, notified_hash ...
  approvals    id, suggestion_id, version_id, evidence_hash, action, mode, lock_changes_approved, actor, at, used_at
  activations  id, approval_id, version_id, action, mode, from_active, to_active, at, actor
  sources      id, url, publisher, author, published, retrieved, idea, cycle_id, query, provider
  jobs         id, kind, status, idem_key UNIQUE, correlation_id, payload, progress, attempts, error, ...
  events       seq, type, correlation_id, strategy_id, version_id, payload, at
  state        key, value  (research cadence: last_processed_session, cycle counter)

Lifecycle (what a strategy is allowed to do) and evidence (how much we know) are
separate fields:
  lifecycle  DRAFT -> SHADOW -> ACTIVE; ACTIVE -> DISABLED; DISABLED -> SHADOW;
             any -> ARCHIVED; ARCHIVED -> DRAFT (restore). ACTIVE only through
             sf_approval.activate (needs a recorded approval).
  evidence   INSUFFICIENT | EARLY | EVALUATED (research_policy.json labels)

Edits never mutate a version: they create a new version (lifecycle DRAFT until
activated). Disabling/archiving stops NEW entries only; open positions keep
their entry config and exit plan (they are pinned in the trading store).
"""
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

import project_paths
import sf_strategy as SS
from sf_core import IST, iso

DB = os.environ.get("SF_LIBRARY") or os.path.join(project_paths.ROOT, "data", "futures", "library.db")
HERE = os.path.dirname(os.path.abspath(__file__))
POLICY = os.environ.get("SF_RESEARCH_POLICY") or next(
    (p for p in (os.path.join(HERE, "config", "research_policy.json"), os.path.join(HERE, "futures_research_policy.json"))
     if os.path.exists(p)), os.path.join(HERE, "config", "research_policy.json"))

LIFECYCLE = ("DRAFT", "SHADOW", "ACTIVE", "DISABLED", "ARCHIVED")
TRANSITIONS = {"DRAFT": {"SHADOW", "ARCHIVED"}, "SHADOW": {"DRAFT", "DISABLED", "ARCHIVED"},
               "ACTIVE": {"DISABLED", "ARCHIVED"}, "DISABLED": {"SHADOW", "ARCHIVED"},
               "ARCHIVED": {"DRAFT"}}
EVIDENCE = ("INSUFFICIENT", "EARLY", "EVALUATED")
SUGGESTION_STATUS = ("OPEN", "REPLACED", "ALONGSIDE", "CONTINUE", "DISMISSED", "SUPERSEDED")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, name TEXT, description TEXT, tags TEXT, mode TEXT,
  lifecycle TEXT, evidence TEXT, provenance TEXT, created TEXT, updated TEXT, head_version TEXT);
CREATE TABLE IF NOT EXISTS versions(version_id TEXT PRIMARY KEY, strategy_id TEXT, n INTEGER, body TEXT,
  structure_hash TEXT, created TEXT, actor TEXT, parent TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS versions_by_strategy ON versions(strategy_id, n);
CREATE TABLE IF NOT EXISTS suggestions(id TEXT PRIMARY KEY, strategy_id TEXT, version_id TEXT, kind TEXT,
  status TEXT, title TEXT, evidence TEXT, evidence_hash TEXT, lock_changes TEXT, created TEXT, updated TEXT,
  decided_at TEXT, decided_by TEXT, decision_note TEXT, reconsider TEXT, notified_hash TEXT, cycle_id TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, suggestion_id TEXT, version_id TEXT, evidence_hash TEXT,
  action TEXT, mode TEXT, priority INTEGER, lock_changes_approved TEXT, actor TEXT, at TEXT, used_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS activations(id TEXT PRIMARY KEY, approval_id TEXT UNIQUE, version_id TEXT, action TEXT,
  mode TEXT, from_active TEXT, to_active TEXT, at TEXT, actor TEXT, config_version INTEGER);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, url TEXT, title TEXT, publisher TEXT, author TEXT,
  published TEXT, retrieved TEXT, idea TEXT, cycle_id TEXT, query TEXT, provider TEXT, fixture INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, status TEXT, idem_key TEXT UNIQUE,
  correlation_id TEXT, payload TEXT, progress TEXT, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  error TEXT, created TEXT, started TEXT, finished TEXT, heartbeat TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, correlation_id TEXT,
  strategy_id TEXT, version_id TEXT, payload TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT, updated TEXT);
"""

JOB_KINDS = ("research_cycle", "replay", "evaluate", "shadow_start")
EVENT_TYPES = ("ResearchRequested", "ResearchStarted", "IdeaDiscovered", "IdeaRejected", "CandidateCompiled",
               "CandidateDuplicate", "EvaluationCompleted", "EvaluationFailed", "SuggestionCreated",
               "SuggestionUpdated", "SuggestionDecided", "ApprovalRecorded", "StrategyActivated",
               "StrategyDeactivated", "ActivationRefused", "StrategyCreated", "StrategyVersioned",
               "LifecycleChanged", "ResearchCompleted", "ResearchFailed", "EntriesPausedAutomatically",
               "TemplateImported")


def load_policy(path=None):
    with open(path or POLICY, encoding="utf-8") as f:
        return json.load(f)


def now_iso():
    return iso(datetime.now(IST))


class Library:
    def __init__(self, path=None):
        self.path = path or DB
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA_SQL)          # executescript commits on its own

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def q(self, sql, *a):
        return [dict(r) for r in self.conn().execute(sql, a).fetchall()]

    def q1(self, sql, *a):
        r = self.conn().execute(sql, a).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ events
    def emit(self, c, type_, payload=None, correlation_id=None, strategy_id=None, version_id=None):
        assert type_ in EVENT_TYPES, type_
        c.execute("INSERT INTO events(type,correlation_id,strategy_id,version_id,payload,at) VALUES(?,?,?,?,?,?)",
                  (type_, correlation_id, strategy_id, version_id, json.dumps(payload or {}, default=str), now_iso()))

    def events(self, limit=200, **where):
        sql, args = "SELECT * FROM events", []
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
            args = list(where.values())
        rows = self.q(sql + " ORDER BY seq DESC LIMIT ?", *args, limit)
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return rows

    # ------------------------------------------------------------ state
    def get(self, key, default=None):
        r = self.q1("SELECT value FROM state WHERE key=?", key)
        return json.loads(r["value"]) if r else default

    def put(self, c, key, value):
        c.execute("INSERT OR REPLACE INTO state(key,value,updated) VALUES(?,?,?)", (key, json.dumps(value), now_iso()))

    # ------------------------------------------------------------ strategies
    def create(self, body, actor, provenance, lifecycle="DRAFT", note="created", strategy_id=None):
        """New strategy with its first version. provenance: {"kind": user|agent|imported, ...}."""
        v = SS.validate(body)
        sid = strategy_id or f"st-{uuid.uuid4().hex[:10]}"
        vid = SS.version_hash(body)
        same = self.q1("SELECT strategy_id, n FROM versions WHERE version_id=?", vid)
        if same:
            raise ValueError(f"identical to version {same['n']} of strategy {same['strategy_id']} ({vid}): "
                             "change something (even the name) to save a new strategy")
        with self.tx() as c:
            c.execute("INSERT INTO strategies(id,name,description,tags,mode,lifecycle,evidence,provenance,created,updated,"
                      "head_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, body.get("name"), body.get("description"), json.dumps(body.get("tags") or []),
                       body.get("mode"), lifecycle, "INSUFFICIENT", json.dumps(provenance), now_iso(), now_iso(), vid))
            self._put_version(c, sid, body, actor, None, note)
            self.emit(c, "StrategyCreated", {"name": body.get("name"), "valid": v["ok"], "errors": v["errors"],
                                             "provenance": provenance}, strategy_id=sid, version_id=vid)
        return sid, vid

    def _put_version(self, c, sid, body, actor, parent, note):
        vid = SS.version_hash(body)
        if c.execute("SELECT 1 FROM versions WHERE version_id=?", (vid,)).fetchone():
            return vid
        n = (c.execute("SELECT MAX(n) FROM versions WHERE strategy_id=?", (sid,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO versions(version_id,strategy_id,n,body,structure_hash,created,actor,parent,note) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (vid, sid, n, SS.canonical(body), SS.structure_hash(body), now_iso(),
                                                actor, parent, note))
        return vid

    def edit(self, sid, body, actor, note="edited"):
        """A new version. An edit of an ACTIVE strategy makes a DRAFT copy, never
        changing what trades: activation of the edit needs approval."""
        st = self.strategy(sid)
        if st is None:
            raise KeyError(sid)
        if st["lifecycle"] == "ARCHIVED":
            raise ValueError("restore the strategy before editing it")
        if SS.version_hash(body) == st["head_version"]:
            return sid, st["head_version"]                  # nothing changed: no new version, no copy
        if st["lifecycle"] == "ACTIVE":
            nsid, vid = self.create(body, actor, {"kind": "user", "copied_from": sid, "from_version": st["head_version"]},
                                    note=f"draft edit of active strategy {sid}")
            return nsid, vid
        with self.tx() as c:
            vid = self._put_version(c, sid, body, actor, st["head_version"], note)
            c.execute("UPDATE strategies SET head_version=?, name=?, description=?, tags=?, mode=?, updated=?, "
                      "lifecycle=CASE WHEN lifecycle='SHADOW' THEN 'DRAFT' ELSE lifecycle END WHERE id=?",
                      (vid, body.get("name"), body.get("description"), json.dumps(body.get("tags") or []),
                       body.get("mode"), now_iso(), sid))
            self.emit(c, "StrategyVersioned", {"note": note}, strategy_id=sid, version_id=vid)
        return sid, vid

    def duplicate(self, sid, actor, name=None, version_id=None):
        body = json.loads(self.version(version_id or self.strategy(sid)["head_version"])["body"])
        body["name"] = name or f"{body.get('name')} (copy)"
        return self.create(body, actor, {"kind": "user", "copied_from": sid, "from_version": version_id})

    def restore_as_new(self, sid, version_id, actor):
        body = json.loads(self.version(version_id)["body"])
        st = self.strategy(sid)
        if st["lifecycle"] == "ARCHIVED":
            self.set_lifecycle(sid, "DRAFT", actor, "restored")
        body.setdefault("tags", [])
        body["description"] = (body.get("description") or "") + f" (restored from {version_id})"
        return self.edit(sid, body, actor, note=f"restored from version {version_id}")

    def set_lifecycle(self, sid, new, actor, reason=""):
        """Lifecycle change other than ACTIVE (which only sf_approval may set)."""
        st = self.strategy(sid)
        if new == "ACTIVE":
            raise PermissionError("ACTIVE is set only by an approved activation (sf_approval.activate)")
        if new not in TRANSITIONS.get(st["lifecycle"], set()):
            raise ValueError(f"{st['lifecycle']} -> {new} is not an allowed transition")
        with self.tx() as c:
            c.execute("UPDATE strategies SET lifecycle=?, updated=? WHERE id=?", (new, now_iso(), sid))
            self.emit(c, "LifecycleChanged", {"from": st["lifecycle"], "to": new, "reason": reason, "actor": actor},
                      strategy_id=sid, version_id=st["head_version"])

    def _force_lifecycle(self, c, sid, new, why):
        cur = c.execute("SELECT lifecycle, head_version FROM strategies WHERE id=?", (sid,)).fetchone()
        c.execute("UPDATE strategies SET lifecycle=?, updated=? WHERE id=?", (new, now_iso(), sid))
        self.emit(c, "LifecycleChanged", {"from": cur[0] if cur else None, "to": new, "reason": why},
                  strategy_id=sid, version_id=cur[1] if cur else None)

    def rename(self, sid, name, actor):
        body = json.loads(self.version(self.strategy(sid)["head_version"])["body"])
        body["name"] = name
        return self.edit(sid, body, actor, note=f"renamed to {name}")

    def set_evidence(self, sid, label):
        assert label in EVIDENCE
        with self.tx() as c:
            c.execute("UPDATE strategies SET evidence=?, updated=? WHERE id=?", (label, now_iso(), sid))

    def strategy(self, sid):
        r = self.q1("SELECT * FROM strategies WHERE id=?", sid)
        if r:
            r["tags"] = json.loads(r["tags"] or "[]")
            r["provenance"] = json.loads(r["provenance"] or "{}")
        return r

    def strategies(self, include_archived=True):
        rows = self.q("SELECT * FROM strategies ORDER BY updated DESC")
        out = []
        for r in rows:
            if not include_archived and r["lifecycle"] == "ARCHIVED":
                continue
            r["tags"] = json.loads(r["tags"] or "[]")
            r["provenance"] = json.loads(r["provenance"] or "{}")
            out.append(r)
        return out

    def version(self, vid):
        return self.q1("SELECT * FROM versions WHERE version_id=?", vid)

    def body(self, vid):
        v = self.version(vid)
        return json.loads(v["body"]) if v else None

    def versions(self, sid):
        return self.q("SELECT version_id, n, created, actor, parent, note, structure_hash FROM versions "
                      "WHERE strategy_id=? ORDER BY n", sid)

    def find_structure(self, sh):
        return self.q1("SELECT * FROM versions WHERE structure_hash=? ORDER BY created LIMIT 1", sh)

    # ------------------------------------------------------------ suggestions
    def upsert_suggestion(self, strategy_id, version_id, kind, title, evidence, lock_changes, cycle_id):
        """One suggestion per version; new evidence updates it. Returns (id, should_notify).
        A DISMISSED suggestion is only re-opened when its reconsider preference
        allows it and the evidence changed meaningfully."""
        eh = SS.version_hash({"e": evidence.get("summary_key") or evidence})
        cur = self.q1("SELECT * FROM suggestions WHERE version_id=?", version_id)
        with self.tx() as c:
            if cur is None:
                sid = f"sg-{uuid.uuid4().hex[:10]}"
                c.execute("INSERT INTO suggestions(id,strategy_id,version_id,kind,status,title,evidence,evidence_hash,"
                          "lock_changes,created,updated,cycle_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, strategy_id, version_id, kind, "OPEN", title, json.dumps(evidence, default=str), eh,
                           json.dumps(lock_changes, default=str), now_iso(), now_iso(), cycle_id))
                self.emit(c, "SuggestionCreated", {"title": title, "kind": kind}, cycle_id, strategy_id, version_id)
                return sid, True
            if cur["evidence_hash"] == eh:
                return cur["id"], False
            status, notify = cur["status"], True
            if status == "DISMISSED":
                pref = cur["reconsider"] or "on_new_evidence"
                notify = pref == "on_new_evidence"
                status = "OPEN" if notify else "DISMISSED"
            elif status in ("REPLACED", "ALONGSIDE"):
                notify = False
            c.execute("UPDATE suggestions SET evidence=?, evidence_hash=?, lock_changes=?, status=?, updated=?, "
                      "cycle_id=? WHERE id=?", (json.dumps(evidence, default=str), eh, json.dumps(lock_changes, default=str),
                                                status, now_iso(), cycle_id, cur["id"]))
            self.emit(c, "SuggestionUpdated", {"title": title, "reopened": status == "OPEN" and cur["status"] == "DISMISSED"},
                      cycle_id, strategy_id, version_id)
            return cur["id"], notify

    def suggestion(self, sid):
        r = self.q1("SELECT * FROM suggestions WHERE id=?", sid)
        if r:
            r["evidence"] = json.loads(r["evidence"] or "{}")
            r["lock_changes"] = json.loads(r["lock_changes"] or "[]")
        return r

    def suggestions(self, status=None):
        rows = self.q("SELECT * FROM suggestions" + (" WHERE status=?" if status else "") + " ORDER BY updated DESC",
                      *([status] if status else []))
        for r in rows:
            r["evidence"] = json.loads(r["evidence"] or "{}")
            r["lock_changes"] = json.loads(r["lock_changes"] or "[]")
        return rows

    def decide(self, suggestion_id, status, actor, note=None, reconsider=None):
        assert status in ("CONTINUE", "DISMISSED")
        with self.tx() as c:
            c.execute("UPDATE suggestions SET status=?, decided_at=?, decided_by=?, decision_note=?, reconsider=?, "
                      "updated=? WHERE id=?", (status, now_iso(), actor, note, reconsider, now_iso(), suggestion_id))
            s = c.execute("SELECT strategy_id, version_id FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
            self.emit(c, "SuggestionDecided", {"status": status, "note": note, "reconsider": reconsider, "actor": actor},
                      None, s[0] if s else None, s[1] if s else None)

    # ------------------------------------------------------------ jobs
    def enqueue(self, kind, idem_key, payload=None, correlation_id=None, max_attempts=3):
        """Idempotent: the same idem_key never creates a second job. Returns (job, created)."""
        assert kind in JOB_KINDS
        cid = correlation_id or f"corr-{uuid.uuid4().hex[:10]}"
        with self.tx() as c:
            cur = c.execute("SELECT * FROM jobs WHERE idem_key=?", (idem_key,)).fetchone()
            if cur:
                return dict(cur), False
            jid = f"job-{uuid.uuid4().hex[:10]}"
            c.execute("INSERT INTO jobs(id,kind,status,idem_key,correlation_id,payload,progress,attempts,max_attempts,"
                      "created) VALUES(?,?,?,?,?,?,?,?,?,?)", (jid, kind, "QUEUED", idem_key, cid,
                                                               json.dumps(payload or {}), json.dumps({"step": "queued"}),
                                                               0, max_attempts, now_iso()))
            self.emit(c, "ResearchRequested" if kind == "research_cycle" else "ResearchRequested",
                      {"job": jid, "kind": kind, "idem_key": idem_key, **(payload or {})}, cid)
        return self.q1("SELECT * FROM jobs WHERE id=?", jid), True

    def active_job(self, kind):
        return self.q1("SELECT * FROM jobs WHERE kind=? AND status IN ('QUEUED','RUNNING') ORDER BY created LIMIT 1", kind)

    def claim(self, kind, worker, stale_s=1800):
        """Take the oldest queued job (or a RUNNING one whose heartbeat is stale:
        restart recovery). At most one RUNNING job per kind."""
        with self.tx() as c:
            run = c.execute("SELECT * FROM jobs WHERE kind=? AND status='RUNNING' ORDER BY started LIMIT 1", (kind,)).fetchone()
            if run:
                hb = run["heartbeat"] or run["started"]
                age = (datetime.now(IST) - datetime.fromisoformat(hb)).total_seconds() if hb else 1e9
                if age < stale_s:
                    return None                     # one at a time; it is alive
                job = dict(run)
            else:
                r = c.execute("SELECT * FROM jobs WHERE kind=? AND status='QUEUED' ORDER BY created LIMIT 1", (kind,)).fetchone()
                if not r:
                    return None
                job = dict(r)
            c.execute("UPDATE jobs SET status='RUNNING', started=COALESCE(started,?), heartbeat=?, attempts=attempts+1 "
                      "WHERE id=?", (now_iso(), now_iso(), job["id"]))
        return self.q1("SELECT * FROM jobs WHERE id=?", job["id"])

    def progress(self, jid, **p):
        with self.tx() as c:
            c.execute("UPDATE jobs SET progress=?, heartbeat=? WHERE id=?", (json.dumps(p, default=str), now_iso(), jid))

    def finish(self, jid, ok, result=None, error=None):
        with self.tx() as c:
            j = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (jid,)).fetchone()
            status = "DONE" if ok else ("FAILED" if j and j[0] >= j[1] else "QUEUED")
            c.execute("UPDATE jobs SET status=?, finished=?, result=?, error=? WHERE id=?",
                      (status, now_iso() if status != "QUEUED" else None, json.dumps(result or {}, default=str),
                       error, jid))
        return status

    def jobs(self, limit=30):
        rows = self.q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", limit)
        for r in rows:
            for k in ("payload", "progress", "result"):
                r[k] = json.loads(r[k] or "{}")
        return rows

    # ------------------------------------------------------------ sources
    def add_source(self, src, cycle_id):
        sid = "src-" + SS.version_hash({"u": src.get("url"), "i": src.get("idea")})
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO sources(id,url,title,publisher,author,published,retrieved,idea,cycle_id,query,"
                      "provider,fixture) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, src.get("url"), src.get("title"), src.get("publisher"), src.get("author"),
                       src.get("published"), src.get("retrieved"), src.get("idea"), cycle_id, src.get("query"),
                       src.get("provider"), 1 if src.get("fixture") else 0))
        return sid

    def sources(self, cycle_id=None):
        return self.q("SELECT * FROM sources" + (" WHERE cycle_id=?" if cycle_id else "") + " ORDER BY retrieved DESC",
                      *([cycle_id] if cycle_id else []))


def evidence_label(replay, forward, policy):
    """INSUFFICIENT / EARLY / EVALUATED from replay and forward counts (labels only)."""
    lb = policy["evidence"]["labels"]
    r_s, r_t = replay.get("sessions", 0), replay.get("settled", 0)
    f_s, f_t = forward.get("sessions", 0), forward.get("settled", 0)
    if (r_s >= lb["evaluated_min_sessions"] and r_t >= lb["evaluated_min_trades"]) or \
            (f_s >= lb["evaluated_min_sessions"] and f_t >= lb["evaluated_min_trades"]):
        return "EVALUATED"
    if r_t < lb["insufficient_max_trades"] and f_s < lb["insufficient_max_forward_sessions"]:
        return "INSUFFICIENT"
    return "EARLY"


def import_templates(lib, cfg, actor="import"):
    """The deployed strategy definitions as named, versioned templates (idempotent).
    The ACTIVE lifecycle is given to the deployed v1 template because it IS what
    trades today; that is a recorded import, not an activation of something new."""
    tpls, diffs = SS.templates_from_config(cfg)
    out = []
    for t in tpls:
        sh = SS.structure_hash(t)
        hit = lib.find_structure(sh)
        if hit:
            out.append((hit["strategy_id"], hit["version_id"], False))
            continue
        sid, vid = lib.create(t, actor, {"kind": "imported", "from": "deployed futures config v1",
                                         "differences": diffs.get(t["name"], [])},
                              lifecycle="ACTIVE", note="imported from the deployed config",
                              strategy_id=f"tpl-{t['mode']}-v1")
        with lib.tx() as c:
            lib.emit(c, "TemplateImported", {"differences": diffs.get(t["name"], [])}, strategy_id=sid, version_id=vid)
        out.append((sid, vid, True))
    return out

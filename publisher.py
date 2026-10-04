"""Push the dashboard's state into Firebase Realtime Database.

Why this exists: Firebase Hosting serves static files and nothing else, so a
`.web.app` page cannot call the FastAPI process on this box. Instead this
process -- which runs HERE, next to the data -- computes exactly what the API
would return and writes it to RTDB. The page subscribes and updates the moment
a write lands. No ports opened, no server for the page to depend on, and the
page keeps working if this box is rebooted (it just shows older data, honestly
labelled).

It imports api.py rather than reimplementing anything. Two implementations of
the same numbers is how a dashboard ends up disagreeing with the Telegram
signal, and the whole point of this project is that the record is trustworthy.

Free-tier arithmetic, because it is the constraint that shapes the cadence:
RTDB's free plan allows 10 GB of download a month. Every subscriber pulls a
node again whenever that node changes, so a big node on a fast cadence is what
costs. Hence the split below -- the chain and structures move every minute,
the intraday series every five, the journal every fifteen. Roughly 90 KB per
minute-tick per viewer works out under a gigabyte a month for one person
watching all session, with plenty of headroom for people you share it with.

`series` and `buildup` are published in COMPACT form for the same reason.
Both derive from the same per-minute parquet read, so neither can change
faster than the collector flushes -- about every five minutes -- and
changed() suppresses the writes in between. Compact drops the per-row legs
from the series (identical on every row of a fixed anchor: bytes, not
information) and the full per-strike tables from the buildup, keeping the
ranked rows, the whole-chain agreement figures and the tilt decomposition.
Measured on synthetic chains that is roughly 55% and 23% of full.

The series is published as a fixed MENU of variants, because a static host
cannot pass query parameters: whatever is not published here cannot be
charted on the Firebase route. Over the API every combination is free.

Usage:
    python publisher.py --once     compute and write once, print sizes
    python publisher.py            loop forever (what systemd runs)
    python publisher.py --dry      compute and print sizes, write nothing
"""

import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import api
import daytrade_api
import series_api
import term_api

IST = timezone(timedelta(hours=5, minutes=30))

CRED = os.environ.get("FIREBASE_CREDENTIALS",
                      "/home/ubuntu/.firebase-service-account.json")
DB_URL = os.environ.get("FIREBASE_DB_URL", "").strip()

ROOT = "/live"

# A COMPLETED session's parquet never changes again, so its payloads can be
# written once and never rewritten. That is the whole trick behind replay on
# the published route: this root is not part of the thirty-second cycle at
# all. /live stays the ~400 KB it already is, and /archive is only ever read
# when somebody actually picks a date.
ARCHIVE = "/archive"

# One session per pass, deliberately. The first run has every collected
# session to do and each one is several chain builds against a different
# day's parquet -- none of which the caches can share -- on a 2 GB box whose
# real job is running the collector. Doing them all in one pass would stall
# the publisher for minutes at a time.
ARCHIVE_PER_PASS = 1

# What gets archived, and what does not. `series` and `buildup` are left out
# on size: series alone is ~105 KB a session against ~52 KB for everything
# here put together, and it would more than double the archive to make the
# Structure tab replay. The page says which tabs do not replay rather than
# leaving it to be discovered.
ARCHIVE_NODES = ("expiries", "chain", "structures", "analytics",
                 "intraday", "term")

# How many expiries to publish. One is enough for the signal; two lets the
# expiry dropdown work without doubling the cost of every other node.
EXPIRIES = 2

# How often each node is RECOMPUTED, in seconds, while the market is open.
# Recomputing is not writing -- see `changed()`. A node is only written when
# its bytes actually differ from what is already in the database, which is what
# makes a 10-second tick affordable.
#
# `ticker` exists because of the flush arithmetic: the option chain physically
# cannot change more often than the collector writes it, which is about every
# five minutes. Spot and VIX come from a live Kite call and genuinely move
# every tick. Separating them means the number that changes is tiny and the
# node that is large only moves when it has something new to say.
# With the collector writing a part every minute (see collector.py), the
# chain can actually change every minute, so the nodes built from it are
# recomputed every 15 seconds -- changed() still means a write only happens
# when the bytes move, which is about once a minute while the market is open.
CADENCE = {"ticker": 5, "health": 30, "chain": 15, "structures": 15,
           "analytics": 30, "scan": 120,
           "intraday": 120, "series": 120, "buildup": 120,
           "daytrade": 120,
           # `term` walks six expiries, so it is the one node whose
           # recompute is genuinely expensive: six build_chain calls
           # against two for `chain`. build_chain is cached on the
           # Parquet signature, so a cadence slower than the collector
           # flush (about five minutes) pays that cost roughly once per
           # flush instead of once per tick. The payload itself is tiny
           # -- six rows of eleven scalars -- so the write is free; it
           # is the build that is being rationed here, not the bytes.
           "term": 300,
           "journal": 900, "expiries": 900,
           "underlyings": 900,
           # Tiny, and the only way the published page can know which dates
           # it is allowed to replay.
           "sessions": 300,
           # Context from outside the chain -- see world.py. Prices every
           # minute, headlines every ten, the hand-kept calendar hourly.
           "world_prices": 60, "world_news": 600, "world_events": 3600,
           # The calibrated distribution (calpop.py): static between refits.
           "calibration": 3600,
           # The nightly post-mortem (postmortem.py, 15:55 IST): a small file
           # read, written only when it changes.
           "postmortem": 900}

# Global markets and news move while NSE is shut -- overnight is exactly when
# US futures and GIFT-style leads matter -- so these do not back off with the
# CLOSED_MULTIPLIER that everything reading the collector's bars uses.
NO_BACKOFF = {"world_prices", "world_news", "world_events",
              # produced AFTER the close -- backing off 15x would hold it
              # back for hours, exactly when it is new
              "postmortem"}

# The series variants published to Firebase. Anything absent here cannot be
# charted on the .web.app route, so this list IS the chart's configurability
# on that side -- keep it short, because every entry is paid for on every
# write, by every viewer.
#
# `atm_rolling` earns its place despite returning null indicators: it is what
# /api/intraday already plots, and having it beside the fixed-anchor line is
# how the difference between them stays visible rather than becoming a
# footnote nobody reads.
SERIES_VARIANTS = {
    "straddle_open": {"structure": "straddle", "anchor": "atm_open"},
    "strangle_open": {"structure": "strangle", "anchor": "atm_open",
                      "width": 2},
    "straddle_roll": {"structure": "straddle", "anchor": "atm_rolling"},
}

# Same thinning as intraday. 375 minutes at every=2 is 188 points, which is
# more than a 560-pixel chart can resolve anyway.
SERIES_EVERY = 2

# How many expiries get a series, against EXPIRIES for every other node.
#
# Measured, not guessed: at EXPIRIES=2 this node came to 246 KB, against 132
# for the whole chain, because it carries underlyings x expiries x variants
# -- twelve payloads where the chain carries four. RTDB re-downloads an
# entire node whenever any part of it changes, and this one changes on every
# collector flush, so that is 246 KB paid roughly 75 times a session by every
# viewer.
#
# The front expiry is the one that justifies a per-minute chart. The back
# weekly moves little intraday and nobody watches its straddle tick. Halving
# the node costs nothing anyone would notice.
SERIES_EXPIRIES = 1

# Outside market hours nothing changes, so back everything off hard rather
# than burning CPU rebuilding payloads that will be discarded as unchanged.
CLOSED_MULTIPLIER = 15


# --------------------------------------------------------------------- shape

def market_open(now=None):
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return "09:15" <= now.strftime("%H:%M") <= "15:35"


def safe(fn, *a, **kw):
    """Call an api.py endpoint, turning any failure into a labelled dict.

    A node that says why it is empty is far more useful on a public page than
    one that is silently absent -- the difference between 'the collector is
    down' and 'the dashboard is broken' is the thing you want to see at 09:00.
    """
    try:
        return fn(*a, **kw)
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e)
        return {"error": str(detail)}


_last_slow = {}      # what the last structures build said, for the ticker


def build(which):
    """The payload for one node, or None if it is not applicable right now."""
    if which == "health":
        return safe(api.health)

    if which == "ticker":
        # Deliberately cheap: one batched Kite call, no Parquet reading at all.
        # The per-underlying gate numbers are carried over from the last
        # structures build, so a 5-second tick costs one quote lookup for every
        # index at once and nothing more.
        try:
            q = api.quotes()
        except Exception as e:
            return {"error": str(e)}
        by = {}
        for u in api.underlyings():
            sym = api.INDEX_SYMBOL.get(u)
            by[u] = {"spot": round(q[sym], 2) if sym and sym in q else None,
                     **_last_slow.get(u, {})}
        return {"at": datetime.now(IST).isoformat(timespec="seconds"),
                "vix": round(q.get(api.VIX_SYMBOL), 2) if q.get(api.VIX_SYMBOL) else None,
                "by": by}

    if which == "underlyings":
        return safe(api.api_underlyings)

    if which == "postmortem":
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "postmortem", "latest.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            return {"error": f"{type(e).__name__}: {e}"}

    if which == "calibration":
        import calpop
        return calpop.public()

    if which in ("world_prices", "world_news", "world_events"):
        import world
        return safe({"world_prices": world.prices, "world_news": world.news,
                     "world_events": world.events}[which])

    if which == "sessions":
        # `archived` is what the picker can actually offer; `sessions` is
        # what exists on disk. They differ while the archive is still
        # catching up, and a picker built on the wrong one offers dates that
        # 404.
        return {"sessions": [str(d) for d in api.sessions()],
                "archived": sorted(archived())}

    if which == "journal":
        return safe(api.api_journal, limit=400)

    if which == "scan":
        # Stamped here because the scan node refreshes on its own cadence: the
        # page shows its EV beside the gate card's, and without a time the two
        # read as the same number disagreeing.
        out = safe(api.api_scan)
        if isinstance(out, dict):
            out["as_of"] = datetime.now(timezone(timedelta(hours=5, minutes=30))
                                        ).isoformat(timespec="seconds")
        return out

    if which == "daytrade":
        # Per underlying but NOT per expiry -- the paper trade is always the
        # front-expiry ATM straddle, so it has no expiry dimension to publish.
        # Compact: the full payload carries the whole journal and every
        # overnight row, which the page does not need on a 30-second refresh.
        return {u: safe(daytrade_api.api_daytrade, underlying=u, compact=1)
                for u in api.underlyings()}

    if which == "term":
        # Per underlying and deliberately NOT per expiry: the whole
        # point of the curve is that it spans expiries, so an expiry
        # dimension here would be a category error. Without this node
        # the term chart works on the API route and draws nothing on
        # nifty-desk.web.app, which is the failure mode that made
        # /api/series and /api/daytrade look healthy while returning
        # 404 -- see the note above the router imports in api.py.
        return {u: safe(term_api.api_term, underlying=u, limit=6)
                for u in api.underlyings()}

    # Everything below is per underlying, and per expiry within it. Discovered
    # from disk rather than listed, so widening instruments.py is the only
    # change needed to make another index appear here.
    out = {}
    for und in api.underlyings():
        exps = safe(api.api_expiries, underlying=und)
        if "error" in exps:
            out[und] = {"error": exps["error"]}
            continue
        # Not settled: from 15:30 IST on expiry day the settled weekly drops
        # out and the next one becomes the front -- see api.is_settled. With
        # EXPIRIES = 2 that also means the week AFTER is published, which is
        # what the builder's calendar spreads price their far leg from.
        live = [e["expiry"] for e in exps.get("expiries", [])
                if e["dte"] >= 0 and not e.get("settled")][:EXPIRIES]
        if which == "expiries":
            out[und] = exps
            continue
        if not live:
            out[und] = {"error": "no live expiry"}
            continue

        node = {}
        for exp in (live[:SERIES_EXPIRIES] if which == "series" else live):
            # RTDB keys cannot contain '.', '#', '$', '/', '[' or ']'.
            key = exp.replace("-", "")
            if which == "chain":
                node[key] = safe(api.api_chain, underlying=und, expiry=exp, width=18)
            elif which == "structures":
                node[key] = safe(api.api_structures, underlying=und, expiry=exp, lots=1)
            elif which == "intraday":
                node[key] = safe(api.api_intraday, underlying=und, expiry=exp, every=2)
            elif which == "analytics":
                node[key] = safe(api.api_analytics, underlying=und, expiry=exp)
            elif which == "buildup":
                node[key] = safe(series_api.api_buildup, underlying=und,
                                 expiry=exp, compact=1)
            elif which == "series":
                # One node per variant. safe() per variant rather than per
                # node, so a structure that cannot be priced on this chain --
                # a strangle whose wing has no two-sided quote, say -- leaves
                # a labelled error in its own key instead of blanking the
                # other two charts alongside it.
                node[key] = {
                    vkey: safe(series_api.api_series, underlying=und,
                               expiry=exp, compact=1, every=SERIES_EVERY,
                               **kw)
                    for vkey, kw in SERIES_VARIANTS.items()}
        out[und] = node

        if which == "structures":
            front = node.get(live[0].replace("-", ""), {})
            if "error" not in front:
                _last_slow[und] = {
                    "c_over_sigma": front.get("c_over_sigma"),
                    "cs_gate": front.get("cs_gate"),
                    "gate_passed": front.get("gate_passed"),
                    "straddle": front.get("straddle"),
                    "forward": front.get("forward"),
                    "vix": front.get("vix"),
                    "vol_source": front.get("spot_source"),
                    "expiry": front.get("expiry"),
                    "dte": front.get("dte"),
                }
    return out


def scrub(obj):
    """RTDB rejects None values inside objects, and silently drops empty ones.

    Rather than let a null POP quietly delete the key it belongs to -- which
    reads on the page as 'this field does not exist' instead of 'this could not
    be computed' -- nulls become the string 'n/a' nowhere; they are dropped
    here deliberately and the page already renders a missing field as an
    em-dash. What matters is that the drop happens in one place, on purpose.
    """
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [scrub(v) for v in obj if v is not None]
    if isinstance(obj, float) and obj != obj:      # NaN
        return None
    return obj


# ------------------------------------------------------------------ firebase

_db = None


def db():
    global _db
    if _db is None:
        import firebase_admin
        from firebase_admin import credentials, db as rtdb
        if not DB_URL:
            sys.exit("FIREBASE_DB_URL is not set -- put it in /home/ubuntu/.env")
        if not os.path.exists(CRED):
            sys.exit(f"service account key not found at {CRED}")
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(CRED),
                                          {"databaseURL": DB_URL})
        _db = rtdb
    return _db


def write(node, payload):
    db().reference(f"{ROOT}/{node}").set(scrub(payload))


def write_at(path, payload):
    """Write anywhere, for the archive, which lives outside /live."""
    db().reference(path).set(scrub(payload))


# ---------------------------------------------------------------------- loop

_seen = {}       # node -> hash of what was last written


def changed(node, blob):
    """Has this node's content actually moved since the last write?

    This is the whole economy of the thing. Recomputing the chain every 30
    seconds is cheap CPU on this box; WRITING it every 30 seconds is not,
    because every subscriber re-downloads the node on every write and the free
    tier is metered on download, not on writes.

    And the chain genuinely cannot change faster than the collector flushes it
    -- roughly every five minutes. So between flushes the bytes are identical
    and there is nothing to send. The hash turns 'poll often' into 'notify on
    change', which is what a live page actually wants.
    """
    h = hashlib.sha1(blob.encode()).hexdigest()
    if _seen.get(node) == h:
        return False
    _seen[node] = h
    return True


# ------------------------------------------------------------------ archive

_archived = None


def archived():
    """Which sessions are already on the archive, read once per process.

    One small read at startup rather than an existence check per session per
    pass: the index is a handful of dates and the alternative is fourteen
    round trips every two minutes to learn nothing.
    """
    global _archived
    if _archived is None:
        try:
            idx = db().reference(f"{ARCHIVE}/_index").get() or {}
        except Exception:
            # An unreadable index must not mean "archive everything again".
            # Empty is the safe answer only because a rewrite is idempotent;
            # it costs bytes, not correctness.
            idx = {}
        _archived = set(idx)
    return _archived


def build_archive(day):
    """One session, in exactly the shape /live has, one expiry per underlying.

    Same shape on purpose. The page can then swap its whole node tree for
    this payload and every existing read keeps working, instead of growing a
    second set of accessors that drift from the first.
    """
    d = str(day)
    out = {node: {} for node in ARCHIVE_NODES}
    out["session"] = d
    for und in api.underlyings():
        exps = safe(api.api_expiries, underlying=und, session=d)
        out["expiries"][und] = exps
        if "error" in exps:
            continue
        # The front expiry AS THAT SESSION SAW IT. Comparing ISO strings is
        # exact here and avoids re-deriving dte: the archive wants the first
        # expiry that had not yet settled on the day being archived, which is
        # not the same question as what is tradeable today.
        cands = sorted(e["expiry"] for e in exps.get("expiries", [])
                       if e["expiry"] >= d)
        if not cands:
            continue
        exp = cands[0]
        key = exp.replace("-", "")
        out["chain"][und] = {key: safe(api.api_chain, underlying=und,
                                       expiry=exp, width=18, session=d)}
        out["structures"][und] = {key: safe(api.api_structures, underlying=und,
                                            expiry=exp, lots=1, session=d)}
        out["analytics"][und] = {key: safe(api.api_analytics, underlying=und,
                                           expiry=exp, session=d)}
        out["intraday"][und] = {key: safe(api.api_intraday, underlying=und,
                                          expiry=exp, every=2, session=d)}
        out["term"][und] = safe(term_api.api_term, underlying=und, limit=6,
                                session=d)
    return out


def archive_pass(dry=False):
    """Archive up to ARCHIVE_PER_PASS sessions that are not there yet.

    The NEWEST session is skipped: it is still being collected, and an
    archive entry is written once and never revisited, so freezing a
    half-finished day would freeze it permanently.
    """
    days = api.sessions()
    if len(days) < 2:
        return ""
    have = archived()
    todo = [d for d in days[:-1] if str(d) not in have][:ARCHIVE_PER_PASS]
    if not todo:
        return ""
    done = []
    for day in todo:
        payload = build_archive(day)
        blob = json.dumps(scrub(payload), default=str, sort_keys=True)
        if not dry:
            write_at(f"{ARCHIVE}/{day}", json.loads(blob))
            write_at(f"{ARCHIVE}/_index/{day}",
                     {"at": datetime.now(IST).isoformat(timespec="seconds"),
                      "bytes": len(blob)})
        have.add(str(day))
        done.append(f"archive {day} {len(blob)/1024:.1f}KB")
    return ", ".join(done)


def cycle(state, dry=False, force=False):
    """Recompute whichever nodes are due; write only the ones that moved."""
    now = time.time()
    mult = 1 if market_open() else CLOSED_MULTIPLIER
    done, skipped, sent = [], 0, 0

    for node, every in CADENCE.items():
        m = 1 if node in NO_BACKOFF else mult
        if not (force or now - state.get(node, 0) >= every * m):
            continue
        state[node] = now
        payload = build(node)
        if payload is None:
            continue
        blob = json.dumps(scrub(payload), default=str, sort_keys=True)
        # Always evaluate changed() -- it is what records the hash. Letting
        # `force` short-circuit it meant the first pass wrote everything and
        # remembered nothing, so the second pass wrote everything again.
        moved = changed(node, blob)
        if not (force or moved):
            skipped += 1
            continue
        if not dry:
            write(node, json.loads(blob))
        sent += len(blob)
        done.append(f"{node} {len(blob)/1024:.1f}KB")

    # Deliberately NOT scaled by CLOSED_MULTIPLIER. The archive is most
    # useful when the market is shut, which is exactly when everything else
    # here backs off fifteen-fold.
    if force or now - state.get("_archive", 0) >= 120:
        state["_archive"] = now
        try:
            line = archive_pass(dry=dry)
            if line:
                done.append(line)
        except Exception:
            traceback.print_exc()

    if done and not dry:
        db().reference(f"{ROOT}/updated_at").set(
            datetime.now(IST).isoformat(timespec="seconds"))
    if not done:
        return ""
    return (", ".join(done)
            + (f"  ({skipped} unchanged)" if skipped else "")
            + f"  total {sent/1024:.1f}KB")


def main():
    dry = "--dry" in sys.argv
    once = "--once" in sys.argv or dry
    state = {}

    if once:
        print(cycle(state, dry=dry, force=True) or "nothing written")
        return 0

    print(f"publisher up · db {DB_URL or '(unset)'} · "
          f"market {'open' if market_open() else 'closed'}", flush=True)
    while True:
        try:
            line = cycle(state)
            if line:
                print(f"{datetime.now(IST):%H:%M:%S}  {line}", flush=True)
        except Exception:
            # One bad cycle -- a half-written Parquet part, a transient network
            # error -- must not take the publisher down for the rest of the
            # session. Log it and try again on the next tick.
            traceback.print_exc()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main() or 0)

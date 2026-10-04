"""Daily instrument master. Resolves the subscription universe and writes it to disk.

TWO SEGMENTS, NOT ONE

NIFTY and BANKNIFTY are NSE F&O (NFO). SENSEX is BSE F&O (BFO) -- a separate
download, a separate entitlement, and a separate exchange tariff downstream
(see api.costs_for). Measured on 2026-09-21, BFO carries 4,506 instruments
under four names: SENSEX, BANKEX, FOCIT and SENSEX50. `name` is the only
thing separating them, which is why the universe below lists names explicitly
rather than pattern-matching: a filter that catches SENSEX50 instead of
SENSEX produces a complete, plausible, entirely wrong chain.

HOW MANY EXPIRIES

Each one is a real subscription cost. Measured the same day: the SENSEX front
weekly is 328 contracts, and NIFTY plus BANKNIFTY together are collecting
roughly 1,600. Two SENSEX expiries is about +38% on the websocket and on
every Parquet partition behind it, on a 2 GB box whose first job is not
dropping ticks. Start at two; widen once a session's worth of data says the
collector kept up.
"""
import os, json, sys
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from session import get_kite

# The box runs UTC and the exchange does not. `date.today()` is naive, so
# between 18:30 and midnight UTC it returns YESTERDAY by Indian reckoning --
# and run_collector.sh re-runs this file on every systemd restart, at
# whatever hour that happens.
#
# The filename is what breaks. api.lot_for() looks for
# data/instruments/<IST date>.json; this would have written <UTC date>.json,
# so a restart in that window leaves the master under a name nothing goes
# looking for. During market hours the two dates always agree, which is
# exactly why it would have sat there unnoticed.
#
# Same definition as api.today_ist(). One clock.
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist():
    return datetime.now(IST).date()

# name -> which segment it lives in, and how many expiries to subscribe.
UNIVERSE = {
    "NIFTY":     {"segment": "NFO", "expiries": 5},
    "BANKNIFTY": {"segment": "NFO", "expiries": 3},
    "SENSEX":    {"segment": "BFO", "expiries": 2},
}

# The index series themselves. These are what session_ref reads to replay a
# past session's spot and volatility, so an underlying whose index is missing
# here can be charted but never replayed honestly.
INDEX_SYMBOLS = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX",
                 "BSE:SENSEX"]

MIN_EXPECTED = 1200
OUT_DIR = "data/instruments"


def _as_date(v):
    return v.date() if isinstance(v, datetime) else v


def choose(instruments, universe, today):
    """(chosen option rows, {name: [expiry, ...]}) for one segment's download.

    Pure, so the part that can silently pick the wrong universe is testable
    without a Kite session: a `name` that matches nothing returns an empty
    expiry list for that underlying, and build() refuses rather than writing
    a master that looks fine and is missing an index.
    """
    opts = [i for i in instruments
            if i["name"] in universe and i["instrument_type"] in ("CE", "PE")]

    by_expiry = defaultdict(list)
    for i in opts:
        e = _as_date(i["expiry"])
        if e >= today:
            by_expiry[(i["name"], e)].append(i)

    chosen, expiries = [], {}
    for name in universe:
        dates = sorted(e for (nm, e) in by_expiry if nm == name)
        dates = dates[:universe[name]["expiries"]]
        expiries[name] = [str(d) for d in dates]
        for d in dates:
            chosen.extend(by_expiry[(name, d)])
    return chosen, expiries


def build():
    kite = get_kite()
    today = today_ist()

    ltp = kite.ltp(INDEX_SYMBOLS)
    indices = [{"instrument_token": v["instrument_token"],
                "tradingsymbol": k.split(":", 1)[1],
                "name": "INDEX", "kind": "index",
                "last_price": v["last_price"]}
               for k, v in ltp.items()]

    chosen, expiries = [], {}
    # One download per segment, not per name: kite.instruments() returns the
    # whole segment and the BFO list alone is 4,500 rows.
    for segment in sorted({u["segment"] for u in UNIVERSE.values()}):
        want = {n: u for n, u in UNIVERSE.items() if u["segment"] == segment}
        print(f"downloading {segment} instrument master...")
        rows = kite.instruments(segment)
        got, exp = choose(rows, want, today)
        chosen.extend(got)
        expiries.update(exp)

    rows = [{"instrument_token": i["instrument_token"],
             "tradingsymbol": i["tradingsymbol"],
             "name": i["name"],
             "expiry": str(_as_date(i["expiry"])),
             "strike": i["strike"],
             "instrument_type": i["instrument_type"],
             "lot_size": i["lot_size"],
             "kind": "option"}
            for i in chosen] + indices

    out = {"date": str(today),
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "expiries": expiries,
           "count": len(rows),
           "instruments": rows}

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{today}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)

    for name in UNIVERSE:
        n = sum(1 for r in rows if r["name"] == name)
        print(f"{name:10s} {len(expiries.get(name, [])):d} expiries, "
              f"{n:>5,} contracts: {', '.join(expiries.get(name, [])) or 'NONE'}")
    print(f"\ntotal instruments : {len(rows)}")
    print(f"written to        : {path}")

    # An underlying that produced nothing is the failure this guard is for.
    # A total-count floor cannot see it: SENSEX contributing zero while NIFTY
    # contributes two thousand still clears any threshold worth setting, and
    # the collector then runs all day against a universe quietly missing an
    # index.
    empty = [n for n in UNIVERSE if not expiries.get(n)]
    if empty:
        print(f"\nFAIL: no expiries found for {', '.join(empty)} -- check the "
              f"`name` field in that segment, and the entitlement")
        sys.exit(1)

    if len(rows) < MIN_EXPECTED:
        print(f"\nFAIL: only {len(rows)} instruments, expected at least {MIN_EXPECTED}")
        sys.exit(1)
    return out


if __name__ == "__main__":
    build()

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

# STOCKS (added 11 Nov 2026, for the website's market pages)
#
# Every F&O stock: its NSE cash share (price, volume) and its nearest-month
# future (price, OI). Roughly 200 names, so about 400 tokens on top of the
# option chains. Strictly optional: anything that goes wrong here prints a
# warning and the option universe is written exactly as before -- collecting
# the chains is this box's first job and the stock layer must never cost it.
# Switch off with STOCKS=0 in .env (read in build(), after .env is loaded).
# Index futures live in the same NFO list as stock futures; these are not stocks.
INDEX_FUT_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
# Kite caps one websocket (it reports 4,000; the docs say 3,000) and allows 3
# per API key. collector.py splits the universe over connections of 3,500.
# Capping the total at 7,000 keeps it to two, leaving the third spare.
MAX_TOKENS = 7000


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


def choose_stocks(nfo_rows, nse_rows, today):
    """Stock rows (cash share + nearest future per F&O stock). Pure, so testable.

    The F&O list decides WHICH stocks: anything with a live stock future. The
    cash token is looked up in the NSE equity list under the same symbol; a
    stock whose symbol does not match there is skipped rather than guessed.
    """
    near = {}
    for i in nfo_rows:
        if i.get("instrument_type") != "FUT" or i.get("name") in INDEX_FUT_NAMES:
            continue
        e = _as_date(i["expiry"])
        if e < today:
            continue
        n = i["name"]
        if n not in near or e < _as_date(near[n]["expiry"]):
            near[n] = i
    eq = {i["tradingsymbol"]: i for i in nse_rows
          if i.get("instrument_type") == "EQ" and i.get("segment", "NSE") == "NSE"}
    rows = []
    for n in sorted(near):
        cash, fut = eq.get(n), near[n]
        if cash is None:
            continue
        rows.append({"instrument_token": cash["instrument_token"], "tradingsymbol": n,
                     "name": n, "expiry": None, "strike": None,
                     "instrument_type": "EQ", "lot_size": fut["lot_size"],
                     "kind": "stock"})
        rows.append({"instrument_token": fut["instrument_token"],
                     "tradingsymbol": fut["tradingsymbol"], "name": n,
                     "expiry": str(_as_date(fut["expiry"])), "strike": None,
                     "instrument_type": "FUT", "lot_size": fut["lot_size"],
                     # tick_size: required by the stock-futures paper system
                     # (futures/) for tick-rounded fills; Kite's dump carries it.
                     "tick_size": fut.get("tick_size"),
                     "kind": "stock_fut"})
    return rows


def add_prev_close(kite, stock_rows, batch=500):
    """Yesterday's close on every stock row, for % change on the market pages.

    Read before the open, Kite's OHLC `close` is the previous session's close.
    Best effort: a row without it simply shows no % change until tomorrow.
    """
    keys = {}
    for r in stock_rows:
        ex = "NSE" if r["kind"] == "stock" else "NFO"
        keys[f"{ex}:{r['tradingsymbol']}"] = r
    names = list(keys)
    for i in range(0, len(names), batch):
        try:
            got = kite.ohlc(names[i:i + batch])
        except Exception as e:  # noqa: BLE001
            print(f"WARNING previous closes not fetched: {type(e).__name__}: {e}")
            return
        for k, v in got.items():
            c = (v.get("ohlc") or {}).get("close")
            if k in keys and c:
                keys[k]["prev_close"] = float(c)


def fit_budget(option_rows, stock_rows, cap=MAX_TOKENS):
    """Trim the stock layer so the whole subscription stays under the cap.
    Futures go first (their OI is the nice-to-have); cash shares last."""
    room = cap - len(option_rows)
    if len(stock_rows) <= room:
        return stock_rows
    cash = [r for r in stock_rows if r["kind"] == "stock"]
    if len(cash) <= room:
        futs = [r for r in stock_rows if r["kind"] == "stock_fut"][:room - len(cash)]
        return cash + futs
    return cash[:max(room, 0)]


def build():
    kite = get_kite()
    today = today_ist()

    ltp = kite.ltp(INDEX_SYMBOLS)
    indices = [{"instrument_token": v["instrument_token"],
                "tradingsymbol": k.split(":", 1)[1],
                "name": "INDEX", "kind": "index",
                "last_price": v["last_price"]}
               for k, v in ltp.items()]

    chosen, expiries, downloaded = [], {}, {}
    # One download per segment, not per name: kite.instruments() returns the
    # whole segment and the BFO list alone is 4,500 rows.
    for segment in sorted({u["segment"] for u in UNIVERSE.values()}):
        want = {n: u for n, u in UNIVERSE.items() if u["segment"] == segment}
        print(f"downloading {segment} instrument master...")
        rows = kite.instruments(segment)
        downloaded[segment] = rows
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

    stock_rows = []
    # Read here, not at import: get_kite() above is what loads .env, so a
    # STOCKS=0 line there only takes effect from this point.
    if os.environ.get("STOCKS", "1") != "0":
        try:
            nfo = downloaded.get("NFO") or kite.instruments("NFO")
            stock_rows = fit_budget(rows, choose_stocks(nfo, kite.instruments("NSE"), today))
            add_prev_close(kite, stock_rows)
            try:
                import sectors
                sectors.refresh()
            except Exception as e:  # noqa: BLE001
                print(f"WARNING sector map not refreshed: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 -- optional layer, see STOCKS above
            print(f"WARNING stock layer skipped: {type(e).__name__}: {e}")
            stock_rows = []
    option_count = len(rows)
    rows = rows + stock_rows

    out = {"date": str(today),
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "expiries": expiries,
           "count": len(rows),
           "stocks": sum(1 for r in stock_rows if r["kind"] == "stock"),
           "stock_futures": sum(1 for r in stock_rows if r["kind"] == "stock_fut"),
           "instruments": rows}

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{today}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)

    for name in UNIVERSE:
        n = sum(1 for r in rows if r["name"] == name)
        print(f"{name:10s} {len(expiries.get(name, [])):d} expiries, "
              f"{n:>5,} contracts: {', '.join(expiries.get(name, [])) or 'NONE'}")
    print(f"stocks            : {out['stocks']} cash + {out['stock_futures']} futures")
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

    # The floor is on the OPTION universe; stocks must not be able to hide a
    # thin chain by padding the total.
    if option_count < MIN_EXPECTED:
        print(f"\nFAIL: only {option_count} option/index instruments, expected at least {MIN_EXPECTED}")
        sys.exit(1)
    return out


if __name__ == "__main__":
    build()

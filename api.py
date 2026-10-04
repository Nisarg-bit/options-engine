"""Read-only HTTP API over everything this machine already collects.

Design constraint that shaped every line below: the collector must not be
touched. It is five sessions into a ten-session soak, and an edit -- even a
harmless one -- restarts that clock in spirit if not in fact. So this process
never imports collector.py, never opens a Kite websocket, and never writes
anything into data/. It reads the Parquet parts the collector has already
flushed, and that is all.

That works because of how BarWriter behaves: it flushes when its buffer holds
five distinct timestamps, so data/bars_1m/date=<today>/ grows by a new
part-NNNNN.parquet roughly every five minutes during the session. A page that
polls this API therefore sees the market with about five minutes of lag and
zero risk to the thing producing it. Genuine tick-by-tick would mean a second
Kite connection and a change to the collector; five minutes is the price of
not doing that, and for structures held to expiry it is not a price worth
arguing about.

Spot and VIX are the one exception -- they are not in the option bars, so
they come from Kite. That call is cached for 20 seconds and falls back to the
parity forward from the chain itself if Kite is unreachable, so a token
expiry degrades the dashboard rather than breaking it.

Endpoints
    GET  /api/health              collector liveness, lag, coverage
    GET  /api/sessions            dates collected
    GET  /api/expiries            expiries live in the latest session
    GET  /api/chain               the chain, priced at mid, with IV
    GET  /api/structures          the seven structures, live
    GET  /api/signal              this morning's journalled signal
    GET  /api/journal             calibration: modelled vs realised
    POST /api/payoff              arbitrary legs -> payoff curve + stats

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""

import base64
import glob
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import chain_metrics as CM
import models as M
import pricing as P
import sigma_sources as SIG
import strategies as S
import stop_touch_api as STA
import catalogue as CAT
import calpop as CP

ROOT = os.path.dirname(os.path.abspath(__file__))
BARS = os.path.join(ROOT, "data", "bars_1m")
JOURNAL_CSV = os.path.join(ROOT, "data", "journal", "signals.csv")
WEB = os.path.join(ROOT, "web")

IST = timezone(timedelta(hours=5, minutes=30))

SIGMA_MULT = 1.5
CS_GATE = 0.9346
DEFAULT_LOT = 65

# Setup!E31:E35 -- the workbook's own margin-per-lot table, which is a better
# source than the 10%-of-notional guess compare.py used, because it is the
# number the sheet's Risk_Sizing block has always sized against. Still an
# estimate: Kite's basket-margin endpoint would give the real figure and is
# not wired up yet, so the page labels these as indicative.
MARGIN_PER_LOT = {
    "NIFTY": 120000.0, "BANKNIFTY": 185000.0, "FINNIFTY": 135000.0,
    "MIDCPNIFTY": 110000.0, "SENSEX": 130000.0,
}
MARGIN_PCT_NOTIONAL = 0.10          # fallback for anything not in the table

# Index spot symbols, for the underlyings this project can price. Only the ones
# the collector actually subscribes to will ever appear -- the list the page
# shows is discovered from disk, not from this table.
INDEX_SYMBOL = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MIDCAP SELECT",
    "SENSEX":     "BSE:SENSEX",
}
VIX_SYMBOL = "NSE:INDIA VIX"

# Same quote-sanity rule as signal.py. A strike whose quote fails this is not
# priced at a guess -- it is dropped and counted, and the count is shown.
SPREAD_ABS = 2.0
SPREAD_REL = 0.25

# Costs by exchange. The rates, their history and the one place they change
# all live in pricing.py; these names are kept because callers and tests use
# them. BSE's SENSEX/BANKEX tariff is lower than NSE's, and SENSEX50 is lower
# again -- an unclassified underlying is priced on NSE's, the dearest.
NSE_OPTION_EXCHANGE = P.NSE_OPTION_EXCHANGE
BSE_OPTION_EXCHANGE = P.BSE_OPTION_EXCHANGE
STT_ON_SELL = P.STT_ON_SELL
costs_for = P.current_costs_for
COSTS = P.CURRENT_COSTS

# Measured half-spreads from spreads.py, by |K/S - 1|. The 1-point-per-leg
# assumption the workbook carries was wrong by roughly 9x at the money.
MEASURED_HALF_SPREAD = [
    (0.0025, 0.11), (0.0100, 0.12), (0.0200, 0.16),
    (0.0400, 0.33), (9.9999, 0.96),
]

# ------------------------------------------------------------------- access
#
# Two mechanisms, both off unless configured:
#
#   DASH_USER + DASH_PASS   HTTP Basic on EVERY route, page included. This is
#                           what guards the Funnel URL -- the browser shows a
#                           login box and remembers it per device.
#   DASH_TOKEN              ?k=<token> on /api/ only. Kept for scripted access
#                           (curl, a cron probe) where a password prompt is
#                           awkward.
#
# Basic auth over plain HTTP would send the password in clear, so it is worth
# being explicit about why that is not the case here: the public route
# terminates TLS at Tailscale's edge and the connection from there to this
# process never leaves the machine. On the private route there is no public
# path at all.
USER  = os.environ.get("DASH_USER", "").strip()
PASS  = os.environ.get("DASH_PASS", "").strip()
TOKEN = os.environ.get("DASH_TOKEN", "").strip()

app = FastAPI(title="options dashboard", docs_url=None, redoc_url=None)


def _basic_ok(header):
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
        user, _, pw = raw.partition(":")
    except Exception:
        return False
    # compare_digest on both halves, and always both -- returning early on a
    # wrong username leaks which half was wrong through response timing.
    return (secrets.compare_digest(user, USER) & secrets.compare_digest(pw, PASS))


# A public URL gets found by scanners within hours -- that is automated and
# constant, not bad luck. With no password in front, the risk that actually
# matters is not someone reading the P&L; it is a bot hammering /api/intraday,
# which reads the whole session's bars, on a 2 GB box whose real job is running
# the collector. So: a plain per-IP token bucket. Generous enough that the
# 30-second page refresh never notices, tight enough that a loop cannot starve
# the thing this project exists for.
RATE_N, RATE_WINDOW = 90, 60.0
_hits = {}


def _rate_ok(ip):
    now = time.time()
    q = _hits.setdefault(ip, [])
    cutoff = now - RATE_WINDOW
    while q and q[0] < cutoff:
        q.pop(0)
    if len(_hits) > 500:                       # don't grow without bound
        for k in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
            _hits.pop(k, None)
    if len(q) >= RATE_N:
        return False
    q.append(now)
    return True


@app.middleware("http")
async def gate(request: Request, call_next):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if not _rate_ok(ip):
        return Response(status_code=429, content="slow down",
                        headers={"Retry-After": "30"})

    if PASS:
        if not _basic_ok(request.headers.get("authorization")):
            return Response(status_code=401, content="unauthorised",
                            headers={"WWW-Authenticate":
                                     'Basic realm="options desk", charset="UTF-8"'})
    elif TOKEN and request.url.path.startswith("/api/"):
        supplied = (request.query_params.get("k")
                    or request.headers.get("x-dash-token", ""))
        if not secrets.compare_digest(supplied, TOKEN):
            return JSONResponse({"error": "unauthorised"}, status_code=401)

    resp = await call_next(request)
    # Reachable by anyone is not the same as wanting it in Google. Search
    # engines honour this; it costs nothing and keeps the page out of results
    # and out of the Wayback Machine.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/api/whoami")
def whoami():
    """Which lock is actually on. Useful before exposing anything publicly."""
    return {"auth": "basic" if PASS else ("token" if TOKEN else "none"),
            "user": USER if PASS else None}


# ------------------------------------------------------------------- caching

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


_parts = {}   # path -> (mtime, size, DataFrame)


def read_parts(day, underlying):
    """Concat every Parquet part for one session, re-reading only what changed.

    The collector appends parts while this runs, so a file can be half-written
    at the moment it is opened. A failed read is skipped rather than fatal --
    the next poll, thirty seconds later, gets the complete file.
    """
    pattern = os.path.join(BARS, f"date={day}", f"underlying={underlying}",
                           "*.parquet")
    frames = []
    for path in sorted(glob.glob(pattern)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig = (st.st_mtime, st.st_size)
        hit = _parts.get(path)
        if hit and hit[0] == sig:
            frames.append(hit[1])
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            if hit:
                frames.append(hit[1])
            continue
        _parts[path] = (sig, df)
        frames.append(df)

    # The box has 2 GB and the collector is the tenant that matters. Hold only
    # the session being viewed; yesterday's parts would otherwise accumulate
    # until the process was restarted.
    prefix = os.path.join(BARS, f"date={day}")
    for stale in [k for k in _parts if not k.startswith(prefix)]:
        del _parts[stale]

    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def sessions():
    out = []
    for p in glob.glob(os.path.join(BARS, "date=*")):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return sorted(out)


def latest_session():
    days = sessions()
    return days[-1] if days else None


def session_moves(spot, lookback=None):
    """Absolute point moves of the index, one per completed session.

    Read from the INDEX partition rather than rebuilt from option chains: the
    index bars are a few thousand rows a session against six hundred thousand
    for the chain, and the move is close-minus-open on the same series
    realised_vol.py measures, so the two agree by construction.

    THE PARTITION HOLDS MORE THAN ONE INSTRUMENT. underlying=INDEX carries
    both the index itself and India VIX. The first version of this function
    took close-minus-open across all of it, differencing a VIX close near 12
    against a NIFTY close near 23,000; every session "moved" ~23,000 points
    and the endpoint served a realised volatility of 2723%.

    The series is picked by whose median close sits nearest `spot` instead of
    by a hard-coded token. NIFTY_TOKEN = 256265 is copy-pasted into seven
    files here and there is no BANKNIFTY equivalent anywhere, so a token
    lookup would work for exactly one underlying and fail silently for the
    rest. Proximity to spot is also self-checking: a series that is not near
    spot is rejected rather than differenced.

    Cached on the session LIST, not a clock. The set of completed sessions
    changes once a day; a TTL would either re-read twenty Parquet files for
    nothing or serve a window that has quietly gone stale.
    """
    lookback = lookback or SIG.REALISED_LOOKBACK
    if not spot or spot <= 0:
        return []
    days = sessions()[-(lookback + 1):]
    if not days:
        return []

    def build():
        out = []
        for d in days:
            df = read_parts(d, "INDEX")
            if df is None or df.empty or "close" not in df.columns:
                continue
            if "instrument_token" in df.columns:
                # Nearest median close to spot, and only if it is actually
                # near -- 20% is far wider than any session gap and far
                # narrower than the gap to a volatility index.
                med = df.groupby("instrument_token")["close"].median()
                med = med[med > 0]
                if med.empty:
                    continue
                tok = (med - spot).abs().idxmin()
                if abs(float(med.loc[tok]) - spot) > 0.20 * spot:
                    continue
                df = df[df["instrument_token"] == tok]
            s = df.sort_values("ts")["close"].astype(float)
            if len(s) < 2:
                continue
            out.append(float(s.iloc[-1]) - float(s.iloc[0]))
        return out

    # spot moves intraday, so it is rounded out of the cache key -- otherwise
    # every tick is a fresh key and the cache never hits.
    return cached(f"session_moves:{days[0]}:{days[-1]}:{len(days)}:"
                  f"{int(spot // 500)}", 3600, build)


def chain_iv_units(flat, spot, dte):
    """This expiry's ATM implied vol, in VIX units, or None.

    market_ref already does exactly this for every underlying that is not
    NIFTY. Pulling it out means NIFTY can ask for it too, which is the whole
    of what sigma_source=chain_iv means.
    """
    if not spot or not flat:
        return None
    try:
        iv = M.atm_iv(flat, spot, max(dte, 1) / 365.0)
    except Exception:
        return None
    return iv * 100.0 if iv else None


def as_ist(ts):
    t = pd.Timestamp(ts)
    return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)


def underlyings(day=None):
    """Which underlyings actually have data, discovered from the partitions.

    Deliberately not a hard-coded list. The collector currently subscribes to
    NIFTY and BANKNIFTY; if instruments.py is ever widened to FINNIFTY or the
    rest, they appear here the next session with nothing else to change. A
    dropdown that offers an underlying with no bars behind it is worse than one
    that offers fewer.
    """
    day = day or latest_session()
    if not day:
        return []
    out = []
    for p in glob.glob(os.path.join(BARS, f"date={day}", "underlying=*")):
        name = os.path.basename(p).split("=", 1)[1]
        if name and name != "INDEX":
            out.append(name)
    # NIFTY first when present -- it is the one the whole model was built on.
    return sorted(out, key=lambda n: (n != "NIFTY", n))


def infer_step(strikes):
    """The strike interval, read off the chain rather than tabulated.

    NIFTY is 50, BANKNIFTY 100, MIDCPNIFTY 25 -- and those change. The most
    common gap between adjacent listed strikes is the answer, and it stays
    right without anyone maintaining a table.
    """
    ks = sorted(set(float(k) for k in strikes))
    if len(ks) < 3:
        return 50.0
    gaps = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a]
    if not gaps:
        return 50.0
    return float(pd.Series(gaps).mode().iloc[0])


def lot_for(underlying, day=None):
    """(lot size, source), from the instrument master the collector wrote."""
    for d in ([day] if day else []) + [latest_session(), datetime.now(IST).date()]:
        if not d:
            continue
        path = os.path.join(ROOT, "data", "instruments", f"{d}.json")
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
            rows = data if isinstance(data, list) else data.get("instruments", [])
            for r in rows:
                if r.get("name") == underlying and (r.get("lot_size") or r.get("lotsize")):
                    return int(r.get("lot_size") or r.get("lotsize")), f"master {d}"
        # Deliberately narrow. A blanket `except Exception` here hid a missing
        # `import json` as a silent fallback to the wrong lot size -- which is
        # the kind of bug that shows up as slightly wrong rupee figures rather
        # than as an error, and can sit there for weeks.
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return DEFAULT_LOT, "fallback"


# --------------------------------------------------------------------- chain

def last_bars(day, underlying):
    """Latest bar per instrument. Cached for 20s.

    A full session is roughly 600k rows by the close, and sorting that on every
    poll would burn more CPU than the collector does. 20 seconds is under the
    page's 30-second refresh, so each refresh pays for it once and the chain and
    structures endpoints share the result.
    """
    def build():
        df = read_parts(day, underlying)
        if df is None or df.empty:
            return None
        return (df.sort_values("ts")
                  .groupby("instrument_token", as_index=False).last())
    return cached(f"lastbars:{day}:{underlying}", 20, build)


def session_stats(day, underlying):
    """Per contract, for the whole session so far: VWAP, and the OI move.

    VWAP is why writer.py stores `pv` -- price times volume, accumulated per
    bar. Summing pv and volume over the session and dividing gives the true
    volume-weighted average price; averaging the bar closes would not, because
    it weights a minute that traded one lot the same as a minute that traded a
    thousand.

    The OI move is measured from the session's FIRST bar to the latest, which
    is the "day buildup" a trader means. Comparing to the previous day's close
    would be the other reasonable definition, but that number is not on this
    machine before the collector has run a full prior session on that contract.
    """
    def build():
        df = read_parts(day, underlying)
        if df is None or df.empty:
            return None
        df = df.sort_values("ts")
        if "pv" not in df.columns:
            # writer.py always writes pv, but a hand-made or older part might
            # not. close*volume per bar is the same quantity to within the
            # within-bar price path, which is the best available then.
            df = df.assign(pv=df["close"] * df["volume"])
        g = df.groupby("instrument_token")
        out = pd.DataFrame({
            "oi_open":  g["oi"].first(),
            "oi_last":  g["oi"].last(),
            "px_open":  g["close"].first(),
            "px_last":  g["close"].last(),
            "pv_sum":   g["pv"].sum(),
            "vol_sum":  g["volume"].sum(),
        })
        # float NaN, never pd.NA. A contract that has not traded this session
        # has no VWAP, and pd.NA propagates into `if vw == vw` as NA rather
        # than False -- which raises "boolean value of NA is ambiguous" and
        # takes the whole structures build down. NaN compares False and is
        # safe. My synthetic chains all had volume on every strike, so this
        # only surfaced on the real BANKNIFTY book.
        vol = out["vol_sum"].astype("float64")
        pv = out["pv_sum"].astype("float64")
        out["vwap"] = (pv / vol).where(vol > 0, float("nan"))
        for c in ("oi_open", "oi_last", "px_open", "px_last"):
            out[c] = out[c].astype("float64")
        return out
    return cached(f"stats:{day}:{underlying}", 30, build)


def fnum(x):
    """A float, or None. Absorbs NaN, pandas NA and anything unparseable."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def buildup(d_px, d_oi):
    """The four-way read on price and open interest moving together.

    Open interest counts contracts outstanding, so it rises when a NEW position
    is opened and falls when one is closed. Pair that with the price direction
    and you get who was doing it:

        price up,   OI up    new buyers paying up          long buildup
        price down, OI up    new sellers pressing          short buildup
        price down, OI down  holders closing longs         long unwinding
        price up,   OI down  shorts buying back            short covering

    The last one is the interesting signal on an option: a strike whose price
    is rising while OI falls is one that writers are giving up on.
    """
    if d_px is None or d_oi is None:
        return None
    # Below a percent of movement it is noise, and labelling noise as a
    # "buildup" is how a dashboard starts telling stories about nothing.
    if abs(d_px) < 0.005 or abs(d_oi) < 0.005:
        return "flat"
    if d_px > 0 and d_oi > 0:
        return "long buildup"
    if d_px < 0 and d_oi > 0:
        return "short buildup"
    if d_px < 0 and d_oi < 0:
        return "long unwinding"
    return "short covering"


def quote_mid(row):
    bid, ask = float(row["bid"]), float(row["ask"])
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    mid, spread = 0.5 * (bid + ask), ask - bid
    if spread > max(SPREAD_ABS, SPREAD_REL * mid):
        return None, spread
    return mid, spread


def half_spread_pts(strike, spot):
    m = abs(strike / spot - 1.0) if spot else 0.0
    for hi, cost in MEASURED_HALF_SPREAD:
        if m < hi:
            return cost
    return MEASURED_HALF_SPREAD[-1][1]


def build_chain(day, underlying, expiry):
    """[{strike, CE, PE}] priced at mid, with quote, VWAP and buildup detail."""
    bars = last_bars(day, underlying)
    if bars is None:
        return None, {"one_sided": 0, "wide": 0}, 0, None
    sub = bars[bars["expiry"] == expiry]
    if sub.empty:
        return None, {"one_sided": 0, "wide": 0}, 0, None

    stats = session_stats(day, underlying)
    legs, dropped = {}, {"one_sided": 0, "wide": 0}
    last_ts = None
    for _, r in sub.iterrows():
        ts = r["ts"]
        if last_ts is None or ts > last_ts:
            last_ts = ts
        mid, spread = quote_mid(r)
        if mid is None:
            dropped["wide" if spread is not None else "one_sided"] += 1
            continue

        cell = {
            "mid": round(mid, 2),
            "bid": round(float(r["bid"]), 2),
            "ask": round(float(r["ask"]), 2),
            "bid_qty": int(r.get("bid_qty", 0) or 0),
            "ask_qty": int(r.get("ask_qty", 0) or 0),
            "oi": int(r.get("oi", 0) or 0),
            "volume": int(r.get("volume", 0) or 0),
            "close": round(float(r.get("close", 0) or 0), 2),
            "vwap": None, "oi_chg": None, "px_chg": None, "buildup": None,
        }

        s = None
        if stats is not None:
            try:
                s = stats.loc[int(r["instrument_token"])]
            except KeyError:
                s = None
        if s is not None:
            vw = fnum(s["vwap"])
            if vw is not None:
                cell["vwap"] = round(vw, 2)
            oi0, oi1 = fnum(s["oi_open"]), fnum(s["oi_last"])
            px0, px1 = fnum(s["px_open"]), fnum(s["px_last"])
            if oi0 and oi1 is not None and oi0 > 0:
                cell["oi_chg"] = round((oi1 - oi0) / oi0, 4)
            if px0 and px1 is not None and px0 > 0:
                cell["px_chg"] = round((px1 - px0) / px0, 4)
            cell["buildup"] = buildup(cell["px_chg"], cell["oi_chg"])

        legs.setdefault(float(r["strike"]), {})[r["opt_type"]] = cell

    rows = [{"strike": k, **v} for k, v in sorted(legs.items())
            if "CE" in v and "PE" in v]
    return rows, dropped, len(sub), last_ts


def priced(rows):
    """The flat {strike, ce, pe} shape pricing/strategies expect."""
    return [{"strike": r["strike"], "ce": r["CE"]["mid"], "pe": r["PE"]["mid"]}
            for r in rows]


def quotes():
    """One Kite call for every index we might show, cached for 4 seconds.

    Batched deliberately: five separate ltp calls at a 5-second tick would be
    five times the API traffic for data that arrives in one request anyway.
    """
    def fetch():
        import session as kite_session
        kite = kite_session.get_kite()
        want = [INDEX_SYMBOL[u] for u in underlyings() if u in INDEX_SYMBOL]
        want.append(VIX_SYMBOL)
        q = kite.ltp(want)
        return {k: float(v["last_price"]) for k, v in q.items()}
    return cached("ltp", 4, fetch)


# India VIX has traded between about 8 and 87 in its history. The window is
# deliberately wider than that at both ends: it exists to tell a volatility
# index apart from an index level of 23,000, not to validate the reading.
VIX_PLAUSIBLE = (4.0, 120.0)


def session_ref(day, underlying, chain_rows, dte):
    """(spot, volatility in VIX units, source) as that session actually stood.

    market_ref reaches for a LIVE Kite quote. That is right for the newest
    session and catastrophic for any other: replaying 2026-09-03 through it
    stamps today's spot and today's India VIX onto that day's chain and
    presents the result as history. Nothing would look broken -- the numbers
    stay internally consistent -- which is the worst way for a number to be
    wrong.

    Both series are in the session's own INDEX partition; the collector stores
    the index and India VIX side by side there. They are told apart by
    magnitude rather than by a hard-coded token, for the reason session_moves
    already gives: NIFTY_TOKEN is copy-pasted into seven files here and there
    is no BANKNIFTY equivalent anywhere, so a token lookup works for exactly
    one underlying and fails silently for the rest.

    This function never calls quotes(). That is the property the replay rests
    on, and test_replay asserts it by making quotes() raise.
    """
    import journal
    level = journal.implied_level(priced(chain_rows)) if chain_rows else None

    spot, vix = None, None
    df = read_parts(day, "INDEX")
    if df is not None and not df.empty and "close" in df.columns:
        if "instrument_token" in df.columns:
            med = df.groupby("instrument_token")["close"].median()
            med = med[med > 0]
            if not med.empty:
                if level:
                    tok = (med - level).abs().idxmin()
                    # Same 20% guard as session_moves: far wider than any
                    # session gap, far narrower than the distance to a
                    # volatility index. A series that is not near the money
                    # is rejected rather than used.
                    if abs(float(med.loc[tok]) - level) <= 0.20 * level:
                        ser = df[df["instrument_token"] == tok].sort_values("ts")
                        spot = float(ser["close"].astype(float).iloc[-1])
                cand = med[(med >= VIX_PLAUSIBLE[0]) & (med <= VIX_PLAUSIBLE[1])]
                # Exactly one, or none. Two series in the volatility range
                # means the partition is not what this function assumes, and
                # guessing between them is how a wrong VIX gets a source
                # label that says it came from the session.
                if len(cand) == 1:
                    ser = df[df["instrument_token"] == cand.index[0]].sort_values("ts")
                    vix = float(ser["close"].astype(float).iloc[-1])
        elif level:
            ser = df.sort_values("ts")["close"].astype(float)
            if len(ser) and abs(float(ser.iloc[-1]) - level) <= 0.20 * level:
                spot = float(ser.iloc[-1])

    src = "session-close" if spot is not None else "parity"
    if spot is None:
        spot = level
    if spot is None:
        return None, None, "unavailable"

    if underlying == "NIFTY" and vix is not None:
        return spot, vix, src + "+session-vix"

    if chain_rows:
        iv = M.atm_iv(priced(chain_rows), spot, max(dte, 1) / 365.0)
        if iv:
            return spot, iv * 100.0, src + "+chain-atm-iv"

    # Deliberately NOT last_known_vix() here: that reads the most recent row
    # of the journal, which is today's, and handing it to a replayed session
    # is the exact substitution this function exists to prevent.
    return spot, None, src + "+no-vol-for-that-session"


def market_ref(underlying, chain_rows, dte, day=None):
    """(spot, volatility in VIX units, source) for one underlying.

    India VIX is a NIFTY volatility index. Feeding it to a BANKNIFTY structure
    would understate that index's volatility by a wide margin and produce a
    confidently wrong POP -- so only NIFTY uses it, which also keeps the
    workbook reproduced exactly. Everything else is priced off its own chain's
    at-the-money implied volatility, which is the honest number for that
    underlying and needs no external feed at all.
    """
    # Replay: anything but the newest session must be valued from its own
    # bars. The live quote below is only ever right for the session that is
    # still running.
    if day is not None and day != latest_session():
        return session_ref(day, underlying, chain_rows, dte)

    spot, src = None, "unavailable"
    try:
        q = quotes()
        sym = INDEX_SYMBOL.get(underlying)
        if sym and sym in q:
            spot, src = q[sym], "kite"
    except Exception:
        pass
    if spot is None:
        import journal
        spot = journal.implied_level(priced(chain_rows)) if chain_rows else None
        src = "parity" if spot else "unavailable"

    if underlying == "NIFTY":
        try:
            return spot, quotes()[VIX_SYMBOL], src + "+india-vix"
        except Exception:
            pass

    if spot and chain_rows:
        iv = M.atm_iv(priced(chain_rows), spot, max(dte, 1) / 365.0)
        if iv:
            return spot, iv * 100.0, src + "+chain-atm-iv"

    return spot, last_known_vix(), src + "+last-known-vix"


def spot_and_vix(chain_rows):
    """NIFTY shorthand, kept for the publisher's ticker."""
    def fetch():
        q = quotes()
        return (q.get(INDEX_SYMBOL["NIFTY"]), q.get(VIX_SYMBOL), "kite")
    try:
        # 4 seconds, not 20: the publisher's ticker fires every 5, and a cache
        # longer than that tick hands it the same price twice, so the hash does
        # not move and a live ticker looks frozen. One quote call per five
        # seconds is 0.2 requests a second -- far under anything Kite limits.
        return cached("ltp", 4, fetch)
    except Exception:
        # No Kite: recover the level from the chain itself. forward_from_parity
        # needs a spot to pick strikes near, which is the thing we are missing,
        # so use the journal's spot-free estimate -- median of K + C - P over
        # the strikes where |C - P| is smallest, which is the money.
        import journal
        lvl = journal.implied_level(priced(chain_rows)) if chain_rows else None
        vix = last_known_vix()
        return (lvl, vix, "parity+last-known-vix" if lvl else "unavailable")


def last_known_vix():
    try:
        df = pd.read_csv(JOURNAL_CSV)
        return float(df["vix"].dropna().iloc[-1])
    except Exception:
        return None


# ------------------------------------------------------------------ endpoints

@app.get("/api/health")
def health():
    days = sessions()
    if not days:
        return {"ok": False, "reason": "no sessions collected"}

    day = days[-1]
    df = read_parts(day, "NIFTY")
    now = datetime.now(IST)
    out = {
        "ok": True,
        "server_time": now.isoformat(timespec="seconds"),
        "sessions_collected": len(days),
        "first_session": str(days[0]),
        "latest_session": str(day),
        "soak_target": 10,
    }
    if df is None or df.empty:
        out.update({"ok": False, "reason": f"no NIFTY bars for {day}"})
        return out

    last = as_ist(df["ts"].max())
    lag = (now - last).total_seconds()
    out.update({
        "last_bar": last.isoformat(timespec="seconds"),
        "lag_seconds": int(lag),
        "bars_today": int(len(df)),
        "contracts": int(df["instrument_token"].nunique()),
        "minutes": int(df["ts"].nunique()),
        "expiries": sorted(str(e) for e in df["expiry"].unique()),
    })

    # 09:15 to 15:30 IST. Outside it, lag is expected and not a fault.
    open_now = (day == now.date()
                and now.time() >= datetime.strptime("09:15", "%H:%M").time()
                and now.time() <= datetime.strptime("15:35", "%H:%M").time())
    out["market_open"] = open_now
    out["stale"] = bool(open_now and lag > 600)
    return out


@app.get("/api/sessions")
def api_sessions():
    return {"sessions": [str(d) for d in sessions()]}


@app.get("/api/underlyings")
def api_underlyings():
    day = latest_session()
    out = []
    for u in underlyings(day):
        bars = last_bars(day, u)
        lot, lot_src = lot_for(u, day)
        out.append({
            "underlying": u,
            "contracts": int(len(bars)) if bars is not None else 0,
            "expiries": int(bars["expiry"].nunique()) if bars is not None else 0,
            "lot": lot, "lot_source": lot_src,
            "index_symbol": INDEX_SYMBOL.get(u),
        })
    return {"session": str(day) if day else None, "underlyings": out}


def today_ist():
    """The exchange's today. One definition, so two endpoints cannot disagree
    about what day it is when the box runs on UTC and the market does not."""
    return datetime.now(IST).date()


def dte_for(session, expiry):
    """Days of risk the PRICED session carried -- NOT days from now.

    Every sigma on this site is spot * (vol/100) * sqrt(dte/365), and the
    number it gets compared against -- the credit -- is taken from `session`'s
    closing chain. Counting the days from today instead pairs one day's
    premium with another day's clock. Intraday the two dates are the same and
    this function changes nothing; the moment the newest session is not today
    -- a weekend, a holiday, a collector outage -- they diverge and sigma is
    wrong by sqrt(dte_from_today / dte_from_session) with nothing on the page
    to say so.

    Measured on Sunday 2026-09-20 against the Friday 2026-09-18 session, which
    is how this was found: the front NIFTY weekly displayed sigma 196.84 where
    the session had priced 278.37, and credit/sigma read 0.8934 against a
    0.9346 gate when the honest figure was 0.6317. It is a fixed two-day
    offset underneath a square root, so the error is worst at the shortest
    horizons -- exactly where the weekly test claimed 78.2% and delivered
    57.9%.

    Clamped at zero because an expiry that has already passed carries no
    remaining risk, and a negative number under the square root is a crash
    rather than a wrong answer.
    """
    return max((expiry - session).days, 0)


# NSE and BSE index options settle at the close. After this, on its own
# expiry day, a contract is finished: its last quotes are the thin,
# one-sided book of a dying contract, and the page must move on to the next
# expiry instead of pricing that. Before this fix the whole site kept pointing
# at the settled weekly all evening -- on 22 Sep 2026 the builder, the
# structures and P(stop) all went blank on "chain too thin for 2026-09-22".
SETTLE_HHMM = "15:30"


def is_settled(expiry, now=None):
    """Has this expiry stopped trading? True from 15:30 IST on the day."""
    # The DATE comes from today_ist(), the one definition of the exchange's
    # today (and what the tests freeze); the time of day from the clock.
    today = now.date() if now is not None else today_ist()
    if expiry != today:
        return expiry < today
    return (now or datetime.now(IST)).strftime("%H:%M") >= SETTLE_HHMM


def live_expiries(day, exps, now=None):
    """The expiries a viewer can still trade, in order.

    On the NEWEST session: those not settled as of now. On a REPLAYED
    session: those not yet expired on that session's date -- the question a
    replay asks is what that day was trading, not what is alive today.
    """
    exps = sorted(exps)
    if day == latest_session():
        live = [e for e in exps if not is_settled(e, now)]
    else:
        live = [e for e in exps if e >= day]
    return live or exps


@app.get("/api/expiries")
def api_expiries(underlying: str = "NIFTY", session: str = Query(None)):
    day = pick_session(session)
    bars = last_bars(day, underlying)
    if bars is None:
        raise HTTPException(404, f"no {underlying} bars for {day}")
    today = today_ist()
    exps = sorted(bars["expiry"].unique())
    # Two different questions, and they only have the same answer while the
    # newest session IS today. `dte` is the horizon the prices were struck
    # over, which is what every sigma divides by. `dte_from_today` is whether
    # the contract is still alive, which is what a dropdown should filter on.
    return {
        "session": str(day),
        "today_ist": str(today),
        "replay": day != latest_session(),
        "expiries": [{"expiry": str(e),
                      "dte": dte_for(day, e),
                      "dte_from_today": (e - today).days,
                      # Settled = past 15:30 IST on its own day (or earlier).
                      # Only meaningful on the live session; a replay filters
                      # on `dte` instead.
                      "settled": (day == latest_session()) and is_settled(e),
                      "contracts": int((bars["expiry"] == e).sum())}
                     for e in exps],
    }


def pick_session(session):
    """The session to serve: the newest, or a named one that was collected.

    A date that was never collected is a 404 rather than a silent fall back to
    the newest. Falling back is how a replay of a day with no bars quietly
    becomes a view of today wearing that day's label in the URL.
    """
    days = sessions()
    if not days:
        raise HTTPException(404, "no sessions collected")
    if not isinstance(session, str) or not session.strip():
        return days[-1]
    try:
        want = date.fromisoformat(session.strip())
    except ValueError:
        raise HTTPException(400, f"session {session!r} is not an ISO date")
    if want not in days:
        raise HTTPException(404, f"no session collected for {want}")
    return want


def resolve(underlying, expiry, session=None):
    day = pick_session(session)
    bars = last_bars(day, underlying)
    if bars is None:
        raise HTTPException(404, f"no {underlying} bars for {day}")
    today = today_ist()
    # `expiry` arrives as a string over HTTP, but as FastAPI's Query sentinel
    # when these functions are called directly -- which publisher.py and the
    # tests both do. Treat anything that is not a real date string as absent
    # rather than letting fromisoformat raise on a Query object.
    if not isinstance(expiry, str) or not expiry.strip():
        expiry = None
    if expiry:
        want = date.fromisoformat(expiry)
    else:
        # SELECTION is anchored to today on the newest session: an expiry
        # that has already settled is not a contract anyone can trade,
        # however good its last prices looked.
        #
        # On a REPLAYED session it is anchored to that session instead,
        # because the question has changed. Replaying 3 September and being
        # offered only the expiries still live today would hide the weekly
        # that session was actually trading -- the one thing someone
        # replaying it came to see.
        #
        # MEASUREMENT is anchored to the session either way -- see dte_for.
        # "Live" now also means "not settled": from 15:30 IST on expiry day
        # the default is the NEXT expiry -- see is_settled.
        want = live_expiries(day, bars["expiry"].unique())[0]
    return day, want, dte_for(day, want)


@app.get("/api/chain")
def api_chain(underlying: str = "NIFTY", expiry: str = Query(None),
              width: int = 20, sigma_source: str = SIG.DEFAULT,
              session: str = Query(None)):
    day, exp, dte = resolve(underlying, expiry, session)
    rows, dropped, total, last_ts = build_chain(day, underlying, exp)
    if not rows:
        raise HTTPException(404, f"no usable chain for {exp}")

    flat = priced(rows)
    spot, vix, src = market_ref(underlying, rows, dte, day)
    fwd = M.forward_from_parity(flat, spot or 0.0)
    ref = spot or fwd
    T = max(dte, 1) / 365.0
    step = infer_step([r["strike"] for r in rows])

    atm = min(rows, key=lambda r: abs(r["strike"] - (fwd or ref)))
    keep = [r for r in rows
            if abs(r["strike"] - atm["strike"]) <= width * step]

    # The workbook's probability engine is a NORMAL terminal distribution with
    # an absolute sigma, so the per-strike block needs both that sigma and the
    # ordinary annualised one the Greeks use. Two different volatility objects
    # for two different models, which is the workbook's design, not a slip.
    lot, _lot_src = lot_for(underlying, day)
    try:
        sig = SIG.resolve(sigma_source, ref or 0, dte, underlying=underlying,
                          vix_units=vix,
                          chain_iv_units=chain_iv_units(flat, ref, dte),
                          session_moves=session_moves(ref))
    except SIG.UnknownSource as e:
        raise HTTPException(422, str(e))
    sigma_pts = sig["sigma"] or 0.0

    out = []
    for r in keep:
        row = {"strike": r["strike"],
               "atm": r["strike"] == atm["strike"],
               "moneyness": round(r["strike"] / (fwd or ref) - 1.0, 5)}
        ivs = {}
        for kind in ("CE", "PE"):
            q = dict(r[kind])
            q["half_spread"] = round(half_spread_pts(r["strike"], ref), 2)
            iv = M.implied_vol(q["mid"], fwd or ref, r["strike"], T, kind)
            q["iv"] = round(iv * 100, 2) if iv else None
            ivs[kind] = iv or 0.0
            row[kind] = q

        if ref and sigma_pts > 0:
            # The `vix` argument here is NOT a probability input -- it is the
            # Greeks' fallback for strikes where the per-strike IV solve
            # returned None (AH28/AI28). sigma_pts is what drives pmp, pop and
            # every EV column. So sigma_source moves the probabilities and
            # leaves the Greeks alone, which is the point: widening it would
            # silently reprice delta and theta too.
            blk = CM.strike_block(ref, r["strike"], row["CE"]["mid"],
                                  row["PE"]["mid"], ivs["CE"], ivs["PE"],
                                  (vix or 0) / 100.0, sigma_pts, T, lot)
            for kind, key in (("CE", "ce"), ("PE", "pe")):
                b = blk[key]
                row[kind].update({
                    "delta": round(b["delta"], 4),
                    "gamma": round(b["gamma"], 6),
                    "theta": round(b["theta"], 2),
                    "vega": round(b["vega"], 3),
                    "iv_used": b["iv_used"],
                    "be": round(b["be"], 1),
                    "pmp": round(b["pmp"], 4),
                    "pop": round(b["pop"], 4),
                    "max_profit": round(b["max_profit"], 0),
                    "ev": round(b["ev"], 0),
                    "ev_fixed": round(b["ev_fixed"], 0),
                })
        out.append(row)

    return {
        "underlying": underlying, "session": str(day), "expiry": str(exp),
        "dte": dte, "today_ist": str(today_ist()),
        "replay": day != latest_session(),
        "as_of": as_ist(last_ts).isoformat(timespec="seconds") if last_ts is not None else None,
        "spot": round(spot, 2) if spot else None,
        "vix": round(vix, 2) if vix else None,
        "spot_source": src,
        "forward": round(fwd, 2) if fwd else None,
        "atm_strike": atm["strike"],
        "usable": len(rows), "returned": len(out),
        "dropped_wide": dropped["wide"], "dropped_one_sided": dropped["one_sided"],
        "contracts_seen": total,
        "step": step, "lot": lot,
        # For the builder's labelled margin estimate, so the page and the
        # scanner use one number (see catalogue.margin_estimate).
        "margin_per_lot": MARGIN_PER_LOT.get(
            underlying, MARGIN_PCT_NOTIONAL * (ref or 0) * lot),
        **sig,
        "sd1_low": round(ref - sigma_pts, 1) if ref else None,
        "sd1_high": round(ref + sigma_pts, 1) if ref else None,
        "sd2_low": round(ref - 2 * sigma_pts, 1) if ref else None,
        "sd2_high": round(ref + 2 * sigma_pts, 1) if ref else None,
        "rows": out,
    }


@app.get("/api/analytics")
def api_analytics(underlying: str = "NIFTY", expiry: str = Query(None),
                  session: str = Query(None)):
    """OI_Analytics, plus the statistical range the Dashboard sits beside it.

    Every input is already in the chain, so this endpoint is arithmetic over
    data the collector holds rather than anything new being fetched.
    """
    ch = api_chain(underlying=underlying, expiry=expiry, width=25,
                   session=session)
    rows = ch["rows"]
    if not rows:
        raise HTTPException(404, "no chain to analyse")

    ratios = CM.pcr(rows)
    lv = CM.levels(rows)
    mp, table = CM.max_pain(rows)
    spot = ch["spot"] or ch["forward"]

    return {
        "underlying": underlying, "expiry": ch["expiry"], "dte": ch["dte"],
        "as_of": ch["as_of"], "spot": spot, "vix": ch["vix"],
        "sigma": ch["sigma"],
        "range": {"sd1_low": ch["sd1_low"], "sd1_high": ch["sd1_high"],
                  "sd2_low": ch["sd2_low"], "sd2_high": ch["sd2_high"],
                  "width": round(2 * (ch["sigma"] or 0), 1)},
        "pcr": {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in ratios.items()},
        "levels": lv,
        "max_pain": mp,
        "distance_to_max_pain": round(mp - spot, 1) if (mp and spot) else None,
        # OI range wider than the statistical range means the market's own
        # positioning allows more room than the volatility model does.
        "oi_vs_stat": (round(lv["oi_range_width"] / (2 * ch["sigma"]), 3)
                       if lv.get("oi_range_width") and ch["sigma"] else None),
        "max_pain_table": [{"strike": t["strike"],
                            "ce_loss": round(t["ce_loss"], 0),
                            "pe_loss": round(t["pe_loss"], 0),
                            "total": round(t["total"], 0)} for t in table],
    }


@app.get("/api/scan")
def api_scan(expiry: str = Query(None)):
    """Scanner -- every collected underlying ranked by EV per rupee of margin.

    The sheet's own note explains the choice: BANKNIFTY's credits look larger
    than NIFTY's simply because the index is larger, and comparing rupees of
    EV would pick it every time. Dividing by the margin it consumes is what
    makes the comparison mean anything.
    """
    out = []
    for u in underlyings():
        try:
            s = api_structures(underlying=u, expiry=None, lots=1)
        except HTTPException as e:
            out.append({"underlying": u, "error": str(e.detail)})
            continue
        best = max(s["structures"], key=lambda x: x["net_ev_rs"])
        out.append({
            "underlying": u, "spot": s["spot"], "vix": s["vix"],
            "vol_source": s["spot_source"], "expiry": s["expiry"],
            "dte": s["dte"], "lot": s["lot"],
            "straddle": s["straddle"], "c_over_sigma": s["c_over_sigma"],
            # Whether a gate APPLIES is now a per-underlying property, so the
            # scanner has to carry it too. Without this the page cannot tell
            # "shut" from "never asked" and renders every row as shut.
            "gate_passed": s["gate_passed"], "cs_gate": s["cs_gate"],
            "best": best["label"], "net_ev_rs": best["net_ev_rs"],
            "margin": best["margin"],
            "ev_per_rupee": best["roi"],
            "pop": best["pop"],
        })
    ok = [o for o in out if "error" not in o]
    ok.sort(key=lambda o: -(o["ev_per_rupee"] or 0))
    return {"scanned": len(out), "rows": ok + [o for o in out if "error" in o]}


@app.get("/api/world")
def api_world():
    """Global prices, tagged headlines and the hand-kept event calendar --
    context only; nothing here feeds a gate. See world.py."""
    import world
    return world.world()


@app.get("/api/structures")
def api_structures(underlying: str = "NIFTY", expiry: str = Query(None),
                   lots: int = 1, sigma_source: str = SIG.DEFAULT,
                   session: str = Query(None)):
    day, exp, dte = resolve(underlying, expiry, session)
    rows, dropped, _total, last_ts = build_chain(day, underlying, exp)
    if not rows or len(rows) < 15:
        raise HTTPException(404, f"chain too thin for {exp}")

    flat = priced(rows)
    spot, vix, src = market_ref(underlying, rows, dte, day)
    if not spot or not vix:
        raise HTTPException(503, f"no spot/volatility available ({src})")

    step = infer_step([r["strike"] for r in rows])
    lot, lot_src = lot_for(underlying, day)
    # The workbook's wings are 200 points on a 50-point NIFTY grid -- four
    # strikes out. Expressed that way it carries over to any underlying instead
    # of putting BANKNIFTY's wings two strikes from the short leg.
    wing = 4 * step
    margin_lot = MARGIN_PER_LOT.get(underlying, MARGIN_PCT_NOTIONAL * spot * lot)

    try:
        sig = SIG.resolve(sigma_source, spot, dte, underlying=underlying,
                          vix_units=vix,
                          chain_iv_units=chain_iv_units(flat, spot, dte),
                          session_moves=session_moves(spot))
    except SIG.UnknownSource as e:
        raise HTTPException(422, str(e))

    # build_all derives POP and every EV column from this volatility, so the
    # chosen source has to reach it -- changing only the displayed sigma would
    # leave the numbers underneath it unchanged and the label lying.
    vol_units = sig["sigma_vol_units"] if sig["sigma_vol_units"] else vix
    built = S.build_all(flat, spot, vol_units / 100.0, dte, step=step,
                        lot_size=lot, lots=lots, wing_width=wing,
                        sigma_mult=SIGMA_MULT, margin_per_lot=margin_lot,
                        costs=costs_for(underlying))

    sigma_pts = sig["sigma"] or 0.0
    straddle = built["short_straddle"].credit_pts

    # Calibrated POP (calpop.py): shown beside the model POP, authorises
    # nothing. NIFTY only, the underlying it was fitted on.
    cal = None
    if CP.available(underlying):
        try:
            import market as MK
            _f = M.forward_from_parity(flat, spot) or spot
            cal = {"forward": round(_f, 2),
                   "sigma_session": round(CP.sigma_session(spot, vol_units, day, exp,
                                                           MK.is_trading_day), 2)}
        except Exception as e:
            cal = {"error": f"{type(e).__name__}: {e}"}
    def pop_cal(lo, hi):
        if not cal or "error" in cal:
            return None
        return round(CP.pop_zone(lo, hi, cal["forward"], cal["sigma_session"]), 4)

    # The gate is only meaningful against VIX sigma -- see sigma_sources for
    # why the 0.9346 threshold does not survive a change of denominator. When
    # it does not apply, NOTHING is recommended: the gate is what authorises a
    # recommendation, and recommending on an uncalibrated basis would be worse
    # than recommending nothing.
    if sig["gate_applies"]:
        cs = straddle / sigma_pts if sigma_pts else 0.0
        best = S.best(built)
        if best is not None and cs < CS_GATE:
            best = None
    else:
        cs, best = None, None

    def pack(s):
        legs = [{"strike": l.strike, "kind": l.kind, "premium": round(l.premium, 2),
                 "qty": (1 if l.long else -1) * lots} for l in s.legs]
        return {
            "name": s.name, "label": s.label,
            "defined_risk": s.defined_risk,
            "ce_strike": s.ce_strike, "pe_strike": s.pe_strike,
            "long_ce_strike": s.long_ce_strike, "long_pe_strike": s.long_pe_strike,
            "credit_pts": round(s.credit_pts, 2), "credit_rs": round(s.credit_rs, 0),
            "max_loss_pts": round(s.max_loss_pts, 2),
            "lower_be": round(s.lower_be, 1), "upper_be": round(s.upper_be, 1),
            "pop": round(s.pop, 4),
            "pop_cal": pop_cal(s.lower_be, s.upper_be),
            "exp_payoff_pts": round(s.exp_payoff_pts, 2),
            "gross_ev_rs": round(s.gross_ev_rs, 0),
            "fees_rs": round(s.fees_rs, 0),
            "slippage_rs": round(s.slippage_rs, 0),
            "net_ev_rs": round(s.net_ev_rs, 0),
            "margin": round(s.margin, 0),
            "roi": round(s.net_ev_rs / s.margin, 5) if s.margin else 0.0,
            "recommended": best is not None and s.name == best.name,
            "p_stop": p_stop.get(s.name),
            # the workbook's number, min(1, 2*(1-POP)), kept for comparison
            "p_stop_workbook": round(P.probability_stop_touched(s.pop), 4),
            "legs": legs,
        }

    # P(stop): the barrier the sizing card actually uses -- see stop_touch.
    # Nearest expiry only; cached per collector flush.
    live_exps = live_expiries(day, last_bars(day, underlying)["expiry"].unique())

    # The catalogue (Bullish / Bearish / Neutral): scored, NEVER recommended.
    # Its own try, so a failure here costs the table and nothing else.
    try:
        cat_rows, cat_meta = catalogue_rows(underlying, day, exp, dte, rows,
                                            spot, sigma_pts, step, lot,
                                            margin_lot, live_exps, cal)
    except Exception as e:
        cat_rows, cat_meta = [], {"error": f"{type(e).__name__}: {e}"}
    cat_stop = [("cat:" + r["key"], r["net_premium_pts"],
                 [{"strike": l["strike"], "kind": l["kind"], "qty": l["qty"]}
                  for l in r["legs"]])
                for r in cat_rows
                if r.get("available") and CAT.stop_eligible(r["key"])
                and r["net_premium_pts"] > 0]
    try:
        import market as MK
        p_stop, p_stop_meta = STA.compute(
            underlying=underlying, day=day, expiry=exp,
            nearest=live_exps[0] if live_exps else None,
            last_ts_ist=as_ist(last_ts) if last_ts is not None else None,
            spot=spot, sigma_pts=sigma_pts,
            structures=[(s.name, s.credit_pts,
                         [{"strike": l.strike, "kind": l.kind,
                           "qty": 1 if l.long else -1} for l in s.legs])
                        for s in built.values()] + cat_stop,
            rows=rows, is_trading_day=MK.is_trading_day)
    except Exception as e:                       # never take the page down
        p_stop, p_stop_meta = {}, {"computed": False,
                                   "note": f"P(stop) failed: {type(e).__name__}: {e}"}

    for r in cat_rows:
        r["p_stop"] = p_stop.get("cat:" + r["key"]) if r.get("available") else None

    fwd = M.forward_from_parity(flat, spot)
    return {
        "underlying": underlying,
        "session": str(day), "expiry": str(exp), "dte": dte, "lots": lots,
        # Scored on the same chain and sigma as the seven, never recommended:
        # the entry rule was fitted on selling premium in the seven only.
        "catalogue": cat_rows, "catalogue_meta": cat_meta,
        # calpop.py: forward and session-clock sigma the calibrated POP uses.
        "calibration": cal,
        "p_stop_meta": p_stop_meta,
        "today_ist": str(today_ist()), "replay": day != latest_session(),
        "as_of": as_ist(last_ts).isoformat(timespec="seconds") if last_ts is not None else None,
        "spot": round(spot, 2), "vix": round(vix, 2), "spot_source": src,
        "step": step, "lot": lot, "lot_source": lot_src, "wing_width": wing,
        "forward": round(fwd, 2) if fwd else None,
        **sig,
        "straddle": round(straddle, 2),
        "fair": round(P.fair_straddle(sigma_pts), 2) if sigma_pts else None,
        "ratio": (round(P.rich_cheap_ratio(straddle, sigma_pts), 3)
                  if sigma_pts else None),
        "c_over_sigma": round(cs, 4) if cs is not None else None,
        "cs_gate": CS_GATE if sig["gate_applies"] else None,
        "gate_passed": (best is not None) if sig["gate_applies"] else None,
        "gate_reason": (sig["gate_note"] if not sig["gate_applies"] else
                        ("" if best is not None else
                         (f"premium too thin: credit/sigma {cs:.3f} below {CS_GATE}"
                          if cs < CS_GATE else
                          "nothing clears positive net EV once costs are paid"))),
        "structures": sorted([pack(s) for s in built.values()],
                             key=lambda d: -d["net_ev_rs"]),
    }


def catalogue_rows(underlying, day, exp, dte, rows, spot, sigma_pts, step, lot,
                   margin_lot, live_exps, cal=None):
    """Score catalogue.STRATEGIES on this chain (and the next expiry's, for
    the calendars). See catalogue.py for the model and its limits."""
    if not sigma_pts:
        return [], {"error": "no sigma"}
    flat = priced(rows)
    fwd = M.forward_from_parity(flat, spot) or spot
    atm = min(rows, key=lambda r: abs(r["strike"] - fwd))["strike"]
    chains = {exp: {}}
    for r in rows:
        chains[exp][(float(r["strike"]), "CE")] = r["CE"]["mid"]
        chains[exp][(float(r["strike"]), "PE")] = r["PE"]["mid"]
    expiries = [exp]
    sig_by = {exp: sigma_pts}
    fwd_by = {exp: fwd}
    later = [e for e in live_exps if e > exp]
    note = ""
    if later:
        nxt = later[0]
        nrows, _d, _t, _ts = build_chain(day, underlying, nxt)
        if nrows:
            chains[nxt] = {}
            for r in nrows:
                chains[nxt][(float(r["strike"]), "CE")] = r["CE"]["mid"]
                chains[nxt][(float(r["strike"]), "PE")] = r["PE"]["mid"]
            expiries.append(nxt)
            fwd_by[nxt] = M.forward_from_parity(priced(nrows), spot) or fwd
            # Same volatility, longer clock: sigma scales with sqrt(days),
            # the convention every sigma on the site already follows.
            d_n = max(dte_for(day, nxt), 1)
            sig_by[nxt] = sigma_pts * (d_n / max(dte, 1)) ** 0.5
        else:
            note = f"no usable chain for {nxt}; calendars unavailable"
    else:
        note = "no later expiry collected; calendars unavailable"
    # Centred on the parity FORWARD -- see catalogue.score.
    pop_fn = None
    if cal and "error" not in cal:
        pop_fn = lambda xs, ys: CP.pop_curve(xs, ys, cal["forward"], cal["sigma_session"])
    out = CAT.score_all(chains, expiries, fwd, atm, step, lot, sig_by,
                        margin_lot, costs=costs_for(underlying), fwd_by_exp=fwd_by,
                        pop_fn=pop_fn)
    return out, {"atm": atm, "width": CAT.default_width(step),
                 "centre": round(fwd, 2),
                 "expiries": [str(e) for e in expiries], "note": note,
                 "model": "Bachelier at the site's sigma; later-expiry legs "
                          "valued at their own sigma. Never recommended."}


@app.get("/api/intraday")
def api_intraday(underlying: str = "NIFTY", expiry: str = Query(None),
                 every: int = 1, session: str = Query(None)):
    """Per-minute parity forward and ATM straddle price for the session.

    Done vectorised, because the naive version -- loop the minutes, rebuild a
    chain, price it -- is 375 chain builds over 600k rows and would take longer
    than the page's refresh interval. Instead: pivot once to (ts, strike) with a
    call and put column, compute |C - P| per row, and let groupby find the
    money at each minute. Parity does the rest, with no volatility assumption
    anywhere in it.
    """
    day, exp, dte = resolve(underlying, expiry, session)

    def build():
        df = read_parts(day, underlying)
        if df is None or df.empty:
            return None
        sub = df[df["expiry"] == exp]
        if sub.empty:
            return None

        w = sub[["ts", "strike", "opt_type", "bid", "ask"]].copy()
        w = w[(w["bid"] > 0) & (w["ask"] >= w["bid"])]
        w["mid"] = 0.5 * (w["bid"] + w["ask"])
        piv = (w.pivot_table(index=["ts", "strike"], columns="opt_type",
                             values="mid", aggfunc="last")
                 .dropna(subset=["CE", "PE"]).reset_index())
        if piv.empty:
            return None

        piv["gap"] = (piv["CE"] - piv["PE"]).abs()
        piv["fwd"] = piv["strike"] + piv["CE"] - piv["PE"]
        piv["straddle"] = piv["CE"] + piv["PE"]
        atm = piv.loc[piv.groupby("ts")["gap"].idxmin()].sort_values("ts")
        return atm

    atm = cached(f"intraday:{day}:{underlying}:{exp}", 60, build)
    if atm is None:
        raise HTTPException(404, f"no intraday series for {exp}")

    step = max(1, int(every))
    rows = [{"ts": as_ist(r.ts).strftime("%H:%M"),
             "forward": round(float(r.fwd), 2),
             "straddle": round(float(r.straddle), 2),
             "atm_strike": float(r.strike)}
            for r in atm.iloc[::step].itertuples()]
    return {"session": str(day), "expiry": str(exp), "dte": dte,
            "today_ist": str(today_ist()), "replay": day != latest_session(),
            "points": len(rows), "rows": rows}


# -------------------------------------------------------------------- payoff

class Leg(BaseModel):
    strike: float
    kind: str            # CE | PE
    premium: float
    qty: int             # negative = sold


class PayoffReq(BaseModel):
    legs: list[Leg]
    spot: float
    sigma: float = 0.0        # absolute points, one expiry
    lot_size: int = DEFAULT_LOT
    dte: float = 1.0          # fractional, as the workbook's B13 computes it
    points: int = 161
    span: float = 0.08        # +/- fraction of spot to plot


def _intrinsic(level, strike, kind):
    return max(0.0, level - strike) if kind == "CE" else max(0.0, strike - level)


@app.post("/api/payoff")
def api_payoff(req: PayoffReq):
    """Expiry payoff for arbitrary legs, plus the numbers that decide it.

    Sign convention: qty < 0 is sold. P&L per unit at level L is
        sum over legs of qty * (intrinsic(L) - premium)
    so a sold option earns its premium and pays its intrinsic, which is the
    only convention that makes a four-leg condor come out right without
    special-casing wings.
    """
    if not req.legs:
        raise HTTPException(400, "no legs")
    lot = req.lot_size
    lo, hi = req.spot * (1 - req.span), req.spot * (1 + req.span)
    n = max(21, min(req.points, 601))
    step = (hi - lo) / (n - 1)

    net_premium = sum(-l.qty * l.premium for l in req.legs)   # credit if positive

    # An expiry payoff is piecewise linear with a kink at every strike. Sample
    # the strikes exactly as well as the even grid, or max profit comes back as
    # whatever the grid happened to land near -- 190.5 instead of 198.2 on a
    # straddle, which is a wrong number on the page, not a rounding artefact.
    levels = sorted(set(
        [lo + i * step for i in range(n)]
        + [l.strike for l in req.legs if lo <= l.strike <= hi]))

    curve = []
    for L in levels:
        pnl = sum(l.qty * (_intrinsic(L, l.strike, l.kind) - l.premium)
                  for l in req.legs)
        curve.append({"level": round(L, 1), "pnl_pts": round(pnl, 3),
                      "pnl_rs": round(pnl * lot, 0)})

    # Breakevens: sign changes on the sampled curve, refined by interpolation.
    bes = []
    for a, b in zip(curve, curve[1:]):
        if a["pnl_pts"] == 0:
            bes.append(a["level"])
        elif a["pnl_pts"] * b["pnl_pts"] < 0:
            t = -a["pnl_pts"] / (b["pnl_pts"] - a["pnl_pts"])
            bes.append(round(a["level"] + t * (b["level"] - a["level"]), 1))

    pnls = [c["pnl_pts"] for c in curve]
    max_p, max_l = max(pnls), min(pnls)
    # A naked short keeps losing past the edge of the window, so its "max loss"
    # is an artefact of `span` rather than a fact. A condor's wings flatten the
    # payoff, so its edge value IS the max loss. Tell them apart by the slope at
    # the boundary, not by whether the minimum happens to sit there -- a flat
    # plateau puts the minimum at the edge too, and calling that unbounded was
    # exactly the mistake this replaces.
    eps = 1e-6
    unbounded = (pnls[0] < pnls[1] - eps) or (pnls[-1] < pnls[-2] - eps)

    out = {
        "net_premium_pts": round(net_premium, 2),
        "net_premium_rs": round(net_premium * lot, 0),
        "breakevens": bes,
        "max_profit_pts": round(max_p, 2),
        "max_profit_rs": round(max_p * lot, 0),
        "max_loss_pts": round(max_l, 2),
        "max_loss_rs": round(max_l * lot, 0),
        "loss_unbounded": unbounded,
        "curve": curve,
    }

    if req.sigma > 0:
        lower = min(bes) if bes else lo
        upper = max(bes) if bes else hi
        out["pop_normal"] = round(
            P.probability_of_profit(req.spot, lower, upper, req.sigma), 4)
        T = max(req.dte, 1) / 365.0
        ann = M.annual_vol_from_points(req.sigma, req.spot, T)
        out["pop_lognormal"] = round(
            M.probability_of_profit(req.spot, lower, upper, ann, T), 4)
        # Expected payoff under the workbook's normal model, so EV lines up
        # with what strategies.py reports for the same structure.
        exp_pay = 0.0
        for l in req.legs:
            e = (P.expected_call(req.spot, l.strike, req.sigma) if l.kind == "CE"
                 else P.expected_put(req.spot, l.strike, req.sigma))
            exp_pay += -l.qty * e
        out["expected_payoff_pts"] = round(exp_pay, 2)
        out["gross_ev_rs"] = round((net_premium - exp_pay) * lot, 0)

    return out


# ------------------------------------------------------------------- journal

def records(df):
    """Rows as JSON-safe dicts.

    An unscored signal has NaN in every outcome column, and `json.dumps` refuses
    NaN outright -- the whole response 500s, not just that field. `.where(notnull)`
    looks like it fixes this and does not: on a float column pandas puts NaN
    back. Casting to object first is what actually converts them.
    """
    if df.empty:
        return []
    clean = df.replace([float("inf"), float("-inf")], pd.NA)
    return clean.astype(object).where(pd.notnull(clean), None).to_dict(orient="records")


def safe(x, nd=2):
    """A float that survives JSON, or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if v == v and abs(v) != float("inf") else None


@app.get("/api/signal")
def api_signal():
    if not os.path.exists(JOURNAL_CSV):
        raise HTTPException(404, "no journal yet")
    df = pd.read_csv(JOURNAL_CSV)
    if df.empty:
        raise HTTPException(404, "journal empty")
    day = sorted(df["signal_date"].unique())[-1]
    return {"signal_date": str(day),
            "rows": records(df[df["signal_date"] == day])}


@app.get("/api/journal")
def api_journal(limit: int = 400):
    if not os.path.exists(JOURNAL_CSV):
        return {"logged": 0, "scored": 0, "rows": [], "calibration": []}
    df = pd.read_csv(JOURNAL_CSV)
    if df.empty:
        return {"logged": 0, "scored": 0, "rows": [], "calibration": []}

    done = df[df["pnl_rs"].notna()].copy()
    calib = []
    if not done.empty:
        for name, g in done.groupby("structure"):
            unit = g["lot"] * g["lots"]
            calib.append({
                "structure": name, "n": int(len(g)),
                "pop": safe(g["pop"].mean() * 100, 1),
                "hit": safe(g["inside_zone"].astype(bool).mean() * 100, 1),
                "ev_unit": safe((g["net_ev_rs"] / unit).mean(), 2),
                "real_unit": safe(g["pnl_per_unit"].mean(), 2),
            })
        calib.sort(key=lambda d: -d["n"])

    rec = done[done["recommended"].astype(bool)] if not done.empty else done
    return {
        "logged": int(df["signal_date"].nunique()),
        "scored": int(done["signal_date"].nunique()) if not done.empty else 0,
        "recommended_n": int(len(rec)),
        "recommended_pop": safe(rec["pop"].mean() * 100, 1) if len(rec) else None,
        "recommended_hit": (safe(rec["inside_zone"].astype(bool).mean() * 100, 1)
                            if len(rec) else None),
        "recommended_ev": safe(rec["net_ev_rs"].mean(), 0) if len(rec) else None,
        "recommended_real": safe(rec["pnl_rs"].mean(), 0) if len(rec) else None,
        "sample_floor": 20,
        "calibration": calib,
        "rows": records(df.tail(limit)),
    }


# ------------------------------------------------------------------ routers
#
# series_api and daytrade_api live in their own modules so this file does not
# grow another four hundred lines. They import `api` lazily INSIDE their
# handlers, because the imports below are circular by construction -- by the
# time a request arrives, api is fully initialised in sys.modules.
#
# These two lines belong in the file, not in a shell session on the server.
# They were once added by hand on the box, which left the working copy and the
# deployed copy disagreeing, and the disagreement was silent: publisher.py
# imports both modules DIRECTLY and kept writing their Firebase nodes, so the
# live site looked perfectly healthy while /api/series and /api/daytrade
# returned 404 to anything talking to the API. Every subsequent deploy of this
# file quietly removed the routes again.
import daytrade_api                                          # noqa: E402
import series_api                                            # noqa: E402
import term_api                                              # noqa: E402

app.include_router(series_api.router)
app.include_router(daytrade_api.router)
app.include_router(term_api.router)


# ---------------------------------------------------------------------- web

@app.get("/")
def index():
    path = os.path.join(WEB, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "web/index.html missing"}, status_code=404)
    return FileResponse(path)


if os.path.isdir(WEB):
    app.mount("/static", StaticFiles(directory=WEB), name="static")

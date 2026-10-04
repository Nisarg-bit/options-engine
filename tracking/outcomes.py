"""Outcome store (roadmap step 6): what actually happened to every decision.

The feature store (features.py) keeps what the market looked like when a
decision was made. This keeps what happened next, joined on the same
`decision_id`, so "which conditions came before the bad weeks?" becomes a
table instead of a memory.

One row per settled DAILY signal x structure (the journal logs all seven).
Paper day-trades already record their own path in daytrade.py; they are
linked by decision_id in step 5b, not duplicated here.

WHAT IS MEASURED
  at expiry    P&L and inside-zone straight from the journal (already scored)
  move         (settlement - forward) / sigma, the realised move in the
               site's own sigma units
  path         every minute from the signal day's 09:15 to expiry, marked at
               COST TO CLOSE -- buy the shorts back at the ask, sell the wings
               at the bid (intraday_vrp.py's convention: the only mark that
               tells a short position the truth). Worst adverse and best
               favourable excursion, and when.
  exit rules   each one on its own, first minute it would have fired:
                 stop_30/50/100/200   cost to close >= credit x 1.3/1.5/2/3
                 target_50/80         cost to close <= credit x 0.5/0.2
                 time                 15:15 IST on the session before expiry
                 house                target_80, else stop_100, else time --
                                      a STARTING point, stated so the store
                                      can show which stop actually worked
                 signal_reverse       blank until the phase-2 state reader
               P&L per rule = (credit - exit cost) x units - the journal's
               fees and slippage. Those are the journal's round-trip
               estimate; for an early exit they are approximate.

KNOWN LIMITS, KEPT VISIBLE RATHER THAN HIDDEN
  * The credit is the journal's: the previous session's closing mids. The
    first mark is 09:15 on the signal day at the ask, so the overnight move
    and the spread land in `open_gap_pts`, not silently in the stop.
  * A minute where any leg lacks a two-sided quote is skipped, not guessed.
  * Sessions the collector only half-recorded (1, 23, 25 Sep 2026) are
    flagged: `coverage` < 1 and `partial` true. A stop that fired in a
    missing morning is invisible -- such rows must not be trusted for stops.

    python outcomes.py              score newly settled signals
    python outcomes.py --backfill   recompute every settled signal
    python outcomes.py --show [N]
"""
import project_paths
import glob, os, sys
from datetime import date, datetime, time as dtime, timedelta, timezone

import pandas as pd

ROOT = project_paths.ROOT
PATH = os.path.join(ROOT, "data", "features", "outcomes.csv")
JOURNAL = os.path.join(ROOT, "data", "journal", "signals.csv")
BARS = os.path.join(ROOT, "data", "bars_1m")
OUTCOME_VERSION = 1
MIN_PER_SESSION = 375
TIME_EXIT_UTC = dtime(9, 45)            # 15:15 IST

STOPS = {"stop_30": 0.3, "stop_50": 0.5, "stop_100": 1.0, "stop_200": 2.0}
TARGETS = {"target_50": 0.5, "target_80": 0.8}
RULES = list(STOPS) + list(TARGETS) + ["time", "house"]

FIELDS = (["decision_id", "structure", "signal_date", "expiry", "recommended",
           "gate_passed", "credit_pts", "units", "pnl_expiry_rs", "inside_zone",
           "final_level", "move_sigma", "abs_move_sigma",
           "sessions", "minutes", "coverage", "partial", "open_gap_pts",
           "mae_pts", "mae_rs", "mae_at", "mfe_pts", "mfe_at"]
          + [f"{r}_{x}" for r in RULES for x in ("at", "cost", "pnl_rs")]
          + ["house_reason", "signal_reverse_pnl_rs", "outcome_version", "computed_at"])


# ---------------------------------------------------------------- core

def legs_of(row):
    """(shorts, longs) as [(strike, 'CE'|'PE')]."""
    shorts = [(float(row["ce_strike"]), "CE"), (float(row["pe_strike"]), "PE")]
    longs = []
    if str(row.get("defined_risk")).lower() in ("true", "1"):
        for k, ot in (("long_ce_strike", "CE"), ("long_pe_strike", "PE")):
            if pd.notna(row.get(k)):
                longs.append((float(row[k]), ot))
    return shorts, longs


def close_cost(q, shorts, longs):
    """Cost to close: shorts at the ask, wings sold at the bid. None if unquoted."""
    total = 0.0
    for leg in shorts:
        ba = q.get(leg)
        if not ba or not ba[1] or ba[1] <= 0:
            return None
        total += ba[1]
    for leg in longs:
        ba = q.get(leg)
        if not ba or ba[0] is None or ba[0] < 0:
            return None
        total -= ba[0]
    return total


def mark_path(minutes, shorts, longs):
    """minutes: [(ts, {(strike, type): (bid, ask)})] -> [(ts, cost)]."""
    out = []
    for ts, q in minutes:
        c = close_cost(q, shorts, longs)
        if c is not None:
            out.append((ts, c))
    return out


def evaluate(credit, path, time_exit_ts=None):
    """First firing of each rule. Returns {rule: (ts, cost) or None}."""
    res = {r: None for r in RULES}
    for ts, c in path:
        for r, k in STOPS.items():
            if res[r] is None and c >= credit * (1 + k):
                res[r] = (ts, c)
        for r, f in TARGETS.items():
            if res[r] is None and c <= credit * (1 - f):
                res[r] = (ts, c)
    if time_exit_ts is not None:
        before = [(ts, c) for ts, c in path if ts <= time_exit_ts]
        if before:
            res["time"] = before[-1]
    cands = [(v[0], name, v) for name, v in (("target_80", res["target_80"]),
             ("stop_100", res["stop_100"]), ("time", res["time"])) if v]
    if cands:
        ts, name, v = min(cands, key=lambda x: x[0])
        res["house"] = v
        res["_house_reason"] = name
    else:
        res["_house_reason"] = "held_to_expiry"
    return res


# ------------------------------------------------------------------ io

_cache = {}


def session_minutes(day, expiry, bars_root=BARS):
    """[(ts, {(strike, type): (bid, ask)})] for one session and one expiry."""
    key = (day, expiry, bars_root)
    if key in _cache:
        return _cache[key]
    files = glob.glob(os.path.join(bars_root, f"date={day}", "underlying=NIFTY", "*.parquet"))
    out = []
    if files:
        cols = ["ts", "expiry", "strike", "opt_type", "bid", "ask"]
        df = pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)
        df = df[pd.to_datetime(df["expiry"]).dt.date == expiry]
        for ts, g in df.groupby("ts"):
            out.append((pd.Timestamp(ts).to_pydatetime(),
                        {(float(k), ot): (b, a) for k, ot, b, a in
                         zip(g["strike"], g["opt_type"], g["bid"], g["ask"])}))
        out.sort(key=lambda x: x[0])
    _cache[key] = out
    return out


def sessions_between(a, b, bars_root=BARS):
    """Collected session dates from a to b inclusive (from the bar folders)."""
    have = sorted(date.fromisoformat(p.split("date=")[1])
                  for p in glob.glob(os.path.join(bars_root, "date=*")))
    return [d for d in have if a <= d <= b]


def expected_sessions(a, b):
    """NSE trading days from a to b inclusive -- what a full path should span."""
    try:
        import market
        return sum(1 for d in pd.date_range(a, b) if market.is_trading_day(d.date()))
    except Exception:  # noqa: BLE001 -- weekdays is a safe over-count
        return len(pd.bdate_range(a, b))


def outcome_row(r, bars_root=BARS):
    sig, exp = pd.to_datetime(r["signal_date"]).date(), pd.to_datetime(r["expiry"]).date()
    units = float(r["lot"]) * float(r["lots"])
    credit = float(r["credit_pts"])
    costs = float(r["fees_rs"] or 0) + float(r["slippage_rs"] or 0)
    shorts, longs = legs_of(r)
    days = sessions_between(sig, exp, bars_root)
    minutes = [m for d in days for m in session_minutes(d, exp, bars_root)]
    path = mark_path(minutes, shorts, longs)
    before_exp = [d for d in days if d < exp]
    t_exit = (datetime.combine(before_exp[-1], TIME_EXIT_UTC, tzinfo=timezone.utc)
              if before_exp else None)
    ev = evaluate(credit, path, t_exit)

    n_sessions = expected_sessions(sig, exp)
    row = {"decision_id": f"daily:{sig}:{exp}", "structure": r["structure"],
           "signal_date": sig, "expiry": exp, "recommended": r.get("recommended"),
           "gate_passed": r.get("gate_passed"), "credit_pts": credit, "units": units,
           "pnl_expiry_rs": r.get("pnl_rs"), "inside_zone": r.get("inside_zone"),
           "final_level": r.get("final_level"), "sessions": len(days),
           "minutes": len(path),
           "outcome_version": OUTCOME_VERSION,
           "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if pd.notna(r.get("final_level")) and r.get("sigma"):
        m = (float(r["final_level"]) - float(r["forward"])) / float(r["sigma"])
        row.update(move_sigma=round(m, 4), abs_move_sigma=round(abs(m), 4))
    expected = max(n_sessions, 1) * MIN_PER_SESSION
    row["coverage"] = round(len(path) / expected, 3)
    row["partial"] = row["coverage"] < 0.95
    if path:
        worst = max(path, key=lambda x: x[1]); best = min(path, key=lambda x: x[1])
        row.update(open_gap_pts=round(path[0][1] - credit, 2),
                   mae_pts=round(worst[1] - credit, 2), mae_rs=round((worst[1] - credit) * units, 0),
                   mae_at=worst[0].isoformat(), mfe_pts=round(credit - best[1], 2),
                   mfe_at=best[0].isoformat())
    for rule in RULES:
        v = ev.get(rule)
        if v:
            row[f"{rule}_at"] = v[0].isoformat()
            row[f"{rule}_cost"] = round(v[1], 2)
            row[f"{rule}_pnl_rs"] = round((credit - v[1]) * units - costs, 0)
        elif rule in ("time",):
            pass
        elif pd.notna(r.get("pnl_rs")):
            # never fired: the trade was held to expiry under this rule
            row[f"{rule}_pnl_rs"] = round(float(r["pnl_rs"]), 0)
    row["house_reason"] = ev["_house_reason"]
    return {c: row.get(c) for c in FIELDS}


def load(path=PATH):
    if not os.path.exists(path):
        return pd.DataFrame(columns=FIELDS)
    return pd.read_csv(path)


def run(backfill=False, journal_path=JOURNAL, bars_root=BARS, out=PATH):
    j = pd.read_csv(journal_path)
    j = j[j["pnl_rs"].notna()]
    have = load(out)
    done = set(zip(have["decision_id"].astype(str), have["structure"].astype(str)))
    rows, fails = [], 0
    for _, r in j.iterrows():
        key = (f"daily:{pd.to_datetime(r['signal_date']).date()}:"
               f"{pd.to_datetime(r['expiry']).date()}", str(r["structure"]))
        if key in done and not backfill:
            continue
        try:
            rows.append(outcome_row(r, bars_root))
        except Exception as e:  # noqa: BLE001 -- one bad row never stops the rest
            fails += 1
            print(f"outcomes: {key} failed: {type(e).__name__}: {e}")
    if rows:
        new = pd.DataFrame(rows, columns=FIELDS)
        keys = set(zip(new["decision_id"].astype(str), new["structure"].astype(str)))
        keep = have[[k not in keys for k in zip(have["decision_id"].astype(str),
                                                 have["structure"].astype(str))]]
        df = new if keep.empty else pd.concat([keep, new], ignore_index=True)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        df.sort_values(["decision_id", "structure"]).to_csv(out, index=False)
    print(f"outcomes v{OUTCOME_VERSION}: {len(rows)} rows written, {fails} failed")
    return rows


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--show" in a:
        n = int(a[a.index("--show") + 1]) if len(a) > a.index("--show") + 1 else 7
        with pd.option_context("display.max_columns", None, "display.width", 250):
            print(load().tail(n).T)
    else:
        run(backfill="--backfill" in a)

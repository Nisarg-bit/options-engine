"""Nightly check: our collected bars vs Kite's official daily candles."""
import os, sys, glob, time, random
from datetime import date, datetime, timedelta
import pyarrow.parquet as pq
from session import get_kite
from aggregator import rollup
import notify

CLOSE_TOL = 0.005     # 0.5%
VOL_TOL   = 0.05      # 5%
OI_TOL    = 0.01      # 1%
RATE_SLEEP = 0.34     # historical endpoint allows ~3 req/sec


def compare_row(ours, theirs):
    """Pure function - returns list of problem strings. Testable offline."""
    problems = []

    if theirs is None:
        if (ours.get("volume") or 0) > 0:
            problems.append("missing_in_kite")
        return problems

    def rel(a, b):
        if a is None or b is None:
            return None
        if b == 0:
            return 0.0 if a == 0 else 1.0
        return abs(a - b) / abs(b)

    d = rel(ours.get("close"), theirs.get("close"))
    if d is not None and d > CLOSE_TOL:
        problems.append(f"close {ours['close']} vs {theirs['close']}")

    d = rel(ours.get("volume"), theirs.get("volume"))
    if d is not None and d > VOL_TOL:
        problems.append(f"volume {ours['volume']} vs {theirs['volume']}")

    d = rel(ours.get("oi"), theirs.get("oi"))
    if d is not None and d > OI_TOL:
        problems.append(f"oi {ours['oi']} vs {theirs['oi']}")

    return problems


def load_our_daily(d):
    """Daily bars derived from the canonical 1m tier."""
    files = glob.glob(f"data/bars_1m/date={d}/**/*.parquet", recursive=True)
    if not files:
        return {}
    rows = []
    for f in files:
        rows.extend(pq.read_table(f).to_pylist())

    by_token = {}
    for r in rows:
        by_token.setdefault(r["instrument_token"], []).append(r)

    out = {}
    for tok, bars in by_token.items():
        merged = rollup(bars)
        merged["tradingsymbol"] = bars[0]["tradingsymbol"]
        out[tok] = merged
    return out


def run(d=None, sample=None):
    d = d or date.today()
    ours = load_our_daily(d)
    if not ours:
        msg = f"RECONCILE {d}: no collected data found"
        print(msg); notify.send(msg)
        return

    tokens = list(ours)
    if sample and sample < len(tokens):
        random.seed(42)
        tokens = random.sample(tokens, sample)

    kite = get_kite()
    checked = matched = 0
    issues = []

    print(f"reconciling {len(tokens):,} instruments for {d} ...")
    for i, tok in enumerate(tokens, 1):
        try:
            candles = kite.historical_data(tok, d, d, "day", oi=True)
            theirs = candles[0] if candles else None
        except Exception as e:
            issues.append((ours[tok]["tradingsymbol"], [f"api_error {e}"]))
            time.sleep(RATE_SLEEP)
            continue

        checked += 1
        probs = compare_row(ours[tok], theirs)
        if probs:
            issues.append((ours[tok]["tradingsymbol"], probs))
        else:
            matched += 1

        if i % 100 == 0:
            print(f"  {i}/{len(tokens)} ...")
        time.sleep(RATE_SLEEP)

    pct = 100 * matched / checked if checked else 0
    lines = [f"<b>RECONCILE {d}</b>",
             f"checked  : {checked:,}",
             f"matched  : {matched:,} ({pct:.1f}%)",
             f"issues   : {len(issues):,}"]
    for sym, probs in issues[:10]:
        lines.append(f"  {sym}: {'; '.join(probs)}")
    if len(issues) > 10:
        lines.append(f"  ... and {len(issues) - 10} more")

    report = "\n".join(lines)
    print(report.replace("<b>", "").replace("</b>", ""))
    notify.send(report)
    return issues


if __name__ == "__main__":
    args = sys.argv[1:]
    d = None
    sample = None
    for a in args:
        if a.startswith("--sample="):
            sample = int(a.split("=")[1])
        elif not a.startswith("--"):
            d = date.fromisoformat(a)
    run(d, sample)
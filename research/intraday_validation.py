"""Chronological daily intraday experiment; never promotes a winning backtest.

Run with project imports configured: python research/intraday_validation.py.
All candidates enter once every data-valid session and exit the same day.
Original raw bars and journals are never changed. The last third is holdout.
"""
import argparse
import json
from pathlib import Path
import intraday_vrp as V

CANDIDATES = (
    {"name": "baseline", "entry": "09:20", "exit_at": "15:15", "width": 0},
    {"name": "later_entry", "entry": "09:45", "exit_at": "15:15", "width": 0},
    {"name": "shorter_window", "entry": "09:45", "exit_at": "14:30", "width": 0},
    {"name": "two_strike_strangle", "entry": "09:20", "exit_at": "15:15", "width": 2},
)


def metrics(rows):
    nets = [r["net_rs"] for r in rows]
    total = peak = drawdown = 0.0
    for n in nets:
        total += n
        peak = max(peak, total)
        drawdown = min(drawdown, total - peak)
    wins = sum(n for n in nets if n > 0)
    losses = -sum(n for n in nets if n < 0)
    return {"sessions": len(nets), "net": round(total, 2),
            "mean": round(total / len(nets), 2) if nets else None,
            "max_drawdown": round(drawdown, 2),
            "profit_factor": wins / losses if losses else None,
            "worst": min(nets) if nets else None}


def run(underlying="NIFTY", lot=65):
    days = sorted(V.sessions(underlying))
    split = len(days) * 2 // 3
    results = {c["name"]: [] for c in CANDIDATES}
    excluded = []
    for day in days:
        bm, expiry, error = V.load(day, underlying)
        if error:
            excluded.append({"date": str(day), "reason": str(error)})
            continue
        sims = {}
        for candidate in CANDIDATES:
            params = {k: v for k, v in candidate.items() if k != "name"}
            sims[candidate["name"]] = V.simulate(bm, day, expiry, lot=lot,
                costs=V.P.current_costs_for(underlying), **params)
        # Paired comparisons: every candidate must be measurable on the same day.
        if not all(s and s["sample_eligible"] for s in sims.values()):
            excluded.append({"date": str(day), "reason": "incomplete paired quote coverage"})
            continue
        for name, sim in sims.items():
            results[name].append(sim)
    boundary = days[split] if days else None
    report = {"status": "research_only" if days else "blocked_missing_minute_bars",
              "live_approved": False, "underlying": underlying, "lot_assumption": lot,
              "cost_basis": "current tariffs, actual order premium turnover",
              "holdout_start": str(boundary) if boundary else None,
              "excluded": excluded, "candidates": {},
              "limitations": ["No depth, queue, latency or intraminute stop simulation",
                              "Fixed lot assumption must match the instrument master",
                              "No automatic selection or live promotion",
                              "Repeated inspection consumes holdout independence"]}
    report['limitations'].append('Historical outcomes were visible during the preceding audit; this is not a prospective test')
    for name, rows in results.items():
        report["candidates"][name] = {
            "train": metrics([r for r in rows if r["date"] < boundary]),
            "holdout": metrics([r for r in rows if r["date"] >= boundary])}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--underlying', default='NIFTY')
    parser.add_argument('--lot', type=int, default=65)
    parser.add_argument('--output', default='data/intraday_validation.json')
    args = parser.parse_args()
    if args.lot <= 0:
        parser.error('lot must be positive')
    report = run(args.underlying, args.lot)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))

"""Performance metrics for the stock-futures paper system, by mode and direction.

Input rows are sf_api.journal() rows (or the same shape from a replay). Only
SETTLED trades enter P&L metrics; excluded / unresolved / open are counted
separately and never folded in. A metric that cannot be computed is None with
its reason in "undefined", never 0 and never a guess:

  expectancy     mean net P&L per settled trade              (needs >= 1 trade)
  win_rate       share of settled trades with net > 0         (needs >= 1 trade)
  avg_win/loss   mean net of winners / losers                 (needs a winner / a loser)
  profit_factor  sum(wins) / |sum(losses)|                    (undefined with no losing trade)
  max_drawdown   worst peak-to-trough of cumulative net, by exit time
  turnover_rs    entry notional summed (one lot each)
  exposure_min   minutes in position summed
  ci80           80% bootstrap interval of the mean net per trade (seeded, needs >= 5 trades)
"""
import random
from datetime import datetime

KEYS = ("intraday:LONG", "intraday:SHORT", "swing:LONG", "swing:SHORT")
MIN_TRADES_FOR_CI = 5


def _ts(s):
    return datetime.fromisoformat(s) if s else None


def metrics(rows, seed=7):
    settled = [r for r in rows if r.get("class") == "settled" and r.get("net_pnl") is not None]
    out = {"settled": len(settled),
           "excluded": sum(r.get("class") == "excluded" for r in rows),
           "unresolved": sum(r.get("class") == "unresolved" for r in rows),
           "open": sum(r.get("class") == "open" for r in rows), "undefined": {}}
    nets = [float(r["net_pnl"]) for r in settled]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    out["net_pnl"] = round(sum(nets), 2)
    out["gross_pnl"] = round(sum(float(r.get("gross_pnl") or 0) for r in settled), 2)
    out["costs"] = round(sum(float(r.get("costs") or 0) for r in settled), 2)

    def put(name, value, why):
        if value is None:
            out[name] = None
            out["undefined"][name] = why
        else:
            out[name] = round(value, 4) if isinstance(value, float) else value
    put("expectancy", sum(nets) / len(nets) if nets else None, "no settled trades")
    put("win_rate", len(wins) / len(nets) if nets else None, "no settled trades")
    put("avg_win", sum(wins) / len(wins) if wins else None, "no winning trade")
    put("avg_loss", sum(losses) / len(losses) if losses else None, "no losing trade")
    put("profit_factor", sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else None,
        "no losing trade (profit factor is unbounded)" if nets else "no settled trades")
    eq = peak = dd = 0.0
    for r in sorted(settled, key=lambda r: r.get("closed_at") or ""):
        eq += float(r["net_pnl"])
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out["max_drawdown"] = round(dd, 2)
    out["turnover_rs"] = round(sum(float(r.get("entry_avg") or 0) * float(r.get("closed_units") or 0)
                                   for r in settled), 0)
    exp = 0.0
    for r in settled:
        a, b = _ts(r.get("opened_at")), _ts(r.get("closed_at"))
        if a and b:
            exp += (b - a).total_seconds() / 60
    out["exposure_min"] = round(exp, 1)
    put("ci80", bootstrap_ci(nets, seed=seed) if len(nets) >= MIN_TRADES_FOR_CI else None,
        f"fewer than {MIN_TRADES_FOR_CI} settled trades")
    return out


def bootstrap_ci(xs, level=0.80, n=2000, seed=7):
    rnd = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rnd.choice(xs) for _ in range(k)) / k for _ in range(n))
    lo, hi = means[int((1 - level) / 2 * n)], means[int((1 + level) / 2 * n) - 1]
    return [round(lo, 2), round(hi, 2)]


def by_mode_direction(rows, seed=7):
    """{"intraday:LONG": {...}, ..., "intraday": {...}, "swing": {...}, "portfolio": {...}}"""
    out = {}
    for k in KEYS:
        m, d = k.split(":")
        out[k] = metrics([r for r in rows if r.get("mode") == m and r.get("direction") == d], seed)
    for m in ("intraday", "swing"):
        out[m] = metrics([r for r in rows if r.get("mode") == m], seed)
    out["portfolio"] = metrics(rows, seed)
    return out


def stressed(rows, extra_ticks_per_side=1, tick_size_default=0.05):
    """Rows with net P&L reduced as if each fill had `extra_ticks_per_side` more
    slippage (one lot, one entry and one exit fill): the cost-stress test."""
    out = []
    for r in rows:
        r2 = dict(r)
        if r.get("class") == "settled" and r.get("net_pnl") is not None:
            units = float(r.get("closed_units") or 0)
            tick = float(r.get("tick_size") or tick_size_default)
            r2["net_pnl"] = float(r["net_pnl"]) - 2 * extra_ticks_per_side * tick * units
        out.append(r2)
    return out

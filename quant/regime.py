"""Today's market view for an index: Up / Down / Sideways odds plus a
volatility state, and the catalogue strategies that fit it.

RULE-BASED, first version (Phase E). Every input is turned into a score on
-1..+1 with a plain formula, the scores are averaged with fixed weights, and
the result is turned into three odds that add to 1. These are NOT calibrated
probabilities -- nothing here was fitted to outcomes yet. Phase H replaces the
odds with a trained model; the inputs and the page stay.

Pure functions only: no I/O, so every rule is unit-tested.
"""
import math

# --------------------------------------------------------------- direction

DIR_WEIGHTS = {"trend": 0.30, "opening_range": 0.20, "breadth": 0.20,
               "flow": 0.15, "oi_flow": 0.15}
DIR_NAMES = {"trend": "Index move vs a normal day", "opening_range": "Price vs opening range",
             "breadth": "F&O breadth", "flow": "Money flow (estimate)",
             "oi_flow": "Put vs call writing today"}


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def trend_score(ret_pct, day_sigma_pct):
    """Today's move in units of half a normal day's move, squashed to -1..+1."""
    if ret_pct is None or not day_sigma_pct:
        return None
    return _clip(math.tanh(ret_pct / (0.5 * day_sigma_pct)))


def opening_range_score(last, or_high, or_low):
    """+1 above the opening range, -1 below it, in between scaled by position."""
    if None in (last, or_high, or_low) or or_high <= or_low:
        return None
    if last > or_high:
        return 1.0
    if last < or_low:
        return -1.0
    return _clip(2.0 * (last - or_low) / (or_high - or_low) - 1.0) * 0.5


def oi_flow_score(pcr_oi_change):
    """Puts written faster than calls (PCR of today's OI change > 1) reads as
    support being built under the index, so bullish; the mirror is bearish.
    log() makes 2.0 and 0.5 equal and opposite."""
    if pcr_oi_change is None or pcr_oi_change <= 0:
        return None
    return _clip(math.tanh(math.log(pcr_oi_change)))


def direction(parts):
    """Weighted mean of the parts that exist; missing ones are re-weighted out."""
    have = {k: v for k, v in parts.items() if v is not None and k in DIR_WEIGHTS}
    if not have:
        return None, have
    w = sum(DIR_WEIGHTS[k] for k in have)
    return sum(DIR_WEIGHTS[k] * v for k, v in have.items()) / w, have


# --------------------------------------------------------------- sideways

def range_used(day_high, day_low, spot, day_sigma_pct):
    """Today's high-low range as a share of a normal day's expected move
    (2 x one-day sigma). Below ~0.5 by afternoon is a quiet, ranging day."""
    if None in (day_high, day_low, spot) or not day_sigma_pct or spot <= 0:
        return None
    return (day_high - day_low) / (spot * 2 * day_sigma_pct / 100.0)


def sideways_score(dir_score, inside_walls, used):
    """-1..+1: how much the evidence says 'range' rather than 'trend'.

    Three pieces: a weak direction score, the index sitting between the call
    and put OI walls, and a day range smaller than a normal day's move."""
    bits = []
    if dir_score is not None:
        bits.append(1.0 - 2.0 * min(1.0, abs(dir_score) / 0.5))
    if inside_walls is not None:
        bits.append(0.5 if inside_walls else -0.5)
    if used is not None:
        bits.append(_clip(1.0 - 2.0 * used))
    return sum(bits) / len(bits) if bits else None


def odds(dir_score, side_score, k=2.5):
    """Three odds that add to 1, from a softmax over the two scores."""
    d = dir_score or 0.0
    s = side_score or 0.0
    e = {"up": math.exp(k * d), "down": math.exp(-k * d), "sideways": math.exp(k * s)}
    t = sum(e.values())
    return {key: round(v / t, 3) for key, v in e.items()}


def call(o, clear=0.45):
    """The headline view, or 'unclear' when no side has a clear lead."""
    best = max(o, key=o.get)
    return best if o[best] >= clear else "unclear"


# --------------------------------------------------------------- volatility

def vol_state(vix_pct, rich_ratio=None):
    """Low / Normal / High from the VIX one-year percentile (0..1), plus whether
    option premium is rich or cheap against the site's fair straddle."""
    if vix_pct is None:
        state = None
    elif vix_pct < 0.33:
        state = "low"
    elif vix_pct > 0.67:
        state = "high"
    else:
        state = "normal"
    prem = None
    if rich_ratio is not None:
        prem = "rich" if rich_ratio > 1.05 else "cheap" if rich_ratio < 0.95 else "fair"
    return {"state": state, "premium": prem}


# --------------------------------------------------------------- strategies

# Which catalogue entries suit each view, best fit first. Defined-risk
# structures lead every list; unlimited-loss ones are never picked here.
PLAYBOOK = {
    ("up", "low"):         ["bull_call_spread", "call_ratio_back_spread", "bull_condor", "bull_butterfly"],
    ("up", "normal"):      ["bull_call_spread", "bull_put_spread", "bull_condor"],
    ("up", "high"):        ["bull_put_spread", "bull_call_spread", "bull_condor"],
    ("down", "low"):       ["bear_put_spread", "put_ratio_back_spread", "bear_condor", "bear_butterfly"],
    ("down", "normal"):    ["bear_put_spread", "bear_call_spread", "bear_condor"],
    ("down", "high"):      ["bear_call_spread", "bear_put_spread", "bear_condor"],
    ("sideways", "low"):   ["long_calendar_calls", "long_calendar_puts", "iron_condor", "iron_butterfly"],
    ("sideways", "normal"):["iron_condor", "iron_butterfly", "long_calendar_calls"],
    ("sideways", "high"):  ["iron_butterfly", "iron_condor", "long_calendar_puts"],
}
WHY = {
    "bull_call_spread": "Rising view; capped cost, profits from a move up.",
    "bull_put_spread": "Rising view with rich premium; collects a credit, loss capped.",
    "call_ratio_back_spread": "Cheap options and a rising view; pays most on a sharp breakout.",
    "bull_condor": "Mild rise expected; wide profit zone above spot, cheap.",
    "bull_butterfly": "Mild rise to a target; very cheap, narrow sweet spot.",
    "bear_put_spread": "Falling view; capped cost, profits from a move down.",
    "bear_call_spread": "Falling view with rich premium; collects a credit, loss capped.",
    "put_ratio_back_spread": "Cheap options and a falling view; pays most on a sharp break.",
    "bear_condor": "Mild fall expected; wide profit zone below spot, cheap.",
    "bear_butterfly": "Mild fall to a target; very cheap, narrow sweet spot.",
    "iron_condor": "Range expected; collects premium on both sides, loss capped by the wings.",
    "iron_butterfly": "Tight range expected; most premium of the capped structures.",
    "long_calendar_calls": "Quiet index and cheap volatility; earns from the near option decaying faster.",
    "long_calendar_puts": "Quiet index; earns from the near option decaying faster.",
}


def size_lots(max_loss_rs, margin_rs, capital=2_000_000, risk_pct=0.01, margin_cap=0.5):
    """Lots allowed by the risk rule (max loss per lot) and by margin, smaller wins."""
    if not max_loss_rs or max_loss_rs <= 0:
        return {"lots": 0, "by_risk": 0, "by_margin": None, "reason": "loss not capped"}
    by_risk = int(capital * risk_pct // abs(max_loss_rs))
    by_margin = int(capital * margin_cap // margin_rs) if margin_rs and margin_rs > 0 else None
    lots = by_risk if by_margin is None else min(by_risk, by_margin)
    reason = None if lots >= 1 else "one lot risks more than the per-trade limit"
    return {"lots": lots, "by_risk": by_risk, "by_margin": by_margin, "reason": reason}


def pick(view, vol, catalogue, n=3, capital=2_000_000, risk_pct=0.01):
    """The n best-fitting, available, loss-capped catalogue rows for this view."""
    if view not in ("up", "down", "sideways") or not catalogue:
        return []
    keys = PLAYBOOK.get((view, vol or "normal"), [])
    rows = {r["key"]: r for r in catalogue}
    out = []
    for k in keys:
        r = rows.get(k)
        if not r or not r.get("available", True) or r.get("loss_unbounded"):
            continue
        ml = r.get("max_loss_rs")
        z = size_lots(abs(ml) if ml is not None else None, r.get("margin_est"), capital, risk_pct)
        out.append({"key": k, "label": r.get("label", k), "group": r.get("group"),
                    "why": WHY.get(k, ""), "pop": r.get("pop"), "net_ev_rs": r.get("net_ev_rs"),
                    "max_profit_rs": r.get("max_profit_rs"), "max_loss_rs": ml,
                    "net_premium_rs": r.get("net_premium_rs"), "breakevens": r.get("breakevens"),
                    "margin_est": r.get("margin_est"), "legs": r.get("legs"), "sizing": z,
                    "costs_rs": (r.get("fees_rs") or 0) + (r.get("slippage_rs") or 0)})
        if len(out) == n:
            break
    return out


def view(parts, inside_walls=None, used=None, vix_pct=None, rich_ratio=None,
         catalogue=None, capital=2_000_000, risk_pct=0.01):
    """Everything the Options page shows for one index."""
    d, have = direction(parts)
    s = sideways_score(d, inside_walls, used)
    if d is None and s is None:
        return {"available": False, "reason": "not enough live data for a view yet"}
    o = odds(d, s)
    c = call(o)
    v = vol_state(vix_pct, rich_ratio)
    return {"available": True, "odds": o, "call": c,
            "direction_score": None if d is None else round(d, 3),
            "sideways_score": None if s is None else round(s, 3),
            "inputs": [{"key": k, "name": DIR_NAMES[k], "value": round(have[k], 2),
                        "weight": DIR_WEIGHTS[k]} for k in DIR_WEIGHTS if k in have],
            "missing": [DIR_NAMES[k] for k in DIR_WEIGHTS if k not in have],
            "inside_walls": inside_walls, "range_used": None if used is None else round(used, 2),
            "vol": v,
            "strategies": pick(c, v["state"], catalogue, capital=capital, risk_pct=risk_pct)
                          if c != "unclear" else [],
            # No clear view: the side with the highest odds anyway, for the paper
            # book, which trades every day. Shown on the page as a weak lead.
            "lean": max(o, key=o.get),
            "lean_strategies": pick(max(o, key=o.get), v["state"], catalogue, capital=capital,
                                    risk_pct=risk_pct) if c == "unclear" else []}

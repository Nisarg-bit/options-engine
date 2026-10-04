"""The strategy catalogue: Bullish / Bearish / Neutral, as the builder offers it.

One definition, two consumers:

  * the page's Strategy builder, which gets these definitions baked in by
    firebase/v2-src/build.py (so the tiles and the scanner can never disagree
    about what "bull put spread" means), and
  * api.api_structures, which scores every entry on the live chain and ships
    the result as `catalogue` beside the engine's seven structures.

Strikes are ATM-relative, in units of a WIDTH `w` (default two strike steps:
100 points on NIFTY). ATM is the chain's ATM row -- the strike nearest the
parity forward -- the same one the chain table highlights.

SCORING uses the site's own model, not a new one: Bachelier (terminal price
Normal(spot, sigma)), the workbook's model that every POP and EV on the site
already uses. A leg on the NEXT expiry (the calendars) is worth, in
expectation at the near expiry, exactly its Bachelier price today at its own
expiry's sigma -- the price process is a martingale -- so EV has a closed form
for every entry, calendars included.

IMPORTANT: nothing here is RECOMMENDED. The entry rule (credit/sigma against
0.9346) was fitted on selling premium in the engine's seven structures; it
says nothing about a debit spread or a back spread. These rows are scored so
they can be compared, and the page says they are never the model's pick.
"""

import math

import pricing as P

GROUPS = [("bullish", "Bullish"), ("bearish", "Bearish"), ("neutral", "Neutral")]

# (kind, strike offset in widths, signed lots, expiry index 0=near 1=next)
STRATEGIES = [
    # ------------------------------------------------------------- bullish
    {"key": "buy_call", "label": "Buy Call", "group": "bullish", "type": "debit",
     "legs": [("CE", 0, 1, 0)]},
    {"key": "sell_put", "label": "Sell Put", "group": "bullish", "type": "credit",
     "legs": [("PE", 0, -1, 0)]},
    {"key": "bull_call_spread", "label": "Bull Call Spread", "group": "bullish",
     "type": "debit", "family": "debit spread",
     "legs": [("CE", 0, 1, 0), ("CE", 1, -1, 0)]},
    {"key": "bull_put_spread", "label": "Bull Put Spread", "group": "bullish",
     "type": "credit", "family": "credit spread",
     "legs": [("PE", 0, -1, 0), ("PE", -1, 1, 0)]},
    {"key": "call_ratio_back_spread", "label": "Call Ratio Back Spread",
     "group": "bullish", "type": "ratio", "family": "ratio spread",
     "legs": [("CE", 0, -1, 0), ("CE", 1, 2, 0)]},
    {"key": "long_synthetic", "label": "Long Synthetic", "group": "bullish",
     "type": "synthetic", "legs": [("CE", 0, 1, 0), ("PE", 0, -1, 0)]},
    {"key": "bull_butterfly", "label": "Bullish Butterfly", "group": "bullish",
     "type": "debit",
     "legs": [("CE", 0, 1, 0), ("CE", 1, -2, 0), ("CE", 2, 1, 0)]},
    {"key": "bull_condor", "label": "Bullish Condor", "group": "bullish",
     "type": "debit",
     "legs": [("CE", 0, 1, 0), ("CE", 1, -1, 0), ("CE", 2, -1, 0), ("CE", 3, 1, 0)]},
    # ------------------------------------------------------------- bearish
    {"key": "buy_put", "label": "Buy Put", "group": "bearish", "type": "debit",
     "legs": [("PE", 0, 1, 0)]},
    {"key": "sell_call", "label": "Sell Call", "group": "bearish", "type": "credit",
     "legs": [("CE", 0, -1, 0)]},
    {"key": "bear_put_spread", "label": "Bear Put Spread", "group": "bearish",
     "type": "debit", "family": "debit spread",
     "legs": [("PE", 0, 1, 0), ("PE", -1, -1, 0)]},
    {"key": "bear_call_spread", "label": "Bear Call Spread", "group": "bearish",
     "type": "credit", "family": "credit spread",
     "legs": [("CE", 0, -1, 0), ("CE", 1, 1, 0)]},
    {"key": "put_ratio_back_spread", "label": "Put Ratio Back Spread",
     "group": "bearish", "type": "ratio", "family": "ratio spread",
     "legs": [("PE", 0, -1, 0), ("PE", -1, 2, 0)]},
    {"key": "short_synthetic", "label": "Short Synthetic", "group": "bearish",
     "type": "synthetic", "legs": [("CE", 0, -1, 0), ("PE", 0, 1, 0)]},
    {"key": "bear_butterfly", "label": "Bearish Butterfly", "group": "bearish",
     "type": "debit",
     "legs": [("PE", 0, 1, 0), ("PE", -1, -2, 0), ("PE", -2, 1, 0)]},
    {"key": "bear_condor", "label": "Bearish Condor", "group": "bearish",
     "type": "debit",
     "legs": [("PE", 0, 1, 0), ("PE", -1, -1, 0), ("PE", -2, -1, 0), ("PE", -3, 1, 0)]},
    # ------------------------------------------------------------- neutral
    {"key": "short_straddle", "label": "Short Straddle", "group": "neutral",
     "type": "credit", "legs": [("CE", 0, -1, 0), ("PE", 0, -1, 0)]},
    {"key": "short_strangle", "label": "Short Strangle", "group": "neutral",
     "type": "credit", "legs": [("CE", 1, -1, 0), ("PE", -1, -1, 0)]},
    {"key": "iron_butterfly", "label": "Iron Butterfly", "group": "neutral",
     "type": "credit",
     "legs": [("CE", 0, -1, 0), ("PE", 0, -1, 0), ("CE", 2, 1, 0), ("PE", -2, 1, 0)]},
    {"key": "iron_condor", "label": "Iron Condor", "group": "neutral",
     "type": "credit",
     "legs": [("CE", 1, -1, 0), ("PE", -1, -1, 0), ("CE", 2, 1, 0), ("PE", -2, 1, 0)]},
    {"key": "call_ratio_spread", "label": "Call Ratio Spread 1:2",
     "group": "neutral", "type": "ratio", "family": "ratio spread",
     "legs": [("CE", 0, 1, 0), ("CE", 1, -2, 0)]},
    {"key": "put_ratio_spread", "label": "Put Ratio Spread 1:2",
     "group": "neutral", "type": "ratio", "family": "ratio spread",
     "legs": [("PE", 0, 1, 0), ("PE", -1, -2, 0)]},
    {"key": "jade_lizard", "label": "Jade Lizard", "group": "neutral",
     "type": "credit",
     "legs": [("PE", -1, -1, 0), ("CE", 1, -1, 0), ("CE", 2, 1, 0)]},
    {"key": "long_calendar_calls", "label": "Long Calendar (Calls)",
     "group": "neutral", "type": "calendar", "family": "calendar spread",
     "legs": [("CE", 0, -1, 0), ("CE", 0, 1, 1)]},
    {"key": "long_calendar_puts", "label": "Long Calendar (Puts)",
     "group": "neutral", "type": "calendar", "family": "calendar spread",
     "legs": [("PE", 0, -1, 0), ("PE", 0, 1, 1)]},
]

BY_KEY = {s["key"]: s for s in STRATEGIES}

# Plain-English one-liners for the tiles and the table.
BLURB = {
    "buy_call": "Pay premium; gains if the index rises past strike + premium.",
    "sell_put": "Collect premium; keeps it above the strike, unlimited loss below.",
    "bull_call_spread": "Debit spread: buy ATM call, sell a higher one. Capped both ways.",
    "bull_put_spread": "Credit spread: sell ATM put, buy a lower one. Capped both ways.",
    "call_ratio_back_spread": "Sell 1 call, buy 2 higher. Big upside if it breaks out.",
    "long_synthetic": "Call bought, put sold: behaves like long futures.",
    "bull_butterfly": "Cheap bet the index finishes near the middle strike, above spot.",
    "bull_condor": "Like the butterfly with a wider sweet spot above spot.",
    "buy_put": "Pay premium; gains if the index falls past strike - premium.",
    "sell_call": "Collect premium; keeps it below the strike, unlimited loss above.",
    "bear_put_spread": "Debit spread: buy ATM put, sell a lower one. Capped both ways.",
    "bear_call_spread": "Credit spread: sell ATM call, buy a higher one. Capped both ways.",
    "put_ratio_back_spread": "Sell 1 put, buy 2 lower. Big downside payoff if it breaks.",
    "short_synthetic": "Put bought, call sold: behaves like short futures.",
    "bear_butterfly": "Cheap bet the index finishes near the middle strike, below spot.",
    "bear_condor": "Like the butterfly with a wider sweet spot below spot.",
    "short_straddle": "Sell ATM call and put. Most premium, unlimited loss both sides.",
    "short_strangle": "Sell OTM call and put. Wider zone, unlimited loss both sides.",
    "iron_butterfly": "Short straddle with wings bought. Loss capped.",
    "iron_condor": "Short strangle with wings bought. Loss capped.",
    "call_ratio_spread": "Buy 1 call, sell 2 higher. Unlimited loss if it rallies hard.",
    "put_ratio_spread": "Buy 1 put, sell 2 lower. Unlimited loss if it falls hard.",
    "jade_lizard": "Short put + short call spread. No upside risk if credit > call width.",
    "long_calendar_calls": "Sell this week's ATM call, buy next week's. Wants a quiet index.",
    "long_calendar_puts": "Sell this week's ATM put, buy next week's. Wants a quiet index.",
}


def definitions():
    """JSON-able definitions for the page (build.py bakes these in)."""
    return {"groups": [{"key": k, "label": l} for k, l in GROUPS],
            "strategies": [{**{k: v for k, v in s.items() if k != "legs"},
                            "blurb": BLURB.get(s["key"], ""),
                            "legs": [{"kind": k, "off": o, "qty": q, "exp": e}
                                     for k, o, q, e in s["legs"]]}
                           for s in STRATEGIES]}


def default_width(step):
    """Two strike steps: 100 on NIFTY, 200 on BANKNIFTY/SENSEX."""
    return 2 * step


def build_legs(key, atm, width, expiries):
    """Concrete legs [{strike, kind, qty, expiry}] for one lot."""
    s = BY_KEY[key]
    return [{"strike": float(atm + off * width), "kind": kind, "qty": qty,
             "expiry": expiries[e] if e < len(expiries) else None}
            for kind, off, qty, e in s["legs"]]


# ----------------------------------------------------------------- scoring

def _bach(spot, strike, kind, sigma):
    if sigma <= 0:
        return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
    return (P.expected_call(spot, strike, sigma) if kind == "CE"
            else P.expected_put(spot, strike, sigma))


def cost_estimate(legs, lot, costs=P.DEFAULT_COSTS):
    """Rupees. Brokerage on an open and a close order per leg, statutory
    charges on the entry premium, slippage per unit traded both ways.

    A generalisation of pricing.transaction_costs to any leg count, labelled
    on the page as an estimate -- like every other cost figure on the site."""
    units = sum(abs(l["qty"]) for l in legs) * lot
    turnover = sum(abs(l["qty"]) * l["premium"] for l in legs) * lot
    rate = (costs.stt_on_sell + costs.exchange_per_side * 2
            + costs.sebi_per_side * 2 + costs.stamp_buy)
    brokerage = costs.brokerage_per_order * 2 * len(legs)
    gst = (brokerage + turnover * (costs.exchange_per_side * 2
                                   + costs.sebi_per_side * 2)) * costs.gst
    slip = costs.slippage_per_leg * 2 * units
    return turnover * rate + brokerage + gst, slip


def margin_estimate(legs, max_loss_rs, bounded, margin_per_lot, debit_rs):
    """Rough, labelled. Bounded risk: the maximum loss (or the debit, for a
    pure buyer). Otherwise the site's per-lot short margin for every short lot
    that no long option on the same side covers."""
    if bounded:
        return max(abs(max_loss_rs or 0.0), debit_rs, 0.0)
    naked = 0
    for kind in ("CE", "PE"):
        short = sum(-l["qty"] for l in legs if l["kind"] == kind and l["qty"] < 0)
        long_ = sum(l["qty"] for l in legs if l["kind"] == kind and l["qty"] > 0)
        naked += max(0, short - long_)
    return margin_per_lot * max(1, naked)


def payoff_near(legs, level, sig_rem, shift=None):
    """P&L in points at the NEAR expiry for one lot, index at `level`.
    Near legs pay intrinsic; later legs are worth their Bachelier price with
    the sigma left until their own expiry (sig_rem[expiry]), on their own
    forward (level + shift[expiry])."""
    shift = shift or {}
    pnl = 0.0
    for l in legs:
        s = sig_rem.get(l["expiry"], 0.0)
        x = level + shift.get(l["expiry"], 0.0)
        pnl += l["qty"] * (_bach(x, l["strike"], l["kind"], s) - l["premium"])
    return pnl


def score(legs, spot, sigma_near, sigma_by_exp, near, lot, margin_per_lot,
          costs=P.DEFAULT_COSTS, span_sd=6.0, n=1201, fwd_by_exp=None,
          pop_fn=None):
    """Every number a table row needs, for legs that already carry premiums.

    spot:         the CENTRE of the terminal distribution. Pass the near
                  expiry's parity forward, not the index: the premiums are
                  priced around the forward, and centring on a spot 70 points
                  below it made every put look rich and every call cheap --
                  harmless for a symmetric strangle, badly wrong for a
                  directional spread.
    sigma_by_exp: {expiry: total Bachelier sigma (points) to that expiry}.
    near:         the near expiry (the one the payoff is measured at).
    fwd_by_exp:   {expiry: forward}; a later expiry's legs are valued on its
                  own forward (offset from the near one).
    pop_fn:       optional (xs, ys) -> probability, the calibrated POP
                  (calpop.pop_curve); returned as `pop_cal`.
    """
    fwd_by_exp = fwd_by_exp or {}
    base = fwd_by_exp.get(near, spot)
    shift = {e: f - base for e, f in fwd_by_exp.items()}
    sig_rem = {}
    for e, s in sigma_by_exp.items():
        if e == near:
            sig_rem[e] = 0.0
        else:
            sig_rem[e] = math.sqrt(max(s * s - sigma_near * sigma_near, 0.0))

    net_pts = sum(-l["qty"] * l["premium"] for l in legs)      # + = credit
    exp_val = sum(l["qty"] * _bach(spot + shift.get(l["expiry"], 0.0),
                                   l["strike"], l["kind"],
                                   sigma_by_exp.get(l["expiry"], sigma_near))
                  for l in legs)                                # model value
    gross_pts = exp_val + net_pts                               # E[P&L] pts

    lo = spot - span_sd * sigma_near
    hi = spot + span_sd * sigma_near
    xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    xs = sorted(set(xs + [l["strike"] for l in legs if lo <= l["strike"] <= hi]))
    ys = [payoff_near(legs, x, sig_rem, shift) for x in xs]

    # POP: probability mass where the near-expiry P&L is positive.
    pop = 0.0
    for i in range(len(xs) - 1):
        a, b = xs[i], xs[i + 1]
        if (ys[i] + ys[i + 1]) / 2.0 > 0:
            pop += (P.norm_cdf((b - spot) / sigma_near)
                    - P.norm_cdf((a - spot) / sigma_near))

    bes = []
    for i in range(len(xs) - 1):
        a, b = ys[i], ys[i + 1]
        if a == 0:
            bes.append(round(xs[i], 1))
        elif a * b < 0:
            t = -a / (b - a)
            bes.append(round(xs[i] + t * (xs[i + 1] - xs[i]), 1))

    eps = 1e-6
    # Unbounded means the P&L is still FALLING at an edge of a 6-sigma window.
    loss_unb = ys[0] < ys[1] - eps or ys[-1] < ys[-2] - eps
    prof_unb = ys[0] > ys[1] + eps or ys[-1] > ys[-2] + eps
    max_p, max_l = max(ys), min(ys)

    fees, slip = cost_estimate(legs, lot, costs)
    gross_rs = gross_pts * lot
    net_rs = gross_rs - fees - slip
    debit_rs = max(0.0, -net_pts) * lot
    margin = margin_estimate(legs, max_l * lot, not loss_unb, margin_per_lot,
                             debit_rs)
    return {
        "net_premium_pts": round(net_pts, 2),
        "net_premium_rs": round(net_pts * lot, 0),
        "max_profit_rs": None if prof_unb else round(max_p * lot, 0),
        "max_loss_rs": None if loss_unb else round(max_l * lot, 0),
        "profit_unbounded": prof_unb, "loss_unbounded": loss_unb,
        "breakevens": bes,
        "pop": round(pop, 4),
        "pop_cal": round(pop_fn(xs, ys), 4) if pop_fn else None,
        "exp_value_pts": round(exp_val, 2),
        "gross_ev_rs": round(gross_rs, 0),
        "fees_rs": round(fees, 0), "slippage_rs": round(slip, 0),
        "net_ev_rs": round(net_rs, 0),
        "margin_est": round(margin, 0),
        "roi": round(net_rs / margin, 5) if margin else None,
    }


def stop_eligible(key):
    """P(stop) is defined against a CREDIT with a stop at a multiple of it,
    simulated one unit per leg -- so only single-expiry credit entries whose
    legs are all one lot qualify. Ratios, debits and calendars do not."""
    s = BY_KEY[key]
    return (s["type"] == "credit"
            and all(abs(q) == 1 and e == 0 for _, _, q, e in s["legs"]))


def score_all(chains, expiries, spot, atm, step, lot, sigma_by_exp,
              margin_per_lot, costs=P.DEFAULT_COSTS, width=None, fwd_by_exp=None,
              pop_fn=None):
    """Score every catalogue entry.

    chains:  {expiry: {(strike, kind): premium}} -- mid prices, two-sided only.
    expiries: [near, next] (next may be absent).
    Returns a list of rows in catalogue order; an entry whose strikes are not
    all quoted comes back `available: False` with the reason, never priced at
    a guess.
    """
    width = width or default_width(step)
    near = expiries[0]
    sigma_near = sigma_by_exp[near]
    out = []
    for s in STRATEGIES:
        legs = build_legs(s["key"], atm, width, expiries)
        row = {"key": s["key"], "label": s["label"], "group": s["group"],
               "type": s["type"], "family": s.get("family"),
               "width": width, "recommended": False}
        missing = None
        for l in legs:
            if l["expiry"] is None:
                missing = "next expiry not published"
                break
            p = chains.get(l["expiry"], {}).get((l["strike"], l["kind"]))
            if p is None:
                missing = (f"no two-sided quote at {l['strike']:,.0f} {l['kind']}"
                           f" ({l['expiry']})")
                break
            l["premium"] = float(p)
        if missing:
            row.update({"available": False, "why": missing,
                        "legs": [{k: v for k, v in l.items()} for l in legs]})
            out.append(row)
            continue
        row["available"] = True
        row["legs"] = [{"strike": l["strike"], "kind": l["kind"],
                        "qty": l["qty"], "expiry": str(l["expiry"]),
                        "premium": round(l["premium"], 2)} for l in legs]
        row.update(score(legs, spot, sigma_near, sigma_by_exp, near, lot,
                         margin_per_lot, costs, fwd_by_exp=fwd_by_exp,
                         pop_fn=pop_fn))
        out.append(row)
    return out

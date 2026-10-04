"""Option_Chain rows 28-58, and the OI_Analytics sheet, ported cell for cell.

Kept separate from pricing.py and models.py on purpose. Those two are the
Phase 2 modules: pricing.py is the workbook's Bachelier world at zero drift,
models.py is the lognormal alternative built to test it. This file is neither
-- it is the workbook's OWN per-strike block, which mixes the two: Black-Scholes
Greeks at a 6.5% risk-free rate, sitting beside probabilities computed from a
NORMAL terminal distribution with an absolute sigma.

That mixture is the workbook's, not mine, and it is reproduced rather than
tidied. Where it produces something arguably wrong -- the EV column especially
-- the wrong number is computed under its own name and a corrected one is
offered alongside, so the difference is visible instead of argued about.

Cell references are given for every function. Option_Chain!B15 is the rate,
B14 is T in years, B19 is the expiry sigma in points, B7 the lot size.
"""

from math import log, sqrt, exp, pi, erf

RISK_FREE = 0.065          # Option_Chain!B15
TRADING_DAYS_BASIS = 365.0


def _pdf(z):
    return exp(-0.5 * z * z) / sqrt(2.0 * pi)


def _cdf(z):
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def norm_cdf_at(x, mean, sd):
    """Excel NORMDIST(x, mean, sd, TRUE) -- the workbook's probability engine."""
    if sd <= 0:
        return 1.0 if x >= mean else 0.0
    return _cdf((x - mean) / sd)


# ------------------------------------------------------------------ helpers

def d1_d2(spot, strike, sigma, T, r=RISK_FREE):
    """Option_Chain!AJ28 and AK28.

    `sigma` is the strike's own implied vol where the chain has one, and India
    VIX where it does not -- that is AH28's `IF(E28>0, E28, $B$9)`. A strike
    with no IV is priced off the index's, which is the right fallback and worth
    knowing about when a far strike's Greeks look suspiciously like the ATM's.
    """
    if sigma <= 0 or T <= 0 or spot <= 0 or strike <= 0:
        return 0.0, 0.0
    v = sigma * sqrt(T)
    d1 = (log(spot / strike) + (r + sigma * sigma / 2.0) * T) / v
    return d1, d1 - v


def call_greeks(spot, strike, sigma, T, r=RISK_FREE):
    """Option_Chain G, H, I, J at row 28 -- delta, gamma, theta/day, vega."""
    d1, d2 = d1_d2(spot, strike, sigma, T, r)
    if sigma <= 0 or T <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    return {
        "delta": _cdf(d1),                                              # G
        "gamma": _pdf(d1) / (spot * sigma * sqrt(T)),                   # H
        # I: divided by 365 to express the annual theta as a per-day number.
        "theta": (-spot * _pdf(d1) * sigma / (2 * sqrt(T))
                  - r * strike * exp(-r * T) * _cdf(d2)) / 365.0,
        # J: divided by 100, so it reads as rupees per 1 volatility POINT.
        "vega": spot * _pdf(d1) * sqrt(T) / 100.0,
    }


def put_greeks(spot, strike, sigma, T, r=RISK_FREE):
    """Option_Chain Y, X, W, V at row 28."""
    d1, d2 = d1_d2(spot, strike, sigma, T, r)
    if sigma <= 0 or T <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    return {
        "delta": _cdf(d1) - 1.0,                                        # Y
        "gamma": _pdf(d1) / (spot * sigma * sqrt(T)),                   # X
        "theta": (-spot * _pdf(d1) * sigma / (2 * sqrt(T))
                  + r * strike * exp(-r * T) * _cdf(-d2)) / 365.0,      # W
        "vega": spot * _pdf(d1) * sqrt(T) / 100.0,                      # V
    }


# --------------------------------------------------------- per-strike block

EV_DEFECT_NOTE = """\
Option_Chain!O28 is `PMP * MaxProfit` -- the probability the option expires
worthless, times the whole premium. There is no loss term anywhere in it. That
is not expected value; it is expected value conditional on winning, which is
always positive and always rises as you move toward the money. Column Q does
the same for puts.

Reproduced here as `ev`. `ev_fixed` beside it is the honest version:

    (premium - E[payoff]) * lot

with E[payoff] under the same normal terminal distribution the sheet already
uses for POP. On any real chain `ev` is largest at the strike nearest the
money and `ev_fixed` is not, which is the whole point of showing both."""


def expected_call_payoff(spot, strike, sigma_pts):
    """E[max(0, S_T - K)] under Normal(spot, sigma_pts) -- pricing.py's B36."""
    if sigma_pts <= 0:
        return max(0.0, spot - strike)
    d = (spot - strike) / sigma_pts
    return sigma_pts * _pdf(d) + (spot - strike) * _cdf(d)


def expected_put_payoff(spot, strike, sigma_pts):
    if sigma_pts <= 0:
        return max(0.0, strike - spot)
    d = (strike - spot) / sigma_pts
    return sigma_pts * _pdf(d) + (strike - spot) * _cdf(d)


def strike_block(spot, strike, ce_px, pe_px, ce_iv, pe_iv, vix,
                 sigma_pts, T, lot, r=RISK_FREE):
    """Every per-strike column the workbook computes, for one strike.

    `ce_iv`/`pe_iv` are decimal fractions (0.11), or None where the solver
    could not find one; `vix` is the same units and is the fallback, exactly as
    AH28/AI28 specify.
    """
    cs = ce_iv if (ce_iv and ce_iv > 0) else vix
    ps = pe_iv if (pe_iv and pe_iv > 0) else vix

    cg = call_greeks(spot, strike, cs, T, r)
    pg = put_greeks(spot, strike, ps, T, r)

    ce_be = strike + ce_px                       # K28
    pe_be = strike - pe_px                       # U28
    ce_pmp = norm_cdf_at(strike, spot, sigma_pts)          # L28
    ce_pop = norm_cdf_at(ce_be, spot, sigma_pts)           # M28
    pe_pmp = 1.0 - norm_cdf_at(strike, spot, sigma_pts)    # T28
    pe_pop = 1.0 - norm_cdf_at(pe_be, spot, sigma_pts)     # S28
    ce_max = ce_px * lot                                    # N28
    pe_max = pe_px * lot                                    # R28

    return {
        "ce": {**cg, "iv_used": round(cs * 100, 2), "be": ce_be,
               "pmp": ce_pmp, "pop": ce_pop, "max_profit": ce_max,
               "ev": ce_pmp * ce_max,                                   # O28
               "ev_fixed": (ce_px - expected_call_payoff(spot, strike, sigma_pts)) * lot},
        "pe": {**pg, "iv_used": round(ps * 100, 2), "be": pe_be,
               "pmp": pe_pmp, "pop": pe_pop, "max_profit": pe_max,
               "ev": pe_pmp * pe_max,                                   # Q28
               "ev_fixed": (pe_px - expected_put_payoff(spot, strike, sigma_pts)) * lot},
    }


# --------------------------------------------------------------- OI analytics

def pcr(rows):
    """OI_Analytics B6, B7, B8 -- put/call ratios on OI, volume and OI change.

    Above 1 means more puts than calls, which is conventionally read as
    bullish positioning. The OI-CHANGE ratio is the interesting one: it is
    today's flow rather than the accumulated book, so it moves first.
    """
    def s(side, key):
        return sum(float(r[side].get(key) or 0) for r in rows)

    ce_oi, pe_oi = s("CE", "oi"), s("PE", "oi")
    ce_v, pe_v = s("CE", "volume"), s("PE", "volume")
    # oi_chg is a fraction on each row; the sheet uses absolute change, so
    # reconstruct it from the base rather than averaging ratios.
    def chg(side):
        t = 0.0
        for r in rows:
            c = r[side]
            if c.get("oi_chg") is not None and c.get("oi"):
                base = c["oi"] / (1.0 + c["oi_chg"]) if c["oi_chg"] != -1 else 0.0
                t += c["oi"] - base
        return t
    ce_dc, pe_dc = chg("CE"), chg("PE")

    return {
        "ce_oi": ce_oi, "pe_oi": pe_oi,
        "pcr_oi": (pe_oi / ce_oi) if ce_oi else None,
        "pcr_volume": (pe_v / ce_v) if ce_v else None,
        "pcr_oi_change": (pe_dc / ce_dc) if ce_dc else None,
        "ce_oi_added": ce_dc, "pe_oi_added": pe_dc,
    }


def levels(rows):
    """OI_Analytics B9:B12 -- where the open interest actually sits.

    Resistance is the strike carrying the most CALL open interest: the level
    the largest number of writers have bet the index will not exceed. Support
    is the mirror on puts. They are not predictions, they are where the pain
    is concentrated, which is why they so often behave like walls.
    """
    def top(side, key):
        best, val = None, None
        for r in rows:
            v = r[side].get(key)
            if v is None:
                continue
            if val is None or v > val:
                best, val = r["strike"], v
        return best, val

    def top_added(side):
        """Strike with the most CONTRACTS added today. oi_chg is a fraction,
        so ranking on it crowned thin far strikes (+40% of a small base) over
        the near-money strikes where the real positions went on."""
        best, val = None, None
        for r in rows:
            c = r[side]
            f, oi = c.get("oi_chg"), c.get("oi")
            if f is None or not oi or f == -1:
                continue
            added = oi - oi / (1.0 + f)
            if val is None or added > val:
                best, val = r["strike"], added
        return best, val

    res, res_oi = top("CE", "oi")
    sup, sup_oi = top("PE", "oi")
    ce_add, ce_add_n = top_added("CE")
    pe_add, pe_add_n = top_added("PE")
    return {"resistance": res, "resistance_oi": res_oi,
            "support": sup, "support_oi": sup_oi,
            "max_ce_addition": ce_add, "max_pe_addition": pe_add,
            "max_ce_added": round(ce_add_n) if ce_add_n is not None else None,
            "max_pe_added": round(pe_add_n) if pe_add_n is not None else None,
            "oi_range_width": (res - sup) if (res and sup) else None}


def max_pain(rows):
    """OI_Analytics A19:D50 -- the strike where option writers lose least.

    At each candidate settlement price, add up what every open call and put
    would cost the people who wrote them. The minimum of that total is 'max
    pain'. It is a crowd-position statistic, not a forecast, and it moves as
    open interest moves -- but expiries do have a habit of gravitating toward
    it, which is why the sheet tracks the distance.
    """
    out = []
    for cand in rows:
        k = cand["strike"]
        ce_loss = sum(float(r["CE"].get("oi") or 0) * max(0.0, k - r["strike"])
                      for r in rows)
        pe_loss = sum(float(r["PE"].get("oi") or 0) * max(0.0, r["strike"] - k)
                      for r in rows)
        out.append({"strike": k, "ce_loss": ce_loss, "pe_loss": pe_loss,
                    "total": ce_loss + pe_loss})
    if not out:
        return None, []
    best = min(out, key=lambda x: x["total"])
    return best["strike"], out

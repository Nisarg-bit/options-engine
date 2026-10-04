"""Phase 2.4 -- the improved model, kept beside the legacy one rather than
replacing it.

pricing.py reproduces the workbook exactly and its tests must stay green, so
nothing here touches it. This module provides the alternatives:

  1. LOGNORMAL terminal distribution instead of the workbook's normal one.
     The workbook uses NORMDIST with a sigma in absolute points -- a Bachelier
     model. It lets NIFTY go negative, is symmetric in points rather than in
     returns, and misprices exactly where the tail lives.

  2. PER-STRIKE IMPLIED VOL backed out of the chain, instead of one flat VIX
     for every strike. NIFTY has a pronounced put skew: the 5% OTM put trades
     at a much higher vol than the 5% OTM call. A single sigma prices the put
     side wrong, and every strike rule you have (+/- 1 sigma, +/- 1.5 sigma)
     and every POP depends on that sigma.

  3. BARRIER-CORRECT P(touch) instead of min(1, 2*(1-POP)). The workbook's
     version is a reflection-principle sketch applied to the wrong level.

Conventions:
  sigma  is ANNUALISED and RELATIVE (0.12 = 12% a year), not points.
  T      is in years.
  Drift  is zero, so E[S_T] = S. Over a four-day horizon the 6.5% carry moves
         things by ~0.07%, far below the noise, and zero drift keeps this
         directly comparable with the workbook's zero-drift normal model.
"""

from math import log, sqrt, exp, erf, pi, isfinite

# --------------------------------------------------------------- normal dist

def norm_pdf(z):
    return exp(-0.5 * z * z) / sqrt(2.0 * pi)


def norm_cdf(z):
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


# ------------------------------------------------------------ unit conversion

def annual_vol_from_points(sigma_points, spot, T):
    """Convert the workbook's absolute sigma to an annualised relative one.

    sigma_points = spot * (vix/sqrt(365)) * sqrt(days), and T = days/365, so
    this returns the VIX itself. The identity is a useful check that the two
    models are being fed the same volatility rather than accidentally
    different ones.
    """
    if spot <= 0 or T <= 0:
        return 0.0
    return sigma_points / (spot * sqrt(T))


def points_from_annual_vol(sigma_annual, spot, T):
    """The inverse -- an absolute sigma pricing.py can consume."""
    return sigma_annual * spot * sqrt(T)


# ------------------------------------------------------- Black-Scholes, r = 0

def _d1_d2(S, K, sigma, T):
    v = sigma * sqrt(T)
    if v <= 0 or S <= 0 or K <= 0:
        return None, None
    d1 = (log(S / K) + 0.5 * sigma * sigma * T) / v
    return d1, d1 - v


def bs_call(S, K, sigma, T):
    """Undiscounted E[max(0, S_T - K)] under lognormal, zero drift."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1, d2 = _d1_d2(S, K, sigma, T)
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def bs_put(S, K, sigma, T):
    """Undiscounted E[max(0, K - S_T)] under lognormal, zero drift."""
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1, d2 = _d1_d2(S, K, sigma, T)
    return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_price(S, K, sigma, T, kind):
    return bs_call(S, K, sigma, T) if kind == "CE" else bs_put(S, K, sigma, T)


# --------------------------------------------------------- implied volatility

IV_LO, IV_HI = 1e-4, 5.0


def implied_vol(price, S, K, T, kind, tol=1e-7, max_iter=100):
    """Back sigma out of a quoted price by bisection.

    Returns None rather than a wrong number when the price is unattainable --
    below intrinsic, or above the no-arbitrage bound. Untraded far-OTM strikes
    in bhavcopy print prices that violate both, and a solver that silently
    returns its bracket edge would poison the smile with 0.0001 and 5.0
    readings that look like data.

    Bisection rather than Newton: vega collapses on deep OTM strikes and
    Newton diverges there. Bisection is slower and always converges.
    """
    if price is None or not isfinite(price) or price <= 0 or T <= 0 or S <= 0:
        return None

    intrinsic = max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    if price < intrinsic - 1e-9:
        return None                      # below intrinsic: not a real quote
    if price <= intrinsic + 1e-9:
        return None                      # pure intrinsic: sigma indeterminate

    lo_p = bs_price(S, K, IV_LO, T, kind)
    hi_p = bs_price(S, K, IV_HI, T, kind)
    if not (lo_p <= price <= hi_p):
        return None                      # outside what any sigma can produce

    lo, hi = IV_LO, IV_HI
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, mid, T, kind)
        if abs(p - price) < tol:
            return mid
        if p < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def forward_from_parity(chain, spot, n=5):
    """The forward the chain is actually priced off, from put-call parity.

    Index options are priced off the FORWARD, not the spot. Put-call parity
    gives it directly: F = K + C - P at every strike, exactly, with no
    volatility assumption. Near the money both legs carry real time value so
    the estimate is stable; far out, one leg is a rounding error and the
    implied F is noise -- hence the median over the `n` nearest strikes.

    This matters more than it sounds. On the 2026-08-28 fixture the chain's
    forward sits 64 points above spot. Feeding spot to the solver instead
    produced a 6-vol-point discontinuity across the money -- the near ATM put
    read 7.3% and the call 13.2%, which is arbitrage, not skew. With the
    forward the two sides line up.
    """
    if not chain or spot <= 0:
        return spot
    near = sorted(chain, key=lambda r: abs(float(r["strike"]) - spot))[:n]
    fwds = []
    for r in near:
        c, p = float(r["ce"]), float(r["pe"])
        if c > 0 and p > 0:
            fwds.append(float(r["strike"]) + c - p)
    if not fwds:
        return spot
    fwds.sort()
    return fwds[len(fwds) // 2]


# --- Is the index quote contemporaneous with the chain?
#
# On 15 September 2026 at 15:27 and 15:28 the NIFTY 50 bar printed the
# identical value 23,172.35 while the option chain repriced by 8 points
# between those minutes. Five strikes implied an underlying of 23,127 and
# agreed with each other to within 2.3 points; the index disagreed with all
# five by 45. At 15:29 the index printed 23,118.60 -- which is what the chain
# had already said at 15:28. The index feed lags the options by about a
# minute near the close and repeats its last value while it lags.
#
# A flat tolerance cannot police this, because the honest allowance depends
# on two things that change every cycle:
#
#   CARRY  -- a real forward sits above spot by roughly the cost of carry,
#             which grows with time to expiry and vanishes at expiry. A gap
#             that is unremarkable at 7 DTE is impossible at 0 DTE.
#   NOISE  -- how much the strikes disagree AMONG THEMSELVES. This is the
#             chain's own estimate of its precision and it is free: if five
#             strikes agree to 2 points the forward is good to 2 points, and
#             if they disagree by 40 the chain is incoherent and nothing
#             built on it can be trusted either.
#
# These constants are judgment, not fitted quantities, and are written
# plainly so they can be argued with.
CARRY_RATE = 0.048            # ~repo less dividend yield, annualised
BASIS_FLOOR = 10.0            # irreducible quote noise, points
DISPERSION_K = 2.0            # multiples of the chain's own disagreement
CHAIN_INCOHERENT = 25.0       # strikes disagreeing by more than this: refuse


def forward_and_spread(chain, spot, n=5):
    """(forward, dispersion) from put-call parity across the near strikes.

    The forward is the median implied underlying -- the same quantity
    forward_from_parity returns. The dispersion is the full range of implied
    underlyings across those strikes: the chain's own statement of how
    precisely it knows its own reference price.

    Returns (None, None) when too few strikes carry two-sided time value.
    """
    if not chain or spot <= 0:
        return None, None
    near = sorted(chain, key=lambda r: abs(float(r["strike"]) - spot))[:n]
    fwds = []
    for r in near:
        try:
            c, p = float(r["ce"]), float(r["pe"])
        except (TypeError, ValueError):
            continue
        if c > 0 and p > 0:
            fwds.append(float(r["strike"]) + c - p)
    if len(fwds) < 3:
        return None, None
    fwds.sort()
    return fwds[len(fwds) // 2], fwds[-1] - fwds[0]


def basis_tolerance(spot, years, dispersion):
    """How far the index may sit from the chain before we disbelieve it.

    `years` is the time to expiry as a fraction of a year, so on expiry day
    the carry allowance collapses to almost nothing -- which is correct. A
    forward and a spot still 45 points apart three minutes before settlement
    cannot both be right.
    """
    carry = abs(spot) * CARRY_RATE * max(0.0, years)
    return BASIS_FLOOR + carry + DISPERSION_K * (dispersion or 0.0)


def spot_is_believable(chain, spot, years, n=5):
    """(ok, forward, dispersion, tolerance, why).

    `why` is None when the spot is believable, otherwise a sentence naming
    what went wrong. The caller decides whether to refuse or merely record.
    """
    fwd, disp = forward_and_spread(chain, spot, n)
    if fwd is None:
        return False, None, None, None, "too few two-sided strikes"
    tol = basis_tolerance(spot, years, disp)
    if disp > CHAIN_INCOHERENT:
        return (False, fwd, disp, tol,
                f"the chain disagrees with itself: near strikes imply "
                f"underlyings {disp:,.0f} pts apart")
    if abs(fwd - spot) > tol:
        return (False, fwd, disp, tol,
                f"index not contemporaneous: chain implies {fwd:,.2f} "
                f"({n} strikes within {disp:.1f} pts), index says "
                f"{spot:,.2f}, gap {fwd - spot:+,.1f} vs tolerance {tol:,.1f}")
    return True, fwd, disp, tol, None


def smile(chain, S, T, moneyness=0.10, forward=None):
    """Per-strike IV across the chain, priced off the forward.

    `chain` is [{strike, ce, pe}, ...]. Uses the OTM side of each strike --
    calls above the forward, puts below -- because the OTM option carries all
    the information and the ITM one is mostly intrinsic, where IV is
    numerically hopeless.

    Returns [{strike, iv, kind}] for strikes within `moneyness` of the forward
    that produced a solvable vol. Strikes that did not solve are simply
    absent; the caller decides what to do about gaps rather than getting a
    fabricated number.
    """
    F = forward if forward is not None else forward_from_parity(chain, S)
    if F <= 0:
        return []
    out = []
    for row in chain:
        K = float(row["strike"])
        if abs(K / F - 1.0) > moneyness:
            continue
        kind = "CE" if K >= F else "PE"
        px = float(row["ce"] if kind == "CE" else row["pe"])
        iv = implied_vol(px, F, K, T, kind)
        if iv is not None:
            out.append({"strike": K, "iv": iv, "kind": kind})
    return out


def atm_iv(chain, S, T, forward=None):
    """The single IV closest to the money -- a like-for-like replacement for
    VIX that is measured on THIS chain rather than an index-wide average."""
    F = forward if forward is not None else forward_from_parity(chain, S)
    pts = smile(chain, S, T, moneyness=0.03, forward=F)
    if not pts:
        return None
    return min(pts, key=lambda p: abs(p["strike"] - F))["iv"]


def skew(chain, S, T, wing=0.05, forward=None):
    """Skew proxy: put IV minus call IV at +/- `wing` moneyness.

    Positive means puts are dearer than calls -- the normal state for an
    equity index, and precisely what a single flat VIX cannot represent.
    """
    F = forward if forward is not None else forward_from_parity(chain, S)
    pts = smile(chain, S, T, moneyness=wing + 0.01, forward=F)
    puts = [p for p in pts if p["kind"] == "PE" and p["strike"] < F]
    calls = [p for p in pts if p["kind"] == "CE" and p["strike"] > F]
    if not puts or not calls:
        return None
    tgt_p, tgt_c = F * (1 - wing), F * (1 + wing)
    p = min(puts, key=lambda x: abs(x["strike"] - tgt_p))
    c = min(calls, key=lambda x: abs(x["strike"] - tgt_c))
    return p["iv"] - c["iv"]


# ------------------------------------------------------ payoffs and outcomes

def expected_call(S, K, sigma, T):
    """Lognormal counterpart of pricing.expected_call."""
    return bs_call(S, K, sigma, T)


def expected_put(S, K, sigma, T):
    """Lognormal counterpart of pricing.expected_put."""
    return bs_put(S, K, sigma, T)


def fair_straddle(S, sigma, T):
    """ATM straddle value under lognormal. The workbook's normal-model version
    is sqrt(2/pi)*sigma_points; these converge as sigma*sqrt(T) -> 0."""
    return bs_call(S, S, sigma, T) + bs_put(S, S, sigma, T)


def prob_below(S, level, sigma, T):
    """P(S_T < level) under lognormal, zero drift."""
    if level <= 0:
        return 0.0
    if sigma <= 0 or T <= 0:
        return 1.0 if S < level else 0.0
    v = sigma * sqrt(T)
    return norm_cdf((log(level / S) + 0.5 * sigma * sigma * T) / v)


def probability_of_profit(S, lower_be, upper_be, sigma, T):
    """Lognormal counterpart of pricing.probability_of_profit."""
    return prob_below(S, upper_be, sigma, T) - prob_below(S, lower_be, sigma, T)


def prob_touch_up(S, barrier, sigma, T):
    """P(the path reaches `barrier` above S at any time before T).

    First-passage for X_t = mu*t + sigma*W_t with mu = -sigma^2/2:
        P = Phi((mu*T - b)/(sigma*sqrt(T)))
          + exp(2*mu*b/sigma^2) * Phi((-b - mu*T)/(sigma*sqrt(T)))
    where b = ln(barrier/S).

    This is the honest version of Daily_Signal row 44. The workbook's
    min(1, 2*(1-POP)) doubles the terminal miss probability, which is the
    reflection principle applied to a driftless random walk and evaluated at
    the breakeven rather than the stop -- right idea, wrong level, no drift
    correction.
    """
    if barrier <= S:
        return 1.0
    if sigma <= 0 or T <= 0:
        return 0.0
    b = log(barrier / S)
    mu = -0.5 * sigma * sigma
    v = sigma * sqrt(T)
    return (norm_cdf((mu * T - b) / v)
            + exp(2.0 * mu * b / (sigma * sigma)) * norm_cdf((-b - mu * T) / v))


def prob_touch_down(S, barrier, sigma, T):
    """P(the path reaches `barrier` below S before T). By symmetry in logs."""
    if barrier >= S:
        return 1.0
    if sigma <= 0 or T <= 0:
        return 0.0
    b = log(S / barrier)
    mu = 0.5 * sigma * sigma          # sign flips going the other way
    v = sigma * sqrt(T)
    return (norm_cdf((-mu * T - b) / v)
            + exp(-2.0 * mu * b / (sigma * sigma)) * norm_cdf((-b + mu * T) / v))


def prob_touch_either(S, lower, upper, sigma, T):
    """P(either breakeven is touched before expiry.

    Union bound, capped at 1. Double-touch is possible but rare over a few
    days at index vol, and ignoring it errs toward overstating the risk --
    the safe direction for a number used to size a short position.
    """
    return min(1.0, prob_touch_up(S, upper, sigma, T)
               + prob_touch_down(S, lower, sigma, T))

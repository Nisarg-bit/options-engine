"""What time decay is actually worth, per structure rather than per session.

THE PROBLEM THIS REPLACES

intraday_vrp.theoretical_decay returns a pure time fraction:

    1 - sqrt(s_out / s_in)

and simulate() applies it as `theo = fraction * credit`. That fraction takes
no strike, no width and no credit -- it is identical for an ATM straddle and
for a strangle four strikes out on the same day. Two consequences, both bad:

  * "Pick the structure with the highest expected value" degenerates into
    "pick the one with the highest credit", which is the ATM straddle on
    every session. A selection rule built on it would change nothing while
    appearing to choose.

  * The `capture` column (realised P&L over theoretical decay) measures a
    strangle against an ATM yardstick. Wings are convex in sigma and shed
    relative value faster than an at-the-money option, so a strangle's
    theoretical decay is NOT its credit times the straddle's time fraction.

THE REPLACEMENT

Value each leg under the same normal (Bachelier) model the rest of the
project uses, at the sigma implied for entry and for exit, and take the
difference:

    sigma_t   = sigma_session * sqrt(sessions_left at t)
    value(t)  = sum over legs of expected_call/expected_put(F, K, sigma_t)
    decay     = value(entry) - value(exit)

WHY THIS IS A GENERALISATION, NOT A REPLACEMENT

For a straddle struck at the forward, sum(expected_call + expected_put)
collapses to sqrt(2/pi) * sigma exactly -- the identity test_pricing already
asserts. So

    decay = 0.7979 * sigma_session * (sqrt(s_in) - sqrt(s_out))

and the old model, fed the MODEL straddle as its credit, gives

    credit * (1 - sqrt(s_out/s_in))
      = 0.7979 * sigma_session * sqrt(s_in) * (1 - sqrt(s_out/s_in))
      = 0.7979 * sigma_session * (sqrt(s_in) - sqrt(s_out))

which is the same number. The two models agree on the straddle by
construction and diverge only on the wings, which is exactly where the old
one was wrong.

IN PRACTICE THEY DIFFER SLIGHTLY EVEN ON THE STRADDLE, and it is worth being
precise about why rather than claiming they do not. The old model scales the
MARKET credit, taken at the bid; this one values the structure at a sigma
inverted from the MID. The gap between them is the half-spread, which the
intraday study measured at about 0.2% of credit. probe block 3 measures it
rather than assuming it stays small.
"""

from math import sqrt

import pricing as P

SESSION_MINUTES = 375           # 09:15 to 15:29 inclusive, as intraday_vrp
BACHELIER = P.SQRT_2_OVER_PI    # 0.797885


def sessions_left(trading_days_out, minute_index):
    """Time to expiry in SESSIONS, today counted as the fraction still to run.

    Mirrors intraday_vrp.theoretical_decay's convention exactly so the two
    can be compared on the same footing: an entry at 09:20 on a 4-sessions-out
    expiry has 4 + 370/375 sessions left.
    """
    return trading_days_out + (SESSION_MINUTES - minute_index) / SESSION_MINUTES


def session_sigma(atm_straddle_mid, s_in):
    """Sigma per sqrt(session), inverted from the at-the-money straddle.

    This is the intraday form of sigma_sources' `chain_iv`: the volatility
    THIS expiry is actually priced at, rather than a 30-day index applied to
    a horizon of hours. Taken from the MID, not the bid -- a model input
    should not carry the spread the trade pays.
    """
    if not atm_straddle_mid or s_in <= 0:
        return None
    return atm_straddle_mid / BACHELIER / sqrt(s_in)


def structure_value(legs, forward, sigma):
    """Undiscounted Bachelier value of a set of short legs, in points.

    `legs` is [(strike, "CE"|"PE"), ...] -- the same shape intraday_vrp's
    legs_for produces. Returned positive: this is what it would cost to buy
    the structure back if nothing moved and only sigma changed.
    """
    if sigma is None or sigma <= 0 or not forward:
        return None
    total = 0.0
    for strike, kind in legs:
        if kind == "CE":
            total += P.expected_call(forward, strike, sigma)
        else:
            total += P.expected_put(forward, strike, sigma)
    return total


def decay_points(legs, forward, sigma_sess, s_in, s_out):
    """Theoretical decay of a short structure over the holding window.

    Assumes the forward does not move -- that is what "theoretical decay"
    means, and it is the yardstick the realised P&L is measured against. The
    forward moving is the thing `capture` is designed to expose.
    """
    if sigma_sess is None or s_in <= 0 or s_out < 0:
        return None
    v_in = structure_value(legs, forward, sigma_sess * sqrt(s_in))
    v_out = structure_value(legs, forward, sigma_sess * sqrt(max(s_out, 0.0)))
    if v_in is None or v_out is None:
        return None
    return v_in - v_out


def net_ev_rupees(legs, forward, sigma_sess, s_in, s_out, credit_pts, lot,
                  costs=None):
    """Decay in rupees, less the round trip's costs. The selection rule.

    `credit_pts` is what the structure actually sells for -- it does not
    enter the decay (which is a model quantity) but it does drive the
    statutory costs, which are levied on premium.
    """
    d = decay_points(legs, forward, sigma_sess, s_in, s_out)
    if d is None:
        return None
    fees = P.transaction_costs(credit_pts, lot, 1, defined_risk=False,
                               costs=costs or P.DEFAULT_COSTS)
    return d * lot - fees


def choose(candidates):
    """Highest net EV wins; ties break toward the WIDER structure.

    A tie means the model cannot separate them, and in that case the wider
    structure is the one further from the money. That is a stated preference,
    not a measured one, and it only ever applies when the EVs are equal.
    """
    usable = [c for c in candidates if c.get("net_ev_rs") is not None]
    if not usable:
        return None
    return max(usable, key=lambda c: (c["net_ev_rs"], c.get("width", 0)))

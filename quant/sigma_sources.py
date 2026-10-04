"""Where sigma comes from, and what changes when you change it.

Every POP, expected value, one-sigma band and entry-gate reading in this
system is the same formula:

    sigma = spot * (vol / 100) * sqrt(dte / 365)

Only `vol` differs between sources. This module decides which one to use and,
just as importantly, refuses to compute the things that stop meaning anything
when it is not the workbook's choice.

    vix       India VIX. A THIRTY-DAY implied volatility applied to a horizon
              that is often one day. The workbook's choice, and the default
              here. It is also the reason the weekly test failed: at 0 DTE it
              claimed 78.2% safety and delivered 57.9%, because a 30-day vol
              understates a same-day move by about a third.

    chain_iv  The at-the-money implied volatility of THIS expiry, solved from
              this chain. Right horizon, no external feed. api.market_ref
              already uses it for every underlying that is not NIFTY, so this
              is the existing path with the India VIX branch skipped.

    realised  RMS of the session moves actually observed over the trailing
              window, annualised on the same 365 basis. What the index DID
              rather than what it was priced to do.


WHY THE GATE IS NOT COMPUTED FOR THE LAST TWO
---------------------------------------------
credit/sigma is compared against CS_GATE = 0.9346, a threshold fitted against
VIX sigma over two years of bhavcopy. The threshold does not survive a change
of denominator, and in two different ways:

  * Under chain_iv the ratio is pinned near the Bachelier constant
    sqrt(2/pi) = 0.7979 BY CONSTRUCTION. Sigma is solved from the same
    at-the-money options whose mids make up the straddle, so the ratio is
    measuring the gap between the lognormal and normal models, not the
    market.

    NOT exactly 0.7979, and an earlier version of this note said it was. The
    inversion is only exact if sigma comes from a Bachelier solve; models
    .atm_iv is a lognormal bisection at the strike nearest the forward, so
    the ratio picks up a second-order correction and the strike-versus-
    forward offset. Measured on live NIFTY, four expiries:

        dte  4   0.8302        dte 18   0.8341
        dte 11   0.8322        dte 25   0.8366

    A spread of 0.0064 over three weeks of DTE, sitting ~4% above the
    Bachelier floor and 11% below the gate. It drifts UP with DTE, which is
    that second-order term growing with sigma*sqrt(T) -- but 0.006 per three
    weeks does not close a gap of 0.098 at any expiry anyone trades. Pinned
    in a narrow band, not a single constant, and still a permanently shut
    gate wearing the costume of a market reading.

  * Under realised the ratio IS meaningful -- it is the variance risk premium
    -- but 0.9346 was never fitted to it. The intraday study measured median
    realised/implied at 0.57, on 13 sessions out of 13, which puts
    credit/sigma near 1.4. Applying the old threshold would swing the gate
    from shut-every-session to open-most-days purely because the units
    changed underneath it.

So any source other than "vix" returns c_over_sigma=None and
gate_passed=None, with `gate_note` stating which of the two reasons applies.
Recalibrating a threshold per source is a study, not a query parameter.


THE SAME ARGUMENT APPLIES TO THE UNDERLYING
-------------------------------------------
0.9346 was fitted on two years of NIFTY bhavcopy. Nothing about it is a
property of index options in general: it is where NIFTY's credit/sigma
distribution happened to sit against a POP of 0.65.

This was missed until SENSEX was added, and the miss was not SENSEX's. The
gate has been rendering for BANKNIFTY since the day that underlying was
collected -- a different index, different lot, different strike step, a
volatility that runs several points above NIFTY's, and a threshold borrowed
wholesale with nothing on the page to say so. It read exactly like a NIFTY
gate reading, which is the worst way for it to be wrong.

So the gate now also requires the underlying it was fitted on. Everything
else gets gate_applies=False and a note naming the gap, the same treatment
chain_iv and realised already get. That turns the gate OFF for BANKNIFTY,
which is a visible change and the correct one: a verdict with no calibration
behind it is worth less than no verdict.
"""

from math import sqrt

BASIS = 365.0               # matches pricing.TRADING_DAYS_BASIS
SOURCES = ("vix", "chain_iv", "realised")
DEFAULT = "vix"

# The underlying CS_GATE was fitted on. A tuple rather than a constant
# because refitting a second one is a study someone might actually do, and
# when they do the change belongs here rather than in a condition.
GATE_FITTED_ON = ("NIFTY",)

# How many completed sessions the realised estimate looks back over. Twenty
# is about a month of trading: long enough that one violent session does not
# dominate the RMS, short enough to still describe the current regime.
REALISED_LOOKBACK = 20

# A session move larger than this share of spot is not a market move, it is a
# loading bug. The first version of the loader read the INDEX partition
# without filtering by instrument, so it differenced a VIX close near 12
# against a NIFTY close near 23,000 and produced session moves of ~23,000
# points -- served as a realised volatility of 2723% and a sigma of 66,342
# points on a 23,000 index. The loader is fixed; this bound is here so that
# the NEXT loader bug surfaces as "unavailable" rather than as a number.
MAX_SESSION_MOVE_FRAC = 0.10

_GATE_NOTE = {
    "chain_iv":
        "not computed: with sigma solved from this chain's own ATM options, "
        "credit/sigma is pinned near sqrt(2/pi)=0.798 by construction -- "
        "measured around 0.83 on live NIFTY -- and cannot reach the 0.9346 "
        "gate. It would measure the lognormal-vs-normal model gap, not the "
        "market.",
    "realised":
        "not computed: credit/sigma against realised vol is the variance "
        "risk premium and is meaningful, but the 0.9346 threshold was fitted "
        "against VIX sigma. Median realised/implied of 0.57 puts this ratio "
        "near 1.4, so the old threshold would open the gate on a change of "
        "units rather than a change in the market.",
}


class UnknownSource(ValueError):
    pass


def normalise(source):
    """Accept None/blank as the default; reject anything unrecognised.

    Rejecting rather than silently defaulting matters: a typo like
    `sigma_source=realized` must not quietly serve VIX numbers labelled as
    something else.
    """
    s = (source or DEFAULT).strip().lower()
    if s not in SOURCES:
        raise UnknownSource(
            f"unknown sigma_source {source!r}; expected one of {', '.join(SOURCES)}")
    return s


def rms(xs):
    xs = [float(x) for x in xs if x is not None]
    return sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def realised_units(session_moves, spot, lookback=REALISED_LOOKBACK):
    """Trailing realised volatility, expressed in VIX units (percent).

    `session_moves` are absolute point moves, one per COMPLETED session,
    oldest first -- close minus open within the session, which is what
    realised_vol.py measures. Returning VIX units rather than an absolute
    sigma is deliberate: it goes through the identical Volatility formula as
    the other two sources, so the three are directly comparable and there is
    only ever one place the sigma arithmetic lives.
    """
    if not spot or spot <= 0:
        return None
    recent = [m for m in (session_moves or []) if m is not None][-lookback:]
    if len(recent) < 5:          # fewer than a week is not an estimate
        return None
    r = rms(recent)
    if not r:
        return None
    if r > MAX_SESSION_MOVE_FRAC * spot:
        return None          # see MAX_SESSION_MOVE_FRAC
    return (r / spot) * sqrt(BASIS) * 100.0


def sigma_points(spot, vol_units, dte):
    """The one sigma formula, spelled out once.

    Mirrors pricing.Volatility.expiry_stdev exactly, including the floor of
    one day inside the sqrt -- on expiry day itself that stops sigma
    collapsing toward zero and reporting a POP near 1.0 on a position that is
    about to be decided.
    """
    if not spot or vol_units is None:
        return None
    return spot * (vol_units / 100.0) * sqrt(max(float(dte), 1.0) / BASIS)


def gate_applies_to(underlying):
    """Is the 0.9346 threshold calibrated for this underlying?

    An unnamed underlying counts as NOT fitted. The safe default is to
    withhold a verdict rather than to assert one: a caller that forgets to
    say which index it is asking about loses the gate visibly, instead of
    quietly receiving NIFTY's threshold applied to something else.
    """
    return underlying in GATE_FITTED_ON


def resolve(source, spot, dte, vix_units=None, chain_iv_units=None,
            session_moves=None, underlying=None):
    """Pick the volatility for `source` and say where it came from.

    Returns a dict that is safe to merge straight into an API payload. It
    always reports every source it could compute, not only the chosen one --
    a sigma with no provenance is a number nobody can check, and the whole
    point of this parameter is to let the three be compared.
    """
    src = normalise(source)

    rv_units = realised_units(session_moves, spot)
    available = {
        "vix": _round(vix_units),
        "chain_iv": _round(chain_iv_units),
        "realised": _round(rv_units),
    }
    chosen = {"vix": vix_units, "chain_iv": chain_iv_units,
              "realised": rv_units}[src]

    fell_back = None
    if chosen is None and src != DEFAULT:
        # Better to serve the workbook's number and SAY so than to 500, and
        # far better than serving a silent None that renders as a blank card.
        fell_back, src, chosen = src, DEFAULT, vix_units

    sigma = sigma_points(spot, chosen, dte)
    fitted = gate_applies_to(underlying)
    gate_ok = src == DEFAULT and fitted

    if gate_ok:
        note = None
    elif src != DEFAULT:
        note = _GATE_NOTE[src]
    else:
        note = (
            f"not computed: the 0.9346 threshold was fitted on two years of "
            f"{' and '.join(GATE_FITTED_ON)} bhavcopy, against that index's "
            f"own credit/sigma distribution. "
            + (f"{underlying} is a different index with a different lot, "
               f"strike step and volatility level"
               if underlying else "no underlying was named")
            + ", and there is no reason the same number separates a thin "
              "premium from a fat one there. Refitting it is a study, not a "
              "substitution.")

    return {
        "sigma_source": src,
        "sigma_vol_units": _round(chosen),
        "sigma": round(sigma, 2) if sigma is not None else None,
        "sigma_available": available,
        "sigma_lookback": REALISED_LOOKBACK if src == "realised" else None,
        "gate_applies": gate_ok,
        "gate_note": note,
        "gate_fitted_on": list(GATE_FITTED_ON),
        "sigma_fallback_from": fell_back,
        "sigma_fallback_note": (
            f"{fell_back} was requested but could not be computed for this "
            f"chain; served {DEFAULT} instead" if fell_back else None),
    }


def _round(v, p=4):
    return round(float(v), p) if v is not None else None

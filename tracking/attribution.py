"""Where a session's paper P&L actually came from.

WHY THIS EXISTS

The journal records what each session made and lost. It does not say WHY, and
the why is the whole question: a short straddle that ends the day up has been
paid for time and charged for movement, and the two can be any size in either
direction while the net looks calm. Fourteen sessions with a median of +414
and a mean of -56 is not a result. Fourteen sessions where the decay was
steady and three days of movement took it all back is the beginning of one.

THE DECOMPOSITION

Value the structure under the same Bachelier model the rest of the project
uses, at three points, and let the terms telescope:

    mid_in            what the structure was worth at entry, at the mid
    V(F0, sigma_in)   the model's value there
    V(F0, sigma_out)  same forward, time moved on
    V(F1, sigma_out)  forward moved too
    mid_out           what it actually cost to close, at the mid

    entry gap = mid_in          - V(F0, sigma_in)
    decay     = V(F0, sigma_in) - V(F0, sigma_out)
    direction = V(F0, sigma_out)- V(F1, sigma_out)
    vol       = V(F1, sigma_out)- mid_out
    ------------------------------------------------
    sum       = mid_in - mid_out

Every term is signed from the SHORT seller's point of view: positive is money
in. Decay is positive whenever time passes. Direction is negative whenever the
forward moves away from the strike, which for a short straddle is any move at
all.

THE SPREAD IS NOT A ROUNDING ERROR

`credit` is a BID and `cover` is an ASK, so the realised P&L is the mid-to-mid
move MINUS half of each spread:

    pnl_pts = (mid_in - mid_out) - (spread_in + spread_out) / 2

Charging the entry spread twice, which is the obvious shortcut when only one
spread is recorded, moves the difference into the decay term and flatters it.
Both ends are measured.

WHY THE ENTRY GAP IS ZERO, AND WHY IT IS STILL REPORTED

sigma_session is inverted from the at-the-money straddle's MID at entry, and
for a straddle struck at the forward the Bachelier value collapses to
sqrt(2/pi) * sigma exactly. So for the ATM straddle this journal actually
trades, V(F0, sigma_in) IS mid_in and the entry gap is zero to floating point.

It is still computed and still carried, because the moment a strangle is
journalled it stops being zero: sigma is inverted from the at-the-money
straddle and the wings are not at the money. An attribution that silently
absorbed that into `vol` would report a model error as a market observation.
A residual that is visible is a residual that can be argued with.

WHAT `vol` HONESTLY MEANS

Everything the model cannot place. For the ATM straddle -- where the entry gap
is exactly zero -- it is precisely the market's implied volatility at exit
differing from the entry volatility scaled by the square root of time, which
is what a volatility term should be. It is not a separately measured quantity
and it should not be read as one.
"""

from math import sqrt

import decay_model as DM


COMPONENTS = ("decay", "direction", "vol", "spread", "entry_gap")


def attribute(sim, lot):
    """Split one `intraday_vrp.simulate` result into its sources.

    Returns None rather than a partial answer when the session cannot be
    valued -- a missing leg quote at either end makes every term below it
    meaningless, and a decomposition with a hole in it is worse than no
    decomposition at all.
    """
    need = ("legs", "fwd_open", "fwd_exit", "sigma_session", "s_in", "s_out",
            "credit", "cover", "spread_at_entry", "spread_at_exit")
    if any(sim.get(k) is None for k in need):
        return None

    sig = sim["sigma_session"]
    f0, f1 = sim["fwd_open"], sim["fwd_exit"]
    legs = sim["legs"]

    sigma_in = sig * sqrt(sim["s_in"])
    sigma_out = sig * sqrt(max(sim["s_out"], 0.0))

    v_in_f0 = DM.structure_value(legs, f0, sigma_in)
    v_out_f0 = DM.structure_value(legs, f0, sigma_out)
    v_out_f1 = DM.structure_value(legs, f1, sigma_out)
    if None in (v_in_f0, v_out_f0, v_out_f1):
        return None

    mid_in = sim["credit"] + sim["spread_at_entry"] / 2.0
    mid_out = sim["cover"] - sim["spread_at_exit"] / 2.0

    entry_gap = mid_in - v_in_f0
    decay = v_in_f0 - v_out_f0
    direction = v_out_f0 - v_out_f1
    vol = v_out_f1 - mid_out
    spread = (sim["spread_at_entry"] + sim["spread_at_exit"]) / 2.0

    pts = {"decay": decay, "direction": direction, "vol": vol,
           "spread": -spread, "entry_gap": entry_gap}
    modelled = sum(pts.values())

    return {
        "pts": pts,
        "rs": {k: v * lot for k, v in pts.items()},
        "fees_rs": sim["fees_rs"],
        # The reconciliation, carried rather than asserted. `modelled` is
        # built only from model values and measured spreads; `pnl_pts` is
        # credit minus cover, computed nowhere near here. They agree or this
        # module is wrong, and a caller can see which.
        "modelled_pts": modelled,
        "pnl_pts": sim["pnl_pts"],
        "unexplained_pts": sim["pnl_pts"] - modelled,
        "net_rs": modelled * lot - sim["fees_rs"],
    }


def row_fields(att):
    """The flat columns the journal stores, or blanks when unattributable.

    Blank rather than zero: a session that could not be valued and a session
    whose decay really was zero must not read the same on the page.
    """
    if not att:
        return {f"{k}_pts": "" for k in COMPONENTS} | {"unexplained_pts": ""}
    out = {f"{k}_pts": round(v, 3) for k, v in att["pts"].items()}
    out["unexplained_pts"] = round(att["unexplained_pts"], 4)
    return out

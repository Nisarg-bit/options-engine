"""Two endpoints over series.py and chain_buildup.py, mounted onto api.py.

WHY A SEPARATE MODULE

api.py is a running service. Adding routes by editing a 1,200-line file
means re-uploading the whole thing and restarting on top of it, and a
mistake anywhere in that file takes the dashboard down, not just the new
charts. This module carries the new routes and api.py gains two lines at
the bottom. If these endpoints misbehave, deleting those two lines is the
rollback.

WHAT THIS FIXES RELATIVE TO CALLING by_minute() DIRECTLY

series_probe.by_minute() is proven against real parquet, and it is used
here unmodified -- that is the point of it. But it was written as a probe:
it globs the session, reads every part, and then builds a nested dict with
a Python-level loop over every row. Run once from a terminal that is fine.
Run it per request and every dashboard poll re-reads and re-walks the whole
session.

api.py is built to avoid exactly that. read_parts() caches DataFrames keyed
on (mtime, size) and evicts yesterday's, and the rate limiter's own comment
names /api/intraday -- "which reads the whole session's bars" -- as the load
this 2 GB box has to be protected from. The collector is the tenant that
matters.

So by_minute() is called behind a cache keyed on the SIGNATURE of the
parquet parts: the sorted (path, mtime, size) of every file in the
partition. The collector flushes a new part roughly every five minutes, so
that key changes about twelve times an hour and not once per poll. It is
not a TTL -- a TTL either re-reads work that has not changed or serves data
that has. A signature does neither.

ON THE RELATIVE PATH

series_probe.BARS is "data/bars_1m", relative to the working directory. I
went looking for a bug here and there isn't one: options-api.service sets
WorkingDirectory=/home/ubuntu and run_api.sh cd's there as well, so it
resolves to the same place api.py's absolute BARS does. It is right by
coincidence of CWD rather than by construction, though, so it is anchored
explicitly below -- one assignment, no edit to the proven file.
"""

import glob
import os

from fastapi import APIRouter, HTTPException, Query

import chain_buildup as CB
import series as S
import series_probe as SP

router = APIRouter()

# Anchor the probe's relative BARS to the same absolute path api.py uses, so
# these endpoints do not depend on which directory the service was started
# from. Assignment rather than an edit: series_probe.py is the file that was
# proven against real data, and it stays byte-for-byte that file.
_ROOT = os.path.dirname(os.path.abspath(__file__))
SP.BARS = os.path.join(_ROOT, "data", "bars_1m")

NEAR_STRIKES = CB.NEAR_STRIKES


# ------------------------------------------------------------------ plumbing

def _signature(day, underlying):
    """(path, mtime, size) for every part in the partition, sorted.

    Cheap: a stat per file, no reads. The collector only ever appends parts
    and rewrites the one it is filling, so this changes exactly when there
    is new data to see.
    """
    pattern = os.path.join(SP.BARS, f"date={day}", f"underlying={underlying}",
                           "*.parquet")
    sig = []
    for p in sorted(glob.glob(pattern)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig.append((p, st.st_mtime, st.st_size))
    return tuple(sig)


_bm_cache = {}      # (day, underlying, expiry) -> (signature, by_minute dict)


def by_minute_cached(day, underlying, expiry):
    """series_probe.by_minute(), recomputed only when the parts change."""
    key = (str(day), underlying, str(expiry))
    sig = _signature(day, underlying)
    hit = _bm_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1], None

    bm, err = SP.by_minute(day, underlying, expiry)
    if err:
        return {}, err

    # Hold one session only. Same reasoning as read_parts' eviction: on a
    # 2 GB box yesterday's minutes would otherwise sit here until restart.
    for k in [k for k in _bm_cache if k[0] != str(day)]:
        _bm_cache.pop(k, None)
    _bm_cache[key] = (sig, bm)
    return bm, None


def jnum(v, nd=4):
    """A JSON-safe number, or the word for the kind of nothing it is.

    indicators.frac_change returns +inf for growth from zero and -inf for
    decay to it, and json.dumps refuses both outright -- the whole response
    500s, not just the field, which is the same failure records() was
    written for on the journal side. Those two cases are information, so
    they become "new" and "gone" rather than null.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                      # NaN
        return None
    if f == float("inf"):
        return "new"
    if f == float("-inf"):
        return "gone"
    return round(f, nd)


def _resolve(underlying, expiry):
    """Session, expiry and DTE, borrowing api.py's own resolver.

    Imported inside the function on purpose. api.py imports this module at
    the bottom of the file, so importing api at module scope here would be
    circular; by the time a request arrives, api is fully initialised in
    sys.modules and this is an ordinary lookup.
    """
    import api as API
    return API.resolve(underlying, expiry)


def _as_of(bm):
    return max(bm) if bm else None


# -------------------------------------------------------------------- series

@router.get("/api/series")
def api_series(underlying: str = "NIFTY", expiry: str = Query(None),
               structure: str = "straddle", anchor: str = "atm_open",
               width: int = 2, ma: int = 20, ema: int = 20, rsi: int = 14,
               every: int = 1, compact: int = 0):
    """Per-minute price for one structure, with VWAP, MA, EMA and RSI.

    `anchor` is the argument that decides whether the indicators mean
    anything, and series.py refuses rather than fakes them:

        atm_open      the ATM as it stood at 09:20, held all session. One
                      contract, continuous price -- indicators are valid.
        <a strike>    that strike, held all session. Same guarantees.
        atm_rolling   the ATM at each minute. What /api/intraday already
                      returns. VWAP, MA, EMA and RSI come back null,
                      because a VWAP carried across a strike roll averages
                      two different instruments.

    So a chart that wants VWAP on the ATM straddle cannot use the rolling
    anchor the existing intraday chart uses. That is a real difference in
    what is being plotted, and `notes` says so on every response.
    """
    if structure not in S.STRUCTURES:
        raise HTTPException(400, f"structure must be one of "
                                 f"{', '.join(S.STRUCTURES)}")
    if anchor not in ("atm_open", "atm_rolling"):
        try:
            anchor = float(anchor)
        except (TypeError, ValueError):
            raise HTTPException(400, "anchor must be atm_open, atm_rolling "
                                     "or a strike")

    day, exp, dte = _resolve(underlying, expiry)
    bm, err = by_minute_cached(day, underlying, exp)
    if err:
        raise HTTPException(404, err)

    try:
        out = S.build(bm, structure=structure, anchor=anchor, width=width,
                      ma=ma, ema=ema, rsi=rsi)
    except ValueError as e:
        raise HTTPException(400, str(e))

    rows = out["rows"]
    if not rows:
        raise HTTPException(404, "; ".join(out["notes"]) or "no series")

    step = max(1, int(every))
    thinned = rows[::step]
    # Always keep the last point. Thinning that drops the most recent minute
    # makes a live chart lag its own data by up to `every` minutes.
    if thinned[-1] is not rows[-1]:
        thinned = thinned + [rows[-1]]

    payload = {
        "underlying": underlying, "session": str(day), "expiry": str(exp),
        "dte": dte, "as_of": _as_of(bm),
        "structure": out["structure"], "anchor": str(out["anchor"]),
        "width": out["width"], "strike_step": out["strike_step"],
        "strike_rolls": out["strike_rolls"],
        "indicators_valid": anchor != "atm_rolling",
        "points": len(thinned), "points_total": out["points"],
        "minutes_collected": len(bm),
        "notes": out["notes"],
    }

    if not compact:
        payload["rows"] = [
            {"ts": r["ts"], "price": r["price"], "vwap": r["vwap"],
             "ma": r["ma"], "ema": r["ema"], "rsi": r["rsi"],
             "oi": r["oi"], "legs": r["legs"]} for r in thinned]
        return payload

    # The legs repeat identically on every row of a fixed anchor, which is
    # most of the payload's bytes and none of its information. They are
    # hoisted out; `k` keeps the first leg's strike per row so a ROLLING
    # anchor still shows where it rolled, which is the one thing that would
    # otherwise be lost. Everything the charts read is still here.
    payload["legs"] = thinned[0]["legs"]
    payload["compact"] = True
    payload["rows"] = [
        {"ts": r["ts"], "price": r["price"], "vwap": r["vwap"],
         "ma": r["ma"], "ema": r["ema"], "rsi": r["rsi"],
         "oi": round(r["oi"]), "k": r["legs"][0]["strike"]} for r in thinned]
    return payload


# ------------------------------------------------------------------- buildup

def _agreement(rows, fwd):
    """Directional agreement near the money vs across the whole chain.

    The ranked table sorts by OI change, which puts near-money strikes at
    the top -- where delta dominates and the quadrants mostly restate the
    index move. Measured near the money the agreement runs around 90-100%
    and across the chain around 30-50%, and reporting only one of those is
    how a chart ends up looking like a signal. Both go in the payload so
    the page can show both.
    """
    def pct(sub, ot, want_up):
        vals = [r for r in sub if r["opt_type"] == ot and r["d_px"] is not None
                and abs(r["d_px"]) != float("inf")]
        if not vals:
            return None, 0
        hit = sum(1 for r in vals if (r["d_px"] > 0) == want_up)
        return round(100.0 * hit / len(vals), 1), len(vals)

    near = rows
    if fwd is not None:
        ks = sorted({r["strike"] for r in rows})
        if ks:
            step = S.infer_step(ks) or 50.0
            band = NEAR_STRIKES * step
            near = [r for r in rows if abs(r["strike"] - fwd) <= band]

    ce_near, n_ce_near = pct(near, "CE", True)
    pe_near, n_pe_near = pct(near, "PE", False)
    ce_all, n_ce_all = pct(rows, "CE", True)
    pe_all, n_pe_all = pct(rows, "PE", False)
    return {
        "near_strikes": NEAR_STRIKES,
        "near": {"calls_up_pct": ce_near, "calls_n": n_ce_near,
                 "puts_down_pct": pe_near, "puts_n": n_pe_near},
        "chain": {"calls_up_pct": ce_all, "calls_n": n_ce_all,
                  "puts_down_pct": pe_all, "puts_n": n_pe_all},
        "caveat": ("Ranking by OI change is a biased sample by construction: "
                   "it sorts near-money strikes to the top, where delta "
                   "dominates and the quadrants largely restate the index "
                   "move. Read the near-money and whole-chain figures "
                   "together."),
    }


def _tilt(rows):
    """Split the residual across strikes into its antisymmetric and symmetric parts.

    The two candidate explanations for a tilt have different shapes, so
    measuring both separates them without a model:

      ANTISYMMETRIC (`tilt`, high third minus low third). Skew steepening
      looks like this. So does a forward correction that over- or
      under-shifts, because the leftover is proportional to dF times the
      curve's slope, which is opposite-signed on the two wings. The two are
      told apart by whether the sign tracks dF across sessions -- one
      session cannot do it, which is why `forward_move` is in the payload
      beside this.

      SYMMETRIC (`hump`, the wings against the middle). The documented
      percentage artefact looks like this: the residual is a percentage of
      price and the wings are far more volatility-sensitive than the money,
      so a parallel vol shift lifts both ends relative to the centre.

    `slope` is the least-squares gradient of residual against strike,
    expressed per 100 points, which is the same antisymmetric signal as a
    single number rather than a difference of thirds.
    """
    pts = [(r["strike"], r["resid"]) for r in rows
           if isinstance(r["resid"], (int, float))]
    if len(pts) < 6:
        return {"n": len(pts), "note": "too few priced strikes to decompose"}
    pts.sort()
    n = len(pts)
    cut = n // 3
    low = [v for _, v in pts[:cut]]
    mid = [v for _, v in pts[cut:n - cut]]
    high = [v for _, v in pts[n - cut:]]
    mean = lambda xs: sum(xs) / len(xs)
    lo_m, mid_m, hi_m = mean(low), mean(mid), mean(high)

    kbar = sum(k for k, _ in pts) / n
    rbar = sum(v for _, v in pts) / n
    den = sum((k - kbar) ** 2 for k, _ in pts)
    slope = (sum((k - kbar) * (v - rbar) for k, v in pts) / den) if den else 0.0

    return {
        "n": n,
        "low_third": round(lo_m, 5), "mid_third": round(mid_m, 5),
        "high_third": round(hi_m, 5),
        # Antisymmetric: skew, or a forward correction that did not fully
        # remove the forward.
        "tilt": round(hi_m - lo_m, 5),
        # Symmetric: the wings against the middle -- the percentage artefact.
        "hump": round(lo_m + hi_m - 2 * mid_m, 5),
        "slope_per_100pt": round(slope * 100.0, 5),
    }


def _leg_row(r):
    return {"strike": r["strike"], "opt_type": r["opt_type"],
            "px_open": round(r["px_open"], 2), "px_last": round(r["px_last"], 2),
            "d_px": jnum(r["d_px"]),
            "oi_open": r["oi_open"], "oi_last": r["oi_last"],
            "oi_chg": r["oi_chg"], "d_oi": jnum(r["d_oi"]),
            "label": r["label"]}


def _otm_row(r):
    return {"strike": r["strike"], "opt_type": r["opt_type"],
            "px_open": round(r["px_open"], 2), "px_last": round(r["px_last"], 2),
            "px_expect": round(r["px_expect"], 2) if r["px_expect"] else None,
            "d_px": jnum(r["d_px"]), "d_adj": jnum(r["d_adj"]),
            "resid": jnum(r["resid"]),
            "oi_open": r["oi_open"], "oi_last": r["oi_last"],
            "oi_chg": r["oi_chg"], "d_oi": jnum(r["d_oi"]),
            "label": r["label"]}


@router.get("/api/buildup")
def api_buildup(underlying: str = "NIFTY", expiry: str = Query(None),
                view: str = "both", top: int = CB.DEFAULT_TOP,
                min_oi: int = CB.MIN_OI, compact: int = 0):
    """Per-strike positioning, in both the per-leg and OTM views.

    view=leg    the familiar four quadrants on the raw leg price.
    view=otm    the out-of-the-money leg with the forward's move removed
                via the opening curve read at K - dF. Model-free, but it
                assumes the smile is sticky in moneyness.
    view=both   default.

    `top` ranks by absolute OI change. The full row set is returned too,
    because the ranked table is the biased sample described in `agreement`.
    """
    if view not in ("leg", "otm", "both"):
        raise HTTPException(400, "view must be leg, otm or both")

    day, exp, dte = _resolve(underlying, expiry)
    bm, err = by_minute_cached(day, underlying, exp)
    if err:
        raise HTTPException(404, err)
    if len(bm) < 2:
        raise HTTPException(404, f"need at least two minutes, have {len(bm)}")

    out = {
        "underlying": underlying, "session": str(day), "expiry": str(exp),
        "dte": dte, "as_of": _as_of(bm),
        "opened": min(bm), "minutes_collected": len(bm),
        "min_oi": min_oi, "top": top,
    }

    # NIFTY weeklies expire Tuesday, so Wednesday is the first full session of
    # a newly-front weekly and last week's positions are still rolling into
    # it. Measured against 11:00 OI, the 09:15 reading is around 70-76% on a
    # roll day against roughly 95% two sessions later -- so a low "building"
    # count that morning is the calendar, not a broken feed.
    out["roll_day"] = dte >= 5
    if out["roll_day"]:
        out["roll_note"] = (
            f"{dte} DTE: this is an early session of a newly-front weekly. "
            f"Positions are still rolling in, so a lower share of contracts "
            f"reading as building is expected and is not a data fault.")

    if view in ("leg", "both"):
        rows, skipped = CB.strike_rows(bm, min_oi=min_oi)
        if not rows:
            raise HTTPException(404, skipped.get("reason", "no leg rows"))
        fwd = CB.parity_forward(bm[min(bm)])
        ranked = sorted(rows, key=lambda r: -abs(r["oi_chg"]))[:top]
        out["leg"] = {
            "forward_open": round(fwd, 2) if fwd else None,
            "counted": len(rows),
            "skipped": {k: v for k, v in skipped.items()
                        if k != "no_open_rows"},
            # Zero opening OI on a contract that carries real OI later is a
            # datum that did not arrive, not a position that did not exist.
            # Named separately rather than ranked first as "new".
            "no_opening_reading": skipped.get("no_open_rows", []),
            "agreement": _agreement(rows, fwd),
            "ranked": [_leg_row(r) for r in ranked],
        }
        # `agreement` is computed over every row before they are dropped, so
        # the whole-chain figure stays honest in compact mode -- it is the
        # counterweight to the ranked table and must not quietly become a
        # measure of the top twelve.
        if not compact:
            out["leg"]["rows"] = [_leg_row(r) for r in rows]

    if view in ("otm", "both"):
        rows, med, skipped = CB.otm_rows(bm, min_oi=min_oi)
        if not rows:
            raise HTTPException(404, skipped.get("reason", "no otm rows"))
        ranked = sorted(rows, key=lambda r: -abs(r["oi_chg"]))[:top]
        f0, f1 = skipped.get("fwd_open"), skipped.get("fwd_last")
        out["otm"] = {
            "forward_open": round(f0, 2) if f0 else None,
            "forward_last": round(f1, 2) if f1 else None,
            "forward_move": round(f1 - f0, 2) if (f0 and f1) else None,
            "median_residual": jnum(med),
            "counted": len(rows),
            "skipped": {k: v for k, v in skipped.items()
                        if k not in ("fwd_open", "fwd_last")},
            "ranked": [_otm_row(r) for r in ranked],
            # Computed here rather than on the page, because compact mode drops
            # the rows it needs -- and because this is the number the whole
            # view exists to produce, so it should not depend on which client
            # is reading it.
            "tilt": _tilt(rows),
            "limitation": (
                "The residual is a percentage of price, and a parallel "
                "volatility shift is not a parallel percentage shift -- the "
                "wings are far more volatility-sensitive than the money, so "
                "a uniform 10% vol rise alone produces residuals spanning "
                "roughly -24% to +39%. A symmetric tilt across the chain is "
                "therefore expected from this artefact and is not evidence "
                "of skew. Separating the two properly means inverting to "
                "implied volatility, which requires a model and is not done."),
        }
        if not compact:
            out["otm"]["rows"] = [_otm_row(r) for r in rows]

    return out

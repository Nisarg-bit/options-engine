"""Fit the two-term volatility model on five years of daily candles.

WHAT IS BEING FITTED

The old model says a calendar day carries one unit of variance and spreads it
evenly with sqrt(t):

    sigma_day = close * (VIX/100) * sqrt(1/365)

Ten sessions of collected data said that is wrong in a specific way: 27% of
the variance happened inside the session and 73% across the night. Nine gaps
is far too thin to build on, so this refits the same split on years of Kite
daily candles, where

    gap     = open[t]  - close[t-1]
    session = close[t] - open[t]

are directly observable.

HOW THE CONSTANTS ARE DEFINED

Every move is normalised by THAT DAY's model sigma before being pooled, so a
day when VIX was 11 and a day when VIX was 28 contribute on equal terms:

    k_session   = rms( session[t] / sigma_day[t] )
    k_gap(c)    = rms( gap[t]     / sigma_day[t] )   for each gap class c
    k_total     = rms( total[t]   / sigma_day[t] )

Then the model becomes

    sigma^2 = sigma_day^2 * ( k_session^2 * minutes/375 + SUM k_gap(c)^2 )

Note what this does to the LEVEL. Because each k is measured as a multiple of
the model's own sigma_day, using the raw values corrects the total as well as
the split -- if realised ran at 0.85x implied, then k_session^2 + k_gap^2 is
0.85^2 and every sigma shrinks accordingly. That is the aggressive choice and
it was made deliberately.

Set NORMALISE_TO_VIX = True to keep only the split and leave VIX's total
untouched; the k's are then rescaled so a full calendar day comes back to
exactly 1.0. One constant separates the two positions.

WHY IT REPORTS YEAR BY YEAR

A level correction fitted across five calm years and applied in a violent one
is at its most confident exactly when it should not be. The per-year table is
there so that a drifting ratio is visible BEFORE anything is switched on.
If those rows disagree materially, a single constant is the wrong model and
no amount of extra history fixes it.

GAP CLASSES are not assumed. A Friday-to-Monday gap carries three calendar
days of news rather than one, and a gap after a holiday more still, so they
are fitted separately and printed separately.

Candles are cached on disk, so re-running costs nothing and does not hit the
API again.

Usage:
    python vol_calibrate.py                 fit 5 years, write the constants
    python vol_calibrate.py --years 2       a different window
    python vol_calibrate.py --refresh       ignore the cache, refetch
    python vol_calibrate.py --dry           print, write nothing
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from math import sqrt

NIFTY_TOKEN = 256265
VIX_TOKEN = 264969
BASIS = 365.0
SESSION_MINUTES = 375

CACHE = "data/history"
OUT = "data/vol_calibration.json"

# False  -> raw k's, so the model corrects BOTH the split and the level.
# True   -> rescale so a full calendar day returns exactly 1.0, correcting
#           the split only and leaving VIX's view of the total intact.
NORMALISE_TO_VIX = False

CHUNK_DAYS = 360          # Kite caps the daily-candle range per request
SENSITIVITY = [5, 2, 1]   # years, longest first

# k_session and rho are flat across windows, so they get the long window and
# its large sample. k_gap has drifted about 26% in a year, so it gets a short
# one. Fitting every term on one compromise window would make the stable ones
# noisier and the drifting one stale at the same time.
WINDOW_SESSION = 5
WINDOW_RHO = 5
WINDOW_GAP = 2

# A gap class needs this many observations to be fitted on its own. Below it,
# fall back to the weekend constant -- five "long break" observations is not
# a measurement, and treating it as one puts a number nobody checked into
# every holiday-week sigma.
MIN_CLASS_N = 30
FALLBACK_CLASS = "weekend"


# ------------------------------------------------------------------ fetching

def cache_path(token, start, end):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, f"{token}_{start}_{end}_day.json")


def fetch(kite, token, start, end, refresh=False):
    """Daily candles, cached. Chunked because the API caps the range."""
    path = cache_path(token, start, end)
    if os.path.exists(path) and not refresh:
        try:
            with open(path) as fh:
                rows = json.load(fh)
            return [(date.fromisoformat(r["date"][:10]), r["open"], r["high"],
                     r["low"], r["close"]) for r in rows]
        except (OSError, ValueError, KeyError):
            pass

    out, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=CHUNK_DAYS), end)
        try:
            chunk = kite.historical_data(token, cur, stop, "day")
        except Exception as e:
            print(f"  fetch failed {cur} -> {stop}: {type(e).__name__}: {e}")
            return []
        out.extend(chunk)
        print(f"  {token}  {cur} -> {stop}   {len(chunk)} candles")
        cur = stop + timedelta(days=1)
        time.sleep(0.4)                 # Kite allows 3 req/s; stay well under

    seen, rows = set(), []
    for r in out:
        d = r["date"].date() if hasattr(r["date"], "date") else r["date"]
        if d in seen:
            continue
        seen.add(d)
        rows.append({"date": d.isoformat(), "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"]})
    rows.sort(key=lambda r: r["date"])
    try:
        with open(path, "w") as fh:
            json.dump(rows, fh)
    except OSError:
        pass
    return [(date.fromisoformat(r["date"]), r["open"], r["high"], r["low"],
             r["close"]) for r in rows]


# ------------------------------------------------------------------- fitting

# One definition of what a weekend is, shared with the pricing side. If these
# ever drifted apart, constants fitted on one classification would be applied
# under another and nothing would complain.
from vol_time import gap_class


def rms(xs):
    return sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def observations(nifty, vixmap):
    """One row per session: the three normalised moves and the gap class."""
    out = []
    for i in range(1, len(nifty)):
        d, o, _, _, c = nifty[i]
        pd_, _, _, _, pc = nifty[i - 1]
        vix = vixmap.get(pd_)
        if vix is None or vix <= 0 or pc <= 0:
            continue
        sig = pc * (vix / 100.0) * sqrt(1.0 / BASIS)
        if sig <= 0:
            continue
        out.append({
            "date": d, "vix": vix, "sigma_day": sig,
            "cls": gap_class(pd_, d),
            "gap": (o - pc) / sig,
            "session": (c - o) / sig,
            "total": (c - pc) / sig,
        })
    return out


def corr(gs, ss):
    """Correlation with no mean removed.

    The model assumes zero drift, so the second moments it needs are about
    zero rather than about the sample mean -- the same convention rms() uses
    two lines up. Subtracting a mean here would measure a different quantity
    from the one the variance decomposition consumes.
    """
    if not gs or len(gs) != len(ss):
        return None
    num = sum(g * s for g, s in zip(gs, ss))
    den = sqrt(sum(g * g for g in gs)) * sqrt(sum(s * s for s in ss))
    return num / den if den else None


def fit(rows):
    if not rows:
        return None
    classes = sorted({r["cls"] for r in rows})

    # Everything per class. Pooling k_session across classes while keeping
    # k_gap split by class was comparing a weekend-inclusive number against
    # an overnight-only one, which manufactured an apparent correlation.
    by = {}
    for c in classes:
        sub = [r for r in rows if r["cls"] == c]
        g = [r["gap"] for r in sub]
        s_ = [r["session"] for r in sub]
        t = [r["total"] for r in sub]
        kg, ks, kt = rms(g), rms(s_), rms(t)
        rho = corr(g, s_)
        # What the decomposition predicts the total should be, given the
        # measured correlation. If this tracks kt, the three-term model is
        # complete; if it does not, something else is going on.
        # k_total^2 = k_gap^2 + k_session^2 + 2*rho*k_gap*k_session is an
        # IDENTITY once rho is the uncentred correlation, so comparing the
        # two proves only the algebra. The number worth printing is what you
        # get by assuming rho = 0, because that is the error the model
        # inherits if the third term is left out.
        quad = None
        if kg is not None and ks is not None:
            quad = sqrt(kg * kg + ks * ks)
        by[c] = {"k_gap": kg, "k_session": ks, "k_total": kt,
                 "rho": rho, "k_total_quadrature": quad, "n": len(sub)}

    return {
        "k_session": rms([r["session"] for r in rows]),
        "k_total": rms([r["total"] for r in rows]),
        "by_class": by,
        "k_gap": {c: {"k": by[c]["k_gap"], "n": by[c]["n"]} for c in classes},
        "k_total_by_class": {c: {"k": by[c]["k_total"], "n": by[c]["n"]}
                             for c in classes},
        "n": len(rows),
        "from": rows[0]["date"].isoformat(),
        "to": rows[-1]["date"].isoformat(),
    }


def normalise(f):
    """Rescale so one full calendar day comes back to exactly 1.0."""
    ks, kg = f["k_session"], f["k_gap"].get("overnight", {}).get("k")
    if not ks or not kg:
        return f
    day = sqrt(ks * ks + kg * kg)
    f = json.loads(json.dumps(f))
    f["k_session"] = ks / day
    for c in f["k_gap"]:
        if f["k_gap"][c]["k"]:
            f["k_gap"][c]["k"] /= day
    f["normalised_by"] = day
    return f


# -------------------------------------------------------------------- report

def show(label, f):
    if not f:
        print(f"{label:<10} no data")
        return
    gaps = "  ".join(f"{c} {v['k']:.3f} (n={v['n']})"
                     for c, v in sorted(f["k_gap"].items()) if v["k"])
    print(f"{label:<10}{f['n']:>7}  k_session {f['k_session']:.3f}   "
          f"k_total {f['k_total']:.3f}")
    print(f"{'':10}{'':7}  {gaps}")


def main():
    years, refresh, dry = 5, "--refresh" in sys.argv, "--dry" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--years":
            years = int(sys.argv[i + 1])

    end = date.today()
    start = end - timedelta(days=int(365.25 * max(years, max(SENSITIVITY))))

    print(f"CALIBRATING   {start} -> {end}\n")
    import session as kite_session
    try:
        kite = kite_session.get_kite()
    except Exception as e:
        print(f"kite session failed: {type(e).__name__}: {e}")
        return 1

    print("NIFTY 50")
    nifty = fetch(kite, NIFTY_TOKEN, start, end, refresh)
    print("INDIA VIX")
    vix = fetch(kite, VIX_TOKEN, start, end, refresh)
    if not nifty or not vix:
        print("\nno candles -- cannot fit")
        return 1
    vixmap = {d: c for d, _, _, _, c in vix}
    print(f"\n{len(nifty)} NIFTY sessions, {len(vix)} VIX sessions\n")

    rows = observations(nifty, vixmap)
    if not rows:
        print("no usable observations")
        return 1

    print("FITTED CONSTANTS   (multiples of the model's own one-day sigma)\n")
    print(f"{'window':<10}{'n':>7}")
    print("-" * 68)
    fits = {}
    for y in SENSITIVITY:
        cut = end - timedelta(days=int(365.25 * y))
        sub = [r for r in rows if r["date"] >= cut]
        fits[y] = fit(sub)
        show(f"{y}y", fits[y])
    print("-" * 68)

    base = fits.get(years) or fits[SENSITIVITY[0]]

    # --- consistency: session and gap should add in quadrature to the total
    ks = base["k_session"]
    kg = (base["k_gap"].get("overnight") or {}).get("k")
    print()
    print()
    print("THE THIRD TERM   session and gap are not independent\n")
    print("  'if rho=0' is what a two-term model would say. 'error' is how")
    print("  far that lands from what actually happened -- the cost of")
    print("  leaving the correlation out.\n")
    h3 = (f"  {'window':<8}{'class':<12}{'n':>6}{'k_gap':>8}{'k_sess':>8}"
          f"{'rho':>8}{'measured':>10}{'if rho=0':>10}{'error':>8}")
    print(h3)
    print("  " + "-" * (len(h3) - 2))
    for y in SENSITIVITY:
        f = fits[y]
        if not f:
            continue
        for c, v in sorted(f["by_class"].items()):
            if v["rho"] is None or v["k_total_quadrature"] is None:
                continue
            err = (v["k_total_quadrature"] / v["k_total"] - 1.0) * 100.0
            print(f"  {str(y) + 'y':<8}{c:<12}{v['n']:>6}{v['k_gap']:>8.3f}"
                  f"{v['k_session']:>8.3f}{v['rho']:>8.3f}"
                  f"{v['k_total']:>10.3f}{v['k_total_quadrature']:>10.3f}"
                  f"{err:>7.1f}%")
    print()
    rhos = [v["rho"] for y in SENSITIVITY if fits[y]
            for c, v in fits[y]["by_class"].items()
            if c == "overnight" and v["rho"] is not None]
    if rhos:
        print(f"  overnight rho across windows: "
              f"{', '.join(f'{r:+.3f}' for r in rhos)}")
        if max(rhos) - min(rhos) < 0.08:
            print("  Stable. A single correlation term is defensible.")
        else:
            print("  Drifting. The correlation is not a constant either.")
        if all(r < -0.03 for r in rhos):
            print("  Consistently negative -- gaps get partly retraced, so")
            print("  plain quadrature OVERSTATES multi-day sigma.")
        elif all(abs(r) < 0.05 for r in rhos):
            print("  Near zero -- quadrature is fine and the third term can")
            print("  be dropped.")

    # --- the level, which is the part that can cost money
    print()
    print("THE LEVEL, YEAR BY YEAR")
    print("  k_total is realised movement as a multiple of what VIX implied.")
    print("  Below 1.0 means the market moved less than it was priced for.\n")
    for y in SENSITIVITY:
        f = fits[y]
        if f:
            print(f"  last {y}y   k_total {f['k_total']:.3f}   (n={f['n']})")
    vals = [fits[y]["k_total"] for y in SENSITIVITY if fits[y]]
    if len(vals) > 1:
        spread = max(vals) - min(vals)
        print()
        if spread > 0.10:
            print(f"  These disagree by {spread:.3f}. A single level constant")
            print("  fitted across all of them will be wrong in every")
            print("  individual regime. Treat the level correction as")
            print("  provisional and watch it.")
        else:
            print(f"  Stable to within {spread:.3f} across windows.")

    # ---------------------------------------------------- the model block
    # This is what the runtime actually reads. Everything above is evidence.
    fs = fits.get(WINDOW_SESSION) or base
    fr = fits.get(WINDOW_RHO) or base
    fg = fits.get(WINDOW_GAP) or base
    model = {
        "k_session": (fs["by_class"].get("overnight") or {}).get("k_session")
                     or fs["k_session"],
        "k_session_window_years": WINDOW_SESSION,
        "rho": (fr["by_class"].get("overnight") or {}).get("rho"),
        "rho_window_years": WINDOW_RHO,
        "k_gap": {},
        "k_gap_window_years": WINDOW_GAP,
        "k_day": {},
        "session_minutes": SESSION_MINUTES,
        "basis": BASIS,
    }
    fb = fg["by_class"].get(FALLBACK_CLASS) or {}
    for c, v in fg["by_class"].items():
        thin = v["n"] < MIN_CLASS_N
        src = fb if (thin and fb) else v
        model["k_gap"][c] = {
            "k": src.get("k_gap"), "n": v["n"],
            "fitted_on": FALLBACK_CLASS if src is fb else c,
        }
        # The directly measured whole-day figure, correlation already inside
        # it. Preferred over reassembling from three terms wherever a horizon
        # spans a complete day.
        model["k_day"][c] = {"k": src.get("k_total"), "n": v["n"],
                             "fitted_on": FALLBACK_CLASS if src is fb else c}
    print()
    print("MODEL BLOCK   what the runtime will read\n")
    print(f"  k_session  {model['k_session']:.4f}   "
          f"({WINDOW_SESSION}y window)")
    print(f"  rho        {model['rho']:+.4f}   ({WINDOW_RHO}y window)")
    for c, v in sorted(model["k_gap"].items()):
        note = "" if v["fitted_on"] == c else f"  <- borrowed from {v['fitted_on']}"
        print(f"  k_gap {c:<12} {v['k']:.4f}  (n={v['n']}, "
              f"{WINDOW_GAP}y){note}")
    for c, v in sorted(model["k_day"].items()):
        print(f"  k_day {c:<12} {v['k']:.4f}")

    out = {
        "fitted_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "window_years": years,
        "normalised_to_vix": NORMALISE_TO_VIX,
        "session_minutes": SESSION_MINUTES,
        "basis": BASIS,
        "fit": normalise(base) if NORMALISE_TO_VIX else base,
        "sensitivity": {str(y): fits[y] for y in SENSITIVITY if fits[y]},
    }
    print()
    if dry:
        print("--dry: nothing written")
        print(json.dumps(out["fit"], indent=2))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {OUT}")
    print()
    print("Nothing uses these yet. The model reads this file only once the")
    print("two-term sigma is wired in, and even then it stays behind a flag.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

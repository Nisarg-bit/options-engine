"""/api/daytrade — the intraday paper journal, served.

WHY THIS RECOMPUTES RATHER THAN READING THE CSV

A journal row is a pure function of that session's parquet, so today's row is
computed live on every request instead of being read back from the file. The
page is therefore never stale and never depends on a timer having fired. The
CSV stays what it is -- the settled historical record -- and one run at 15:20
is all that is needed to persist each day.

WHY IT CACHES FOUR NUMBERS PER SESSION AND THROWS THE BOOK AWAY

The overnight decomposition needs every collected session. Holding thirteen
sessions of bid/ask books in memory is several hundred megabytes, on a 2 GB
box whose actual job is running the collector. From each past session this
only needs four numbers -- the first and last forward, implied sigma and
realised volatility -- so each session is loaded, reduced, and released. A
completed session's parquet never changes again, so that summary is cached
for the life of the process; only the live session is re-read, and then only
when its parts change.
"""

import project_paths
import glob
import os

from fastapi import APIRouter, HTTPException, Query

import daytrade as D
import intraday_richness as R
import intraday_vrp as V

router = APIRouter()

# Anchored absolutely, for the same reason series_api is: these modules
# default to a path relative to the working directory, which is right only by
# coincidence of how the service happens to be started.
_ROOT = project_paths.ROOT
V.BARS = os.path.join(_ROOT, "data", "bars_1m")
D.JOURNAL = os.path.join(_ROOT, "data", "journal", "intraday_trades.csv")

MAX_JOURNAL_ROWS = 120

_cache = {}


def _sig(day, underlying):
    """(path, mtime, size) per parquet part. Cheap: a stat, never a read."""
    pattern = os.path.join(V.BARS, f"date={day}", f"underlying={underlying}",
                           "*.parquet")
    out = []
    for p in sorted(glob.glob(pattern)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((p, st.st_mtime, st.st_size))
    return tuple(out)


def _stats(day, underlying):
    """Four numbers per session, book discarded. Cached on the parquet."""
    key = ("stats", underlying, str(day))
    sig = _sig(day, underlying)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]

    bm, exp, err = V.load(day, underlying)
    val = None
    if not err and bm:
        rich = R.study(day, bm, exp, D.ENTRY, D.EXIT)
        val = {
            "date": day, "expiry": exp,
            "complete": max(bm) >= D.EXIT,
            "first_fwd": V.forward(bm[min(bm)]),
            "last_fwd": V.forward(bm[max(bm)]),
            "implied": rich["implied"] if rich else None,
            "rv15": rich["rv15"] if rich else None,
        }
    _cache[key] = (sig, val)
    return val


def _today_row(day, underlying, history, rich_hist):
    """The live row. Loads one book, evaluates, releases it."""
    key = ("row", underlying, str(day), len(history))
    sig = _sig(day, underlying)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    bm, exp, err = V.load(day, underlying)
    row = None if err or not bm else D.evaluate(day, bm, exp, underlying,
                                                history, rich_hist)
    _cache[key] = (sig, row)
    return row


def _overnight(stats):
    """Gap decomposition from the cached per-session summaries."""
    done = [s for s in stats if s and s["complete"]]
    if len(done) < 3:
        return [], {"reason": "too few complete sessions for the gap study"}
    rows = []
    for prev, cur in zip(done, done[1:]):
        if None in (prev["last_fwd"], cur["first_fwd"],
                    cur["implied"], cur["rv15"]):
            continue
        gap = cur["first_fwd"] - prev["last_fwd"]
        intr, imp = cur["rv15"], cur["implied"]
        tot = (intr ** 2 + gap ** 2) ** 0.5      # variance adds
        rows.append({"date": str(cur["date"]),
                     "prev_close": round(prev["last_fwd"], 1),
                     "open": round(cur["first_fwd"], 1), "gap": round(gap, 1),
                     "intraday_rv": round(intr, 1), "implied": round(imp, 1),
                     "explained": round(tot / imp, 3)})
    if not rows:
        return [], {"reason": "no session pair could be measured"}
    g = [abs(r["gap"]) for r in rows]
    spot = R.med([r["prev_close"] for r in rows]) or 1.0
    return rows, {
        "n": len(rows),
        "median_abs_gap": round(R.med(g), 1),
        "gap_pct_of_spot": round(100 * R.med(g) / spot, 2),
        "median_intraday_rv": round(R.med([r["intraday_rv"] for r in rows]), 1),
        "median_implied": round(R.med([r["implied"] for r in rows]), 1),
        "median_explained": round(R.med([r["explained"] for r in rows]), 3),
        "note": D.EXPLAINED_NOTE,
    }


@router.get("/api/daytrade")
def api_daytrade(underlying: str = "NIFTY", compact: int = 0,
                 expiry: str = Query(None)):
    """Today's paper trade, the journal behind it, and what it does not claim."""
    days = V.sessions(underlying)
    if not days:
        raise HTTPException(404, "no sessions collected")

    stats = [_stats(d, underlying) for d in days]

    # Trailing realised feeds the richness score and must contain only
    # sessions BEFORE the one being scored. Built here in date order so the
    # live row is scored on exactly what was knowable at 09:20.
    history, rich_hist = [], []
    today = days[-1]
    for s in stats:
        if s is None or s["date"] >= today:
            continue
        if s["complete"] and s["rv15"] is not None:
            history.append(s["rv15"])
            _, sc = D.richness_score(s["implied"], history[:-1])
            if sc:
                rich_hist.append(sc)

    row = _today_row(today, underlying, history, rich_hist)
    if row is None:
        raise HTTPException(404, f"could not price {today}")

    journal = D.read_journal()
    # The live row supersedes whatever the CSV holds for the same date -- the
    # file may have been written at 11:00 and it is now 14:30.
    journal = [r for r in journal if r["date"] != row["date"]] + [row]
    journal.sort(key=lambda r: r["date"])
    journal = journal[-MAX_JOURNAL_ROWS:]

    on_rows, on_sum = _overnight(stats)

    # Distance to the stop, as a share of credit, for a position being watched.
    credit = float(row["credit"])
    mark = float(row["cover"])
    out = {
        "underlying": underlying, "session": str(today),
        "status": row["status"], "as_of": row["exit"],
        "today": {**row,
                  "mark": round(mark, 2),
                  "pnl_now_rs": round(float(row["pnl_pts"]) * V.DEFAULT_LOT, 0),
                  "to_stop_pct": round((float(row["stop_level"]) - mark) / credit, 4),
                  "stop_frac": D.STOP_FRAC},
        "summary": D.summary_data(journal),
        "overnight": on_sum,
        "notes": [
            "Paper only. Nothing in this project places an order.",
            "Entry 09:20 after the opening auction settles, exit 15:15 before "
            "the closing auction. Sold at the bid, covered at the ask, so the "
            "spread is measured rather than modelled.",
            "No profit target. With the stop at 30% of credit, a target breaks "
            "even at a win rate of 0.30/(T+0.30) — around 15% of credit at the "
            "observed 67%, where the median decay actually achieved is about "
            "5%. A reachable target cannot pay for the stop.",
            "The richness score is today's implied sigma over the trailing "
            "median of REALISED volatility, not over other prices — implied is "
            "dominated by days to expiry, so a price-against-price score would "
            "mostly report DTE in disguise.",
        ],
    }
    if not compact:
        out["journal"] = journal
        out["overnight_rows"] = on_rows
    else:
        # The whole journal, not the last 20: the summary above is computed on
        # every row, and a page shown fewer rows than the summary counted
        # printed "21 settled, -9,774" beside a table that summed to -7,226.
        # MAX_JOURNAL_ROWS already caps it; each row is a few hundred bytes.
        out["journal"] = journal
    out["journal_total"] = len(journal)
    return out

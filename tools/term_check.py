"""Render checks for page behaviour that needs no fixtures.

The page's own boot needs a dozen fixtured endpoints; the chart does not. So
every /api/ call is refused, the page is allowed to degrade, and then the one
read under test is replaced and the one function under test is called. What
this proves is exactly what it touches: loadTerm, lineChart's uneven-x path,
the table, and the caveat text.
"""
import functools, http.server, json, socketserver, threading
from math import sqrt

from playwright.sync_api import sync_playwright

import attribution as A
import decay_model as DM
import pricing as P

PORT = 8741
handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory="firebase/public")
socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(("127.0.0.1", PORT), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

SPOT, VIX = 24500.0, 11.50
RAW = [("2026-09-22", 2, 10.20), ("2026-09-29", 9, 10.95),
       ("2026-10-06", 16, 11.30), ("2026-10-27", 37, 11.80),
       ("2026-11-24", 65, 12.25)]
rows = []
for exp, dte, iv in RAW:
    sig = SPOT * (iv / 100) * (dte / 365) ** 0.5
    vsig = SPOT * (VIX / 100) * (dte / 365) ** 0.5
    rows.append({"expiry": exp, "dte": dte, "atm_strike": 24500,
                 "straddle": round(sig * 0.8, 2), "atm_iv": iv,
                 "sigma": round(sig, 2), "vix_sigma": round(vsig, 2),
                 "iv_minus_vix": round(iv - VIX, 3),
                 "sigma_ratio": round(sig / vsig, 4), "usable_strikes": 37})

PAYLOAD = {"underlying": "NIFTY", "session": "2026-09-18",
           "as_of": "2026-09-18T15:29:00", "spot": SPOT, "vix": VIX,
           "vix_source": "india_vix",
           "slope_iv_per_day": 0.0325,
           "shape": "upward -- near expiries cheaper than VIX implies",
           "note": "n/a", "rows": rows,
           "skipped": [{"expiry": "2026-12-29", "why": "only 3 usable strikes, needs 8"}]}

# Attribution columns for the render check.
#
# These are NOT the recorded ones: the components live in the journal CSV and
# the parquet behind it, neither of which this harness has. They are generated
# by attribution.attribute itself over model sessions, so the arithmetic on
# the page is exercised against real module output rather than against numbers
# typed here -- the totals reconcile because the module makes them reconcile,
# which is the property being checked. The magnitudes are illustrative.
LOT = 65
_F0, _SIG, _S_IN, _S_OUT = 23300.0, 120.0, 3.987, 3.040
_MOVES = [40.0, -25.0, 15.0, -8.0, 120.0, -60.0, 30.0, -300.0,
          20.0, -45.0, 90.0, -18.0, 55.0, -210.0]
_VOLS = [0.97, 1.02, 0.94, 0.99, 1.06, 0.95, 0.98, 1.11,
         0.96, 1.01, 0.93, 0.99, 0.97, 1.08]


def _model_session(move, volmult):
    legs = [(_F0, "CE"), (_F0, "PE")]
    mid_in = DM.structure_value(legs, _F0, _SIG * sqrt(_S_IN))
    mid_out = DM.structure_value(legs, _F0 + move,
                                 _SIG * sqrt(_S_OUT) * volmult)
    credit, cover = mid_in - 0.8, mid_out + 1.2
    fees = P.transaction_costs(credit, LOT, 1, defined_risk=False)
    return {"legs": legs, "fwd_open": _F0, "fwd_exit": _F0 + move,
            "sigma_session": _SIG, "s_in": _S_IN, "s_out": _S_OUT,
            "credit": credit, "cover": cover,
            "spread_at_entry": 1.6, "spread_at_exit": 2.4,
            "pnl_pts": credit - cover, "fees_rs": fees}


def _attr_columns(i):
    sim = _model_session(_MOVES[i % len(_MOVES)], _VOLS[i % len(_VOLS)])
    att = A.attribute(sim, LOT)
    return {**A.row_fields(att), "lot": LOT,
            "fees_rs": round(sim["fees_rs"]),
            "net_rs": round(att["net_rs"])}


# Recorded from /live/daytrade/NIFTY on 2026-09-20. The string-vs-number mix
# is not tidied up on purpose: daytrade.py writes "" for a field it could not
# compute and the CSV round-trip makes every settled row a string, while the
# live row stays numeric. Code that only ever sees a hand-written fixture of
# clean floats is code that has not met this payload.
RV = [("2026-08-31", "129.3", "107.9", "0.835"), ("2026-09-01", "106.7", "81.4", "0.763"),
      ("2026-09-02", "156.5", "78.0", "0.498"),  ("2026-09-03", "133.6", "72.3", "0.541"),
      ("2026-09-04", "145.6", "59.9", "0.412"),  ("2026-09-07", "127.4", "35.4", "0.278"),
      ("2026-09-08", "124.1", "65.5", "0.528"),  ("2026-09-09", "147.8", "108.7", "0.735"),
      ("2026-09-10", "142.4", "72.3", "0.508"),  ("2026-09-11", "152.1", "128.4", "0.844"),
      ("2026-09-15", "135.1", "103.1", "0.763"), ("2026-09-16", "179.5", "102.8", "0.573"),
      ("2026-09-17", "168.8", "135.0", "0.8"),   ("2026-09-18", 160.3, 55.1, 0.344)]

TODAY_ROW = {
    "atm": 23300, "cover": 189, "credit": 220.75, "date": "2026-09-18", "dte": 4,
    "entry": "09:20", "exit": "15:15", "exit_reason": "time", "expiry": "2026-09-22",
    "fees_rs": 133, "implied_sigma": 160.3, "mark": 189, "minutes": 356,
    "net_rs": 1931, "pnl_now_rs": 2064, "pnl_pts": 31.75, "ratio": 0.344,
    "realised_sigma": 55.1, "richness": 1.831, "richness_pct": 50,
    "status": "settled", "stop_frac": 0.3, "stop_level": 286.98,
    "structure": "atm_straddle", "to_stop_pct": 0.4439,
    "trailing_realised": 87.6, "underlying": "NIFTY",
}

DAYTRADE = {
    "underlying": "NIFTY", "session": "2026-09-18", "status": "settled",
    "as_of": "15:15", "today": TODAY_ROW,
    "journal": [dict(TODAY_ROW, date=d, implied_sigma=i, realised_sigma=r,
                     ratio=x, status="settled", **_attr_columns(n))
                for n, (d, i, r, x) in enumerate(RV)],
    "summary": {"floor": 40, "journalled": 14, "mean_net": -56, "median_net": 414,
                "median_ratio": 0.557, "ratio_below_one": 14, "ratio_n": 14,
                "remaining": 26, "settled": 14, "stops": 3, "total_net": -779,
                "wins": 8},
    "overnight": {"gap_pct_of_spot": 0.34, "median_abs_gap": 79.2,
                  "median_explained": 0.863, "median_implied": 145.6,
                  "median_intraday_rv": 78.0, "n": 13, "note": "recorded"},
    "notes": ["Paper only. Nothing in this project places an order."],
}

def _sig(day, struct, rec, pnl):
    return {"signal_date": day, "structure": struct, "expiry": "2026-09-22",
            "spot": 23346.4, "c_over_sigma": 0.63, "credit_pts": 175.85,
            "pop": 0.64, "net_ev_rs": 841, "final_level": 23360,
            "pnl_rs": pnl, "pnl_per_unit": pnl / 65.0,
            "inside_zone": "true", "recommended": rec}


JOURNAL = {
    "logged": 3, "scored": 2, "sample_floor": 20, "recommended_n": 2,
    "recommended_pop": 71.0, "recommended_hit": 60.0,
    "recommended_ev": 900, "recommended_real": -200,
    "calibration": [{"structure": "short_straddle", "n": 2, "pop": 71,
                     "hit": 60, "ev_unit": 1.2, "real_unit": -0.4}],
    "rows": [_sig("2026-09-16", "short_straddle", "true", 700),
             _sig("2026-09-17", "expiry_strangle", "false", -300),
             _sig("2026-09-17", "short_straddle", "true", 250),
             _sig("2026-09-18", "maxev_strangle", "true", 1931)],
}

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  - ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)

errors = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    pg = b.new_page(viewport={"width": 1500, "height": 1150},
                    accept_downloads=True)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/api/**", lambda r: r.fulfill(
        status=404, content_type="application/json", body='{"error":"refused"}'))
    pg.goto(f"http://127.0.0.1:{PORT}/index.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(1200)

    print("\nTERM STRUCTURE CHART\n")
    pg.evaluate("p => { DATA.term = async () => p; }", PAYLOAD)
    pg.evaluate("() => loadTerm()")
    pg.wait_for_timeout(600)

    n = pg.eval_on_selector_all("#termbox svg polyline", "e=>e.length")
    check("solved curve and flat VIX both drew", n == 2, f"{n} polylines")

    trows = pg.eval_on_selector_all("#termtbl tbody tr", "e=>e.length")
    check("a table row per priced expiry", trows == len(rows), f"{trows} rows")

    note = pg.inner_text("#termnote").lower()
    check("calendar-day caveat is attached", "calendar" in note and "weekend" in note)
    check("the shape is stated", "upward" in note)
    check("the unpriced expiry is named, not dropped silently",
          "2026-12-29" in note and "3 usable strikes" in note)

    # The point of the xs work: x position must track DTE, not index.
    pts = pg.eval_on_selector(
        "#termbox svg polyline",
        "e=>e.getAttribute('points').split(' ').map(s=>parseFloat(s.split(',')[0]))")
    dtes = [r["dte"] for r in rows]
    lo, hi = min(dtes), max(dtes)
    PL, PR, W = 58, 14, 560
    want = [PL + (d - lo) / (hi - lo) * (W - PL - PR) for d in dtes]
    ok = all(abs(a - b) < 0.6 for a, b in zip(pts, want))
    check("points sit at their real DTE, not at equal notches", ok,
          "got " + ", ".join(f"{x:.1f}" for x in pts)
          + " | want " + ", ".join(f"{x:.1f}" for x in want))
    even = [PL + i / (len(dtes) - 1) * (W - PL - PR) for i in range(len(dtes))]
    check("and that is visibly different from equal spacing",
          max(abs(a - b) for a, b in zip(want, even)) > 40,
          f"middle point {want[2]:.0f} vs {even[2]:.0f} on equal notches")

    # A chart that silently swallows a failed read is the bug being fixed.
    pg.evaluate("() => { DATA.term = async () => { throw new Error('no term structure published yet'); }; }")
    pg.evaluate("() => loadTerm()")
    pg.wait_for_timeout(400)
    empt = pg.inner_text("#termbox")
    check("a failed read says so in the box", "no term structure published yet" in empt,
          " ".join(empt.split())[:60])

    pg.evaluate("p => { DATA.term = async () => p; }", PAYLOAD)
    pg.evaluate("() => loadTerm()")
    pg.wait_for_timeout(600)
    print("\nREALISED AGAINST IMPLIED\n")
    pg.evaluate("p => { DATA.daytrade = async () => p; }", DAYTRADE)
    pg.click('.tabs button[data-t="intraday"]')
    pg.evaluate("() => loadIntraday()")
    pg.wait_for_timeout(900)

    n = pg.eval_on_selector_all("#rvbox svg polyline", "e=>e.length")
    check("implied and realised both drew", n == 2, f"{n} polylines")
    n = pg.eval_on_selector_all("#rvratio svg polyline", "e=>e.length")
    check("ratio drew against a 1.0 line and the median", n == 3, f"{n} polylines")

    st = pg.inner_text("#rvstats")
    check("quotes the journal's own median, not a second opinion",
          "0.557" in st, " ".join(st.split())[:70])
    check("counts the sessions under 1.0", "14 / 14" in st)
    # 0.278 (2026-09-07) is the quietest session in the recorded journal and
    # 0.844 (2026-09-11) the closest to its own price -- NOT 0.344, which is
    # merely the most recent row. This check was written wrong first time and
    # the run caught it, which is the only reason to write it at all.
    check("names the extremes of the series", "0.278" in st and "0.844" in st,
          "min and max of the ratio series, not the latest row")
    check("carries the overnight figure from the payload, not from memory",
          "0.863" in st, "median_explained")
    check("says a one-sided streak is not a distribution",
          "streak" in st.lower() and "distribution" in st.lower())

    # A journal of "" strings is what a fresh install actually has.
    pg.evaluate("p => { DATA.daytrade = async () => p; }",
                {**DAYTRADE, "journal": [dict(TODAY_ROW, implied_sigma="",
                                              realised_sigma="", ratio="")]})
    pg.evaluate("() => loadIntraday()")
    pg.wait_for_timeout(600)
    check("degrades to a message when nothing is priced",
          "needs two priced sessions" in pg.inner_text("#rvbox"),
          " ".join(pg.inner_text("#rvbox").split())[:50])

    pg.evaluate("p => { DATA.daytrade = async () => p; }", DAYTRADE)
    pg.evaluate("() => loadIntraday()")
    pg.wait_for_timeout(700)
    pg.locator("#rvbox").evaluate_handle("e=>e.closest('.grid')").as_element(
        ).screenshot(path="/home/claude/shot_rv.png")

    print("\nP&L ATTRIBUTION\n")
    n = pg.eval_on_selector_all("#attrbox svg polyline", "e=>e.length")
    check("net plus its five components drew", n == 6, f"{n} polylines")

    txt = pg.inner_text("#attrstats")
    for label in ("decay collected", "movement against it",
                  "implied vol against it", "spread", "statutory charges",
                  "net"):
        check(f"{label!r} is on the card", label in txt)
    check("says decay is a model number and the rest are measured",
          "model number" in txt and "measured" in txt)
    check("carries the sample floor while the sample is short",
          "floor of 40" in txt)
    check("states how closely it reconciles",
          "reconciles to credit minus cover" in txt)

    # The components must ADD to the net line, or the chart is four
    # unrelated series drawn on one axis.
    ends = pg.eval_on_selector_all(
        "#attrbox svg polyline",
        "e=>e.map(p=>{const q=p.getAttribute('points').split(' ');"
        "return parseFloat(q[q.length-1].split(',')[1]);})")
    check("the net line is drawn first, so it reads as the total",
          len(ends) == 6)

    totals = pg.evaluate("""() => {
      const t = [...document.querySelectorAll('#attrstats .kv')]
        .map(e => parseFloat(e.lastElementChild.textContent.replace(/\u2212/g,'-').replace(/[^0-9.\-]/g,'')));
      return {parts: t.slice(0,5), net: t[5]};
    }""")
    ssum = sum(totals["parts"])
    # The tolerance is the rounding budget, not a fudge factor. Each session's
    # net_rs is stored rounded to whole rupees and the four component totals
    # are summed before being rounded once, so the two can differ by up to
    # half a rupee per session. Anything beyond that is a real disagreement.
    budget = len(DAYTRADE["journal"])
    check("the five totals add to the net, within per-session rounding",
          abs(ssum - totals["net"]) <= budget,
          f"{ssum:.0f} against {totals['net']:.0f}, budget {budget}")

    # A journal from before the attribution existed has no components.
    pg.evaluate("p => { DATA.daytrade = async () => p; }",
                {**DAYTRADE, "journal": [dict(TODAY_ROW, decay_pts="",
                                              direction_pts="", vol_pts="",
                                              spread_pts="", lot=65)]})
    pg.evaluate("() => loadIntraday()")
    pg.wait_for_timeout(600)
    check("says what to run when no session is attributed yet",
          "--backfill" in pg.inner_text("#attrbox"),
          " ".join(pg.inner_text("#attrbox").split())[:60])

    pg.evaluate("p => { DATA.daytrade = async () => p; }", DAYTRADE)
    pg.evaluate("() => loadIntraday()")
    pg.wait_for_timeout(700)
    print("\nLOGGED SIGNALS - DAY FILTER\n")
    pg.evaluate("p => { DATA.journal = async () => p; }", JOURNAL)
    pg.click('.tabs button[data-t="journal"]')
    pg.evaluate("() => loadJournal()")
    pg.wait_for_timeout(800)

    chips = pg.eval_on_selector_all("#jdays [data-jday]", "e=>e.map(x=>x.dataset.jday)")
    check("one chip per distinct signal day, newest first",
          chips == ["2026-09-18", "2026-09-17", "2026-09-16"], str(chips))
    n = pg.eval_on_selector_all("#jrowtbl tbody tr", "e=>e.length")
    check("with nothing selected every row shows", n == 4, f"{n} rows")
    check("and it says so rather than leaving it to be guessed",
          "all 3 days" in pg.inner_text("#jdays"))

    pg.click('[data-jday="2026-09-17"]')
    pg.wait_for_timeout(300)
    n = pg.eval_on_selector_all("#jrowtbl tbody tr", "e=>e.length")
    check("selecting one day shows only that day", n == 2, f"{n} rows")
    check("the selected chip is marked",
          pg.eval_on_selector('[data-jday="2026-09-17"]',
                              "e=>e.classList.contains('on')"))

    pg.click('[data-jday="2026-09-18"]')
    pg.wait_for_timeout(300)
    n = pg.eval_on_selector_all("#jrowtbl tbody tr", "e=>e.length")
    check("selecting a second adds to it rather than replacing", n == 3, f"{n} rows")

    # The export has to follow the filter. An unfiltered file downloaded from
    # a filtered view is a mismatch nobody notices until it is elsewhere.
    with pg.expect_download() as dl:
        pg.click('[data-csv="signals"]')
    got = dl.value
    body = [ln for ln in open(got.path(), encoding="utf-8", newline="").read()
            .split("\r\n") if ln][1:]
    check("the csv carries exactly the filtered rows", len(body) == 3,
          f"{len(body)} rows, file {got.suggested_filename}")
    check("and the filename names the days it holds",
          "2026-09-17" in got.suggested_filename
          and "2026-09-18" in got.suggested_filename,
          got.suggested_filename)

    pg.click('[data-jday="*"]')
    pg.wait_for_timeout(300)
    n = pg.eval_on_selector_all("#jrowtbl tbody tr", "e=>e.length")
    check("clearing goes back to everything", n == 4, f"{n} rows")
    check("the chips all read unselected again",
          pg.eval_on_selector_all("#jdays .chip.on", "e=>e.length") == 0)

    print("\nDASHBOARD ORDER\n")
    pg.click('.tabs button[data-t="dash"]')
    pg.wait_for_timeout(400)
    # innerText is the RENDERED text and h2 is text-transform: uppercase, so
    # this compares case-insensitively. The first version did not and failed
    # against a card that was in exactly the right place.
    last = pg.eval_on_selector("#dash",
        "e=>e.lastElementChild.innerText.slice(0,60)").lower()
    check("the failure card sits at the foot of the dashboard",
          "weekly premium strategy was tested and it failed" in last, last)
    first = pg.eval_on_selector("#dash", "e=>e.firstElementChild.id")
    check("and the gate card is what the tab opens on", first == "gates", first)

    print("\nGATE SCOPE - THREE STATES, NOT TWO\n")
    BASE = {"underlying": "NIFTY", "session": "2026-09-18",
            "today_ist": "2026-09-18", "expiry": "2026-09-22", "dte": 4,
            "spot": 23346.4, "vix": 11.39, "sigma": 278.37, "lot": 65,
            "lot_source": "master", "straddle": 175.85, "fair": 222.1,
            "ratio": 0.792, "step": 50, "spot_source": "kite",
            "structures": [{"label": "MAX-EV STRANGLE", "name": "maxev",
                            "recommended": True, "pop": 0.64,
                            "net_ev_rs": 841, "margin": 120000, "roi": 0.007,
                            "ce_strike": 23500, "pe_strike": 23100,
                            "credit_pts": 175.85, "breakevens": [23000, 23600],
                            "max_loss_rs": None, "lots": 1}]}

    def dash_with(extra):
        pg.evaluate("p => { DATA.structures = async () => p; "
                    "DATA.analytics = async () => { throw new Error('none'); }; "
                    "DATA.scan = async () => ({rows: p.__scan || []}); }",
                    {**BASE, **extra})
        pg.click('.tabs button[data-t="dash"]')
        pg.evaluate("() => loadDash()")
        pg.wait_for_timeout(700)
        return pg.inner_text("#gates")

    txt = dash_with({"c_over_sigma": 0.6317, "cs_gate": 0.9346,
                     "gate_passed": False})
    check("a shut gate still says NO TRADE", "NO TRADE" in txt)
    check("and shows how far short it is", "short by" in txt.lower())

    txt = dash_with({"c_over_sigma": 0.6317, "cs_gate": None,
                     "gate_passed": None,
                     "gate_reason": "the 0.9346 threshold was fitted on NIFTY"})
    check("an uncalibrated underlying says NO GATE, not NO TRADE",
          "NO GATE" in txt and "NO TRADE" not in txt, " ".join(txt.split())[:80])
    check("and never renders null or NaN",
          "null" not in txt.lower() and "nan" not in txt.lower(),
          " ".join(txt.split())[:110])
    check("credit/sigma is still reported, it just has nothing to beat",
          "0.632" in txt)
    check("the reason is carried onto the card",
          "fitted on NIFTY" in txt)
    check("the Edge row reads n/a rather than fail",
          "no threshold calibrated" in txt)

    # Through the same helper: a structures payload that renders, with the
    # scanner rows attached. Feeding loadDash an error object instead made it
    # abort before the scan table and the check read an empty list -- the
    # harness testing its own stub rather than the page.
    dash_with({"c_over_sigma": 0.6317, "cs_gate": 0.9346, "gate_passed": False,
               "__scan": [
                   {"underlying": "NIFTY", "spot": 23346, "dte": 4,
                    "c_over_sigma": 0.63, "cs_gate": 0.9346,
                    "gate_passed": False, "best": "S", "net_ev_rs": 1,
                    "margin": 1, "ev_per_rupee": 0.01},
                   {"underlying": "SENSEX", "spot": 74295, "dte": 4,
                    "c_over_sigma": 0.71, "cs_gate": None,
                    "gate_passed": None, "best": "S", "net_ev_rs": 1,
                    "margin": 1, "ev_per_rupee": 0.01}]})
    # .pill is text-transform: uppercase, and innerText is the RENDERED text.
    # This is the second check in this file to compare case-sensitively
    # against a transformed element and fail on a page that was right --
    # lowercase anything read out of innerText.
    pills = pg.eval_on_selector_all("#scantbl tbody tr td:nth-child(5)",
                                    "e=>e.map(x=>x.innerText.trim().toLowerCase())")
    check("the scanner tells shut apart from never-asked",
          pills == ["shut", "n/a"], str(pills))

    print("\nCSV EXPORT\n")
    # The journal button lives on the Intraday tab and the block above left
    # the page on the Dashboard. Playwright will not click what is not
    # visible, which is correct of it and was wrong of this script.
    pg.click('.tabs button[data-t="intraday"]')
    pg.wait_for_timeout(400)
    nbtn = pg.eval_on_selector_all("[data-csv]", "e=>e.length")
    check("the tables worth taking away each offer one", nbtn >= 4,
          f"{nbtn} buttons")

    with pg.expect_download() as dl:
        pg.click('[data-csv="journal"]')
    got = dl.value
    # newline="" or Python's universal-newline translation turns every CRLF
    # into a bare LF before the split ever sees it, and the whole file reads
    # as one line. That is a property of open(), not of the export -- this
    # check was wrong first time for exactly that reason.
    text = open(got.path(), encoding="utf-8", newline="").read()
    check("lines end CRLF, as RFC 4180 asks", text.count("\r\n") > 1
          and "\n" not in text.replace("\r\n", ""))
    head, *body = [ln for ln in text.split("\r\n") if ln]
    cols = head.split(",")
    check("the filename names the underlying and the session",
          got.suggested_filename == "journal_NIFTY_2026-09-18.csv",
          got.suggested_filename)
    check("the attribution columns came with it",
          all(c in cols for c in ("lot", "decay_pts", "direction_pts",
                                  "vol_pts", "spread_pts", "unexplained_pts")),
          "union of keys, not a hardcoded list")
    check("one row per journalled session",
          len(body) == len(DAYTRADE["journal"]), f"{len(body)} rows")
    # The whole reason this is built from the payload and not the table: a
    # number rendered as 1,23,456 would arrive here as three columns.
    ragged = [n for n in (len(ln.split(",")) for ln in body) if n != len(cols)]
    check("every row has the header's column count", not ragged,
          f"{len(cols)} columns, ragged rows {ragged[:3]}")

    with pg.expect_download() as dl:
        pg.evaluate("""() => {
            exportable('probe', 'probe.csv', [{plain:'ok', comma:'a,b',
              quote:'say \"hi\"', line:'one\\ntwo', empty:null, flag:true}]);
            csvDownload('probe');
        }""")
    probe = open(dl.value.path(), encoding="utf-8").read()
    check("a comma inside a value is quoted", '"a,b"' in probe)
    check("a quote inside a value is doubled", '"say ""hi"""' in probe)
    check("a newline survives inside quotes", '"one\ntwo"' in probe)
    check("null exports as empty, not as the word null",
          "null" not in probe and ",," in probe)
    check("a boolean exports as true, not as [object]", ",true" in probe)

    pg.locator("#attrbox").scroll_into_view_if_needed()
    pg.locator("#attrbox").evaluate_handle("e=>e.closest('.grid')").as_element(
        ).screenshot(path="/home/claude/shot_attr.png")
    pg.click('.tabs button[data-t="dash"]')
    pg.wait_for_timeout(500)

    print("\nSTALE SESSION BANNER\n")
    pg.evaluate("() => paintStale({session:'2026-09-18', today_ist:'2026-09-20'})")
    vis = pg.eval_on_selector("#stale", "e=>getComputedStyle(e).display")
    txt = pg.inner_text("#stale")
    check("shown when the priced session is not today", vis != "none", vis)
    check("names both dates", "2026-09-18" in txt and "2026-09-20" in txt)
    check("says which date dte is counted from", "counted from" in txt.lower())

    pg.evaluate("() => paintStale({session:'2026-09-18', today_ist:'2026-09-18'})")
    check("hidden while the session IS today",
          pg.eval_on_selector("#stale", "e=>getComputedStyle(e).display") == "none")

    # A node published before today_ist existed must not produce a banner
    # built out of undefined -- during a deploy the page and the publisher
    # are briefly on different versions, in whichever order they land.
    pg.evaluate("() => paintStale({session:'2026-09-18'})")
    check("hidden for a payload that predates the field",
          pg.eval_on_selector("#stale", "e=>getComputedStyle(e).display") == "none")

    print("\nREPLAY\n")
    pg.evaluate("() => paintStale({session:'2026-09-03', today_ist:'2026-09-20', replay:true})")
    rep = pg.inner_text("#stale")
    check("a replay says which session it is replaying", "2026-09-03" in rep)
    check("and that the numbers come from that session's own bars",
          "own bars" in rep and "live quote" in rep)
    check("and names what the replay does NOT cover",
          "paper journal" in rep and "scanner" in rep)
    check("the replay message replaces the stale one rather than adding to it",
          "not today's" not in rep)

    # replay:true with session == today must still read as a replay, since a
    # session can be replayed on the day it happened.
    pg.evaluate("() => paintStale({session:'2026-09-20', today_ist:'2026-09-20', replay:true})")
    check("a same-day replay is still announced",
          pg.eval_on_selector("#stale", "e=>getComputedStyle(e).display") != "none")

    check("the header offers a session picker",
          pg.eval_on_selector_all("#sesssel", "e=>e.length") == 1)

    # The published route reads a replayed session out of /archive, which is
    # written in the same shape /live has. base() is the whole switch.
    pg.evaluate("""() => {
      SRC.mode = 'firebase';
      SRC.live = {marker:'live', expiries:{}};
      ARCH = {'2026-09-03': {marker:'archive', expiries:{}}};
      SESSION = null;
    }""")
    check("with no session picked the reads see the live tree",
          pg.evaluate("() => base().marker") == "live")
    pg.evaluate("() => { SESSION = '2026-09-03'; }")
    check("with one picked they see that session's archive",
          pg.evaluate("() => base().marker") == "archive")
    pg.evaluate("() => { SESSION = '2026-09-04'; }")
    check("a session not yet fetched falls back rather than showing nothing",
          pg.evaluate("() => base().marker") == "live")
    # The filter that broke the first real replay. An archived payload's
    # dte_from_today was computed the day the archive was WRITTEN, so
    # filtering a replayed session on it hides the expiry that session was
    # actually trading.
    REPLAYED = {"replay": True, "expiries": [
        {"expiry": "2026-09-15", "dte": 4, "dte_from_today": -5},
        {"expiry": "2026-09-22", "dte": 11, "dte_from_today": 2}]}
    LIVE_EXPS = {"replay": False, "expiries": [
        {"expiry": "2026-09-15", "dte": 0, "dte_from_today": -5},
        {"expiry": "2026-09-22", "dte": 4, "dte_from_today": 2}]}

    got = pg.evaluate("p => expiryRows(p).map(x=>x.expiry)", REPLAYED)
    check("a replay offers the expiry that session was trading",
          got == ["2026-09-15", "2026-09-22"], str(got))
    got = pg.evaluate("p => expiryRows(p).map(x=>x.expiry)", LIVE_EXPS)
    check("the live session still refuses a settled one",
          got == ["2026-09-22"], str(got))

    pg.evaluate("() => { SESSION = '2026-09-11'; }")
    msg = pg.evaluate("() => noData('structures', '2026-09-22').message")
    check("a replayed expiry with no archive says what to do",
          "one expiry per replayed session" in msg and "2026-09-22" in msg, msg)
    pg.evaluate("() => { SESSION = null; }")
    msg = pg.evaluate("() => noData('structures').message")
    check("and on the live route it keeps the plain message",
          msg == "no structures published yet", msg)

    pg.evaluate("() => { SESSION = null; ARCH = {}; }")

    pg.evaluate("() => { ARCHFAIL = '2026-09-04'; paintStale({session:'x'}); }")
    afail = pg.inner_text("#stale")
    check("an unarchived date says so instead of silently showing today",
          "2026-09-04" in afail and "not archived yet" in afail)
    pg.evaluate("() => { ARCHFAIL = null; }")

    # The picker builds from `archived`, not from `sessions`: the two differ
    # while the archive is catching up and the difference is exactly the set
    # of dates that would 404.
    pg.evaluate("""() => {
      SRC.mode = 'firebase';
      SRC.live = {health:{latest_session:'2026-09-18'},
                  sessions:{sessions:['2026-09-16','2026-09-17','2026-09-18'],
                            archived:['2026-09-16']},
                  expiries:{}, underlyings:{underlyings:[]}};
      document.querySelector('#sesssel').innerHTML = '';
      document.querySelector('#undsel').innerHTML = '';
    }""")
    pg.evaluate("() => pickers()")
    pg.wait_for_timeout(400)
    opts = pg.eval_on_selector_all("#sesssel option",
                                   "e=>e.map(o=>({v:o.value,t:o.textContent}))")
    check("the picker offers the archived sessions and the live one",
          [o["t"] for o in opts] == ["2026-09-18 · live", "2026-09-16"], str(opts))
    check("the live one carries an empty value, so no session is sent for it",
          opts and opts[0]["v"] == "")
    check("a date that exists but is not archived is not offered",
          all("2026-09-17" not in o["t"] for o in opts))
    check("and the picker is enabled once there is something to pick",
          pg.eval_on_selector("#sesssel", "e=>e.disabled") is False)

    pg.evaluate("() => paintStale({session:'2026-09-18', today_ist:'2026-09-20'})")
    pg.locator("#termbox").scroll_into_view_if_needed()
    card = pg.locator("#termbox").evaluate_handle("e=>e.closest('.grid')")
    card.as_element().screenshot(path="/home/claude/shot_term.png")
    b.close()
srv.shutdown()
print("\nuncaught page errors (the page boots against 404s, so some are expected):")
for e in errors[:6] or ["  (none)"]:
    print("  " + e)
print(f"\n{len(fails)} failed check(s)" if fails else "\nALL CHECKS PASSED")
raise SystemExit(1 if fails else 0)

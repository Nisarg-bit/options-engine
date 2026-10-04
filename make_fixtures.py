"""Rebuild fixtures.json for browser_check.py, from the real API.

WHY THIS EXISTS

fixtures.json held the payloads the browser harness runs against, and it
existed in exactly one place: the server. It was not in the working copy, not
in git, and not on the whitelist the nightly backup uses -- so the harness
could not be run anywhere else, and one `rm` would have taken the only copy
of the shapes every page assertion is written against.

The fix is not to copy the file somewhere safer. It is to stop the file being
a thing that can only exist: this rebuilds it from the same functions the API
serves, in process, the way publisher.py does. Run it wherever the parquet
lives and the fixtures come back.

The shapes are REAL. A hand-written fixture of clean floats is a fixture that
has never met daytrade.py writing "" for a field it could not compute, or the
CSV round trip turning every settled row into a string while the live row
stays numeric. Both of those are in here because they are in the data.

Usage:
    python make_fixtures.py              write fixtures.json
    python make_fixtures.py --check      verify the existing one covers
                                         every endpoint the page calls
"""

import json
import os
import re
import sys

# The API modules are imported inside build() on purpose. Importing them here
# would make --check need fastapi, pandas and a parquet tree just to answer
# "does this file cover every endpoint" -- a question the working copy has to
# be able to ask without any of that installed.

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "fixtures.json")

# The page is in a different place depending on where this runs: the working
# copy keeps it under firebase/public, and the server serves /home/ubuntu/web.
# Hard-coding the first one made the self-check crash on the box -- AFTER
# writing the fixtures perfectly well, which is the worst moment to raise.
PAGE_CANDIDATES = (
    os.path.join(ROOT, "firebase", "public", "index.html"),
    os.path.join(ROOT, "web", "index.html"),
)


def page_path():
    """Wherever index.html is, or None. None is an answer, not a failure:
    the fixtures are still checkable against FIXTURE_KEYS without it."""
    for p in PAGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

UNDERLYING = "NIFTY"

# What build() produces. Declared rather than inferred so a test can compare
# it against the endpoints the page calls without needing fastapi, pandas or
# a parquet tree -- and so the two lists failing to match is a test failure
# rather than a 404 nobody sees.
FIXTURE_KEYS = (
    "health", "underlyings", "sessions", "expiries", "scan", "journal",
    "chain", "structures", "analytics", "intraday", "term", "daytrade",
    "buildup", "series", "world",
)

# The three the publisher writes and the page offers on both routes. Keyed by
# the name browser_check resolves a /api/series request to, so the harness can
# answer a query-parameter combination out of a static file.
SERIES_VARIANTS = {
    "straddle_open": {"structure": "straddle", "anchor": "atm_open"},
    "strangle_open": {"structure": "strangle", "anchor": "atm_open", "width": 2},
    "straddle_roll": {"structure": "straddle", "anchor": "atm_rolling"},
}


def page_endpoints(path=None):
    """Every /api/<name> the page actually calls, read out of the page.

    Derived rather than listed. A hand-maintained list is how /api/intraday
    came to have no fixture at all while the harness reported a pass: the
    endpoint was added to the page and nobody remembered the other file.
    """
    path = path or page_path()
    if not path:
        return []
    src = open(path, encoding="utf-8").read()
    return sorted(set(re.findall(r"""get\(['"]/api/([a-z_]+)['"]""", src)))


def calls(underlying, exp):
    """{key: (function, kwargs)} -- the call table, not the results.

    Returned rather than executed so a test can bind every signature without
    a parquet tree behind it. A renamed parameter three modules away then
    fails in pytest, in a second, instead of at 15:20 on the day somebody
    next rebuilds the fixtures.
    """
    import api
    import daytrade_api
    import series_api
    import term_api

    return {
        "health": (api.health, {}),
        "underlyings": (api.api_underlyings, {}),
        "sessions": (api.api_sessions, {}),
        "expiries": (api.api_expiries, {"underlying": underlying}),
        "scan": (api.api_scan, {}),
        "journal": (api.api_journal, {"limit": 400}),
        "chain": (api.api_chain,
                  {"underlying": underlying, "expiry": exp, "width": 18}),
        "structures": (api.api_structures,
                       {"underlying": underlying, "expiry": exp, "lots": 1}),
        "analytics": (api.api_analytics,
                      {"underlying": underlying, "expiry": exp}),
        "intraday": (api.api_intraday,
                     {"underlying": underlying, "expiry": exp, "every": 2}),
        "term": (term_api.api_term, {"underlying": underlying, "limit": 6}),
        "daytrade": (daytrade_api.api_daytrade, {"underlying": underlying}),
        "buildup": (series_api.api_buildup,
                    {"underlying": underlying, "expiry": exp, "compact": 1}),
        # One key, three payloads: browser_check resolves a /api/series
        # request's query parameters to one of these names.
        "series": (series_api.api_series,
                   {"underlying": underlying, "expiry": exp,
                    "compact": 1, "every": 2}),
        # Network-backed (Yahoo, RSS); a fixture built offline records the
        # labelled failures, which is also what the page must survive.
        "world": (api.api_world, {}),
    }


def build(underlying=UNDERLYING):
    """One payload per endpoint, at the front expiry, from live data."""
    import api

    exps = api.api_expiries(underlying=underlying)
    rows = exps.get("expiries") or []
    if not rows:
        raise SystemExit(f"no expiries for {underlying} -- run this where the "
                         f"collector's parquet lives")
    # `dte_from_today` is the one that answers "still tradeable"; `dte` is the
    # horizon the prices were struck over and stays positive for an expiry
    # that settled since. The fallback keeps this working against an older
    # payload that predates the split.
    live = [e["expiry"] for e in rows
            if e.get("dte_from_today", e.get("dte", 0)) >= 0]
    exp = live[0] if live else rows[0]["expiry"]

    table = calls(underlying, exp)
    fn, base = table.pop("series")
    out = {k: f(**kw) for k, (f, kw) in table.items()}
    out["series"] = {key: fn(**base, **kw)
                     for key, kw in SERIES_VARIANTS.items()}
    # The declaration and the builder cannot be allowed to drift: a key here
    # and not in FIXTURE_KEYS would pass every test and still 404.
    assert set(out) == set(FIXTURE_KEYS), (
        f"build() and FIXTURE_KEYS disagree: "
        f"{set(out) ^ set(FIXTURE_KEYS)}")
    return out, exp


def check(path=None):
    """Does the fixture file answer everything the page asks for?"""
    path = path or PATH
    missing_file = not os.path.exists(path)
    if missing_file:
        print(f"FAIL: {path} does not exist")
        return 1
    fix = json.load(open(path, encoding="utf-8"))
    from_page = page_endpoints()
    if not from_page:
        print(f"note: no index.html beside this script, so the fixtures are "
              f"checked against FIXTURE_KEYS alone.")
        print(f"      looked in: {', '.join(PAGE_CANDIDATES)}")
    want = sorted(set(from_page) | set(FIXTURE_KEYS))
    missing = [k for k in want if k not in fix]
    thin = [k for k, v in fix.items()
            if isinstance(v, dict) and "error" in v]
    for k in want:
        mark = "ok  " if k in fix else "MISS"
        print(f"  {mark}  /api/{k}")
    lost_variants = []
    if "series" in fix:
        for v in SERIES_VARIANTS:
            here = v in (fix.get("series") or {})
            if not here:
                lost_variants.append(v)
            print(f"  {'ok  ' if here else 'MISS'}  series/{v}")
    if thin:
        print("\nfixtures that recorded an error rather than a payload:")
        for k in thin:
            print(f"  {k}: {fix[k]['error']}")
    if missing or lost_variants:
        # Printing MISS and then exiting 0 is the shape of a check nobody
        # trusts. Whatever is missing fails the run.
        if missing:
            print(f"\nFAIL: no fixture for {', '.join(missing)}")
        if lost_variants:
            print(f"FAIL: series has no {', '.join(lost_variants)}")
        return 1
    print(f"\nOK: {len(want)} endpoints covered, "
          f"{len(SERIES_VARIANTS)} series variants")
    return 0


def main():
    if "--check" in sys.argv:
        return check()
    fix, exp = build()
    blob = json.dumps(fix, default=str, indent=1, sort_keys=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, PATH)          # atomic, as write_journal is
    print(f"wrote {PATH}  {len(blob)/1024:.0f}KB  front expiry {exp}")
    print("the file is written -- anything below is the self-check\n")
    for k, v in sorted(fix.items()):
        n = len(json.dumps(v, default=str))
        flag = "  <-- recorded an error" if isinstance(v, dict) and "error" in v else ""
        print(f"  {k:12} {n/1024:7.1f}KB{flag}")
    return check()


if __name__ == "__main__":
    sys.exit(main())

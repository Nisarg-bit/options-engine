"""The browser harness's fixtures, and the three places that must agree.

fixtures.json existed only on the server: not in the working copy, not in
git, not on the backup whitelist. The harness could not run anywhere else and
one `rm` would have taken the only copy of the payload shapes every page
assertion is written against.

Copying it somewhere safer would not have fixed the interesting half. The
harness served a silent 404 for any endpoint with no fixture, and /api/intraday
sat in that state for as long as the harness existed -- the page's per-minute
chart was tested against an error and the run reported a pass. Nothing in the
repo related the page's list of endpoints to the fixture's list of keys.

These tests are that relation. They need no data and no API: the endpoints are
read out of the page, the keys out of the generator, the series variants out
of the publisher.

Run:  python -m pytest test_fixtures.py -q
"""

import project_paths
import json
import os
import re

import pytest

import make_fixtures as MF


ROOT = project_paths.ROOT


HAVE_PAGE = MF.page_path() is not None
needs_page = pytest.mark.skipif(
    not HAVE_PAGE, reason="no index.html beside the tests -- the server keeps "
                          "it at web/, the working copy at firebase/public/")


@needs_page
def test_the_page_calls_something():
    """A guard on the guard: if the regex stops matching, every test below
    would pass vacuously."""
    assert len(MF.page_endpoints()) >= 10


@needs_page
def test_every_endpoint_the_page_calls_has_a_fixture_key():
    missing = sorted(set(MF.page_endpoints()) - set(MF.FIXTURE_KEYS))
    assert not missing, f"the page calls /api/{missing} with no fixture"


def test_the_endpoints_that_have_already_bitten_are_covered():
    for name in ("intraday", "term", "daytrade"):
        assert name in MF.FIXTURE_KEYS
    if HAVE_PAGE:
        for name in ("intraday", "term", "daytrade"):
            assert name in MF.page_endpoints()


# ---------------------------------------------------- three lists, one truth

def _page_series_variants():
    src = open(MF.page_path(), encoding="utf-8").read()
    block = src.split("const SERIES_VARIANTS = {", 1)[1].split("};", 1)[0]
    # Exactly two spaces: the variant names sit at the top level of the
    # literal and their `q:` sub-objects are indented far further. A loose
    # \s* matched both and reported three phantom variants called "q".
    return sorted(re.findall(r"^  ([a-z_]+):", block, re.M))


def test_the_generator_and_the_publisher_agree_on_the_series_variants():
    """publisher.py decides what is on the Firebase route; this decides what
    the harness can answer. They are written out separately in both files and
    nothing has ever compared them."""
    import publisher
    assert sorted(MF.SERIES_VARIANTS) == sorted(publisher.SERIES_VARIANTS)


@needs_page
def test_the_page_offers_exactly_those_variants():
    """The page's own comment says it mirrors publisher.py and has to stay in
    step with it. This is the first thing that checks."""
    assert _page_series_variants() == sorted(MF.SERIES_VARIANTS)


def test_the_variant_parameters_match_the_publisher_not_just_the_names():
    import publisher
    for key, kw in MF.SERIES_VARIANTS.items():
        theirs = dict(publisher.SERIES_VARIANTS[key])
        assert kw == theirs, f"{key}: {kw} against {theirs}"


def test_every_call_the_generator_makes_binds_against_the_real_signature():
    """No data needed: bind the arguments, never call them.

    This is the check that would have caught a renamed parameter in api.py
    before the next person rebuilt the fixtures and found out at the point
    they most needed the file to exist.
    """
    import inspect
    table = MF.calls("NIFTY", "2026-09-22")
    assert set(table) == set(MF.FIXTURE_KEYS)
    for key, (fn, kw) in table.items():
        sig = inspect.signature(fn)
        try:
            sig.bind_partial(**kw)
        except TypeError as e:
            pytest.fail(f"{key}: {fn.__module__}.{fn.__name__}{sig} "
                        f"rejects {kw} -- {e}")


def test_the_series_variants_bind_too():
    import inspect
    fn, base = MF.calls("NIFTY", "2026-09-22")["series"]
    for key, kw in MF.SERIES_VARIANTS.items():
        try:
            inspect.signature(fn).bind_partial(**base, **kw)
        except TypeError as e:
            pytest.fail(f"series/{key}: {kw} -- {e}")


def test_build_assembles_the_whole_shape_without_touching_a_parquet(monkeypatch):
    """Every endpoint stubbed, so what is under test is the assembly: the keys
    it produces, the expiry it picks, and the three series variants being
    three calls rather than one."""
    import api
    seen = []

    def stub(name):
        def f(**kw):
            seen.append((name, kw))
            return {"stub": name, **kw}
        return f

    monkeypatch.setattr(api, "api_expiries", lambda **kw: {
        "expiries": [{"expiry": "2026-09-15", "dte": 0, "dte_from_today": -5},
                     {"expiry": "2026-09-22", "dte": 7, "dte_from_today": 2}]})
    table = {k: (stub(k), kw) for k, (_f, kw)
             in MF.calls("NIFTY", "2026-09-22").items()}
    monkeypatch.setattr(MF, "calls", lambda u, e: table)

    out, exp = MF.build("NIFTY")
    assert set(out) == set(MF.FIXTURE_KEYS)
    # The settled expiry is skipped: a fixture struck on a contract that no
    # longer exists is a fixture nobody can reason about.
    assert exp == "2026-09-22"
    assert sorted(out["series"]) == sorted(MF.SERIES_VARIANTS)
    assert sum(1 for n, _ in seen if n == "series") == 3


# ------------------------------------------------------- the file, if present

FIX = os.path.join(ROOT, "fixtures.json")


@pytest.mark.skipif(not os.path.exists(FIX),
                    reason="no fixtures.json here -- run make_fixtures.py "
                           "where the parquet lives")
def test_the_fixture_file_covers_every_endpoint():
    fix = json.load(open(FIX, encoding="utf-8"))
    want = set(MF.page_endpoints()) | set(MF.FIXTURE_KEYS)
    missing = sorted(want - set(fix))
    assert not missing, f"fixtures.json has no {missing}"


@pytest.mark.skipif(not os.path.exists(FIX), reason="no fixtures.json here")
def test_the_fixture_file_has_every_series_variant():
    fix = json.load(open(FIX, encoding="utf-8"))
    assert sorted(fix.get("series", {})) == sorted(MF.SERIES_VARIANTS)


@pytest.mark.skipif(not os.path.exists(FIX), reason="no fixtures.json here")
def test_no_fixture_recorded_an_error_instead_of_a_payload():
    """A fixture that captured {"error": ...} is worse than a missing one: the
    harness serves it with a 200 and the page renders the error as though the
    API had answered."""
    fix = json.load(open(FIX, encoding="utf-8"))
    bad = {k: v["error"] for k, v in fix.items()
           if isinstance(v, dict) and "error" in v}
    assert not bad, f"recorded errors: {bad}"

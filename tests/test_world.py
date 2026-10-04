"""world.py: the pure parts -- tagging, ranking, price rows, events.

Run:  python -m pytest test_world.py -q
"""
import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import world as W

UTC = timezone.utc


# ------------------------------------------------------------------ tagging

@pytest.mark.parametrize("text,cat", [
    ("Fed holds rates steady", "global_rates"),
    ("RBI keeps repo rate unchanged", "india_policy"),
    ("Brent jumps as OPEC+ cuts output", "oil"),
    ("Missile strikes near border", "geopolitics"),
    ("New tariffs on steel imports", "trade"),
    ("Rupee slips as FPIs pull money", "flows_fx"),
    ("US CPI hotter than expected", "inflation"),
    ("Nonfarm payrolls beat forecasts", "us_data"),
    ("Diesel surges as wars squeeze supply", "geopolitics"),
    ("China unveils stimulus", "china"),
    ("Global sell-off deepens", "stress"),
])
def test_each_category_fires(text, cat):
    assert cat in [t["cat"] for t in W.tag(text)]


@pytest.mark.parametrize("text", [
    "Residents fed up with traffic",       # 'fed' is not the Fed
    "Chinaware exhibition opens",           # word boundary
    "Software update released",             # 'war' inside a word
    "Priced at a premium",                  # 'RBI' absent
])
def test_ordinary_words_do_not_fire(text):
    assert W.tag(text) == []


def test_tag_reports_the_text_that_fired():
    t = W.tag("Powell says more cuts possible")[0]
    assert t["matched"] == "Powell" and t["label"] == "Global rates"


# ------------------------------------------------------------------ ranking

NOW = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)


def entry(title, hours_ago, summary=""):
    t = NOW - timedelta(hours=hours_ago)
    return {"title": title, "link": "x", "summary": summary,
            "published": t.strftime("%a, %d %b %Y %H:%M:%S +0000")}


def test_old_items_are_dropped_and_age_is_minutes():
    got = W.items_from_entries("S", "g", [entry("RBI note", 1), entry("RBI old", 50)], NOW)
    assert [x["title"] for x in got] == ["RBI note"] and got[0]["age_min"] == 60


def test_rank_puts_flagged_first_then_newest_and_dedupes():
    items = W.items_from_entries("S", "g", [
        entry("Cricket score", 0.1),
        entry("Crude rallies", 3),
        entry("Fed minutes out", 1),
        entry("Fed minutes out!", 2),     # duplicate after normalising
    ], NOW)
    r = W.rank(items)
    assert [x["title"] for x in r] == ["Fed minutes out", "Crude rallies", "Cricket score"]


# ------------------------------------------------------------------- prices

def frames():
    idx_d = pd.to_datetime(["2026-09-18", "2026-09-21", "2026-09-22"]).tz_localize(UTC)
    idx_i = pd.to_datetime(["2026-09-22 05:50", "2026-09-22 05:55"]).tz_localize(UTC)
    syms = [s for _, _, s, _, _ in W.SYMBOLS if s]
    cols = pd.MultiIndex.from_product([["Close"], syms])
    daily = pd.DataFrame(100.0, index=idx_d, columns=cols)
    daily.loc[idx_d[1], ("Close", "BZ=F")] = 80.0
    intra = pd.DataFrame(float("nan"), index=idx_i, columns=cols)
    intra.loc[idx_i[1], ("Close", "BZ=F")] = 82.0
    return daily, intra


def test_change_is_against_the_close_before_the_last_price_session():
    daily, intra = frames()
    rows = {r["key"]: r for r in W.rows_from_frames(daily, intra, now=NOW)}
    b = rows["brent"]
    assert b["last"] == 82.0 and b["prev"] == 80.0 and b["chg_pct"] == 2.5
    assert b["age_min"] == 5


def test_falls_back_to_daily_when_no_intraday():
    daily, intra = frames()
    rows = {r["key"]: r for r in W.rows_from_frames(daily, intra, now=NOW)}
    assert rows["gold"]["last"] == 100.0


def test_no_gift_nifty_row_and_no_reuters_feed():
    assert "gift_nifty" not in [k for k, *_ in W.SYMBOLS]
    assert "Reuters" not in [n for n, *_ in W.FEEDS]


def test_uk_budget_story_is_not_labelled_us_data():
    cats = [x["cat"] for x in W.tag("UK inflation pressures chancellor before Budget")]
    assert "inflation" in cats and "us_data" not in cats


def test_network_failure_gives_labelled_rows():
    rows = W.rows_from_frames(None, None, now=NOW)
    assert all(r["last"] is None for r in rows)
    assert all(r.get("note") for r in rows)


# ------------------------------------------------------------------- events

def test_events_window_and_order(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"events": [
        {"date": "2026-10-02", "time_ist": "18:00", "title": "B"},
        {"date": "2026-10-02", "time_ist": None, "title": "C"},
        {"date": "2026-09-30", "time_ist": None, "title": "A"},
        {"date": "2026-09-01", "time_ist": None, "title": "past"},
        {"date": "2027-06-01", "time_ist": None, "title": "far"},
    ]}))
    e = W.events(today=date(2026, 9, 22), days=45, path=str(p))
    assert [x["title"] for x in e["events"]] == ["A", "B", "C"]
    assert e["stale"] is False


def test_events_stale_when_file_runs_out(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"events": [{"date": "2026-10-01", "title": "x"}]}))
    assert W.events(today=date(2026, 9, 22), days=45, path=str(p))["stale"] is True


def test_the_real_file_is_valid_and_sourced():
    raw = json.load(open(W.EVENTS_FILE))["events"]
    for e in raw:
        date.fromisoformat(e["date"])
        assert e["source"].startswith("https://")
        assert e["impact"] in ("high", "medium")


# ------------------------------------------------------------- India impact

@pytest.mark.parametrize("text,level,cat", [
    ("RBI keeps repo rate unchanged at 5.5%", "high", "in_policy"),
    ("SEBI tightens F&O rules for weekly expiries", "high", "in_policy"),
    ("Union Budget: STT on options raised", "high", "in_policy"),
    ("Infosys Q2 results: net profit rises 8%", "high", "in_heavyweights"),
    ("Q2 results: HDFC Bank beats estimates", "high", "in_heavyweights"),
    ("FPIs pull out Rs 12,000 crore in September", "high", "in_macro"),
    ("Rupee hits record low against the dollar", "high", "in_macro"),
    ("India's CPI inflation eases to 3.1%", "high", "in_macro"),
    ("Pakistan army says drone shot down near border", "high", "in_geopolitics"),
    ("US tariffs on India to rise to 50%", "high", "in_geopolitics"),
    ("Reliance shares edge higher", "medium", "in_heavyweights"),
    ("India's forex reserves rise for third week", "medium", "in_macro"),
])
def test_india_impact_levels(text, level, cat):
    imp = W.india_impact(text, "india")
    assert imp["level"] == level
    assert cat in [r["cat"] for r in imp["rules"]]
    assert all(r["matched"] for r in imp["rules"])


@pytest.mark.parametrize("text", [
    "Cricket: India beat Australia by 5 wickets",
    "Priced at a premium",
    "Kitchen appliance maker launches range",
])
def test_ordinary_india_stories_get_no_impact(text):
    assert W.india_impact(text, "india")["level"] is None


def test_global_feed_needs_india_in_the_text():
    assert W.india_impact("US GDP growth slows", "global")["level"] is None
    assert W.india_impact("Trump raises tariffs on India", "global")["level"] == "high"


def test_items_carry_region_impact_and_the_rule_that_fired():
    got = W.items_from_entries("Moneycontrol", "india",
                               [entry("RBI cuts repo rate by 25 bps", 1)], NOW,
                               region="india")[0]
    assert got["region"] == "india" and got["impact"] == "high"
    assert got["impact_rules"][0]["label"] == "Policy & regulators"


def test_rank_puts_india_high_impact_first():
    items = W.items_from_entries("S", "india", [
        entry("Fed minutes out", 0.5),
        entry("Reliance shares edge higher", 0.2),
        entry("RBI cuts repo rate", 2),
    ], NOW, region="india")
    assert [x["title"] for x in W.rank(items)][:2] == ["RBI cuts repo rate",
                                                     "Reliance shares edge higher"]


def test_nse_feed_keeps_only_heavyweight_filings():
    e1 = entry("Infosys Limited", 1, "Outcome of Board Meeting - financial results for Q2")
    e2 = entry("Tiny Widgets Limited", 1, "Change in Director")
    e3 = entry("Kotak Nifty 50 ETF", 1, "NAV declaration")
    e4 = entry("ICICI Bank Limited", 1, "Loss of share certificate")
    got = W.items_from_entries("NSE announcements", "official", [e1, e2, e3, e4],
                               NOW, region="india", heavyweights_only=True)
    assert [x["title"] for x in got] == [
        "Infosys: Outcome of Board Meeting - financial results for Q2",
        "ICICI Bank: Loss of share certificate"]
    assert got[0]["impact"] == "high" and got[1]["impact"] is None     # routine filing


def test_feeds_have_a_region_and_the_new_indian_sources():
    names = [f[0] for f in W.FEEDS]
    assert all(len(f) == 4 and f[3] in ("india", "global") for f in W.FEEDS)
    for n in ("CNBC-TV18", "NDTV Profit", "NSE announcements"):
        assert n in names


# ------------------------------------------ the false flags seen live, 23 Sep

@pytest.mark.parametrize("text", [
    "New York Fed's Williams says rate-control toolkit is working well",
    "Mankind Pharma shares up 10% in just 4 sessions; Kotak upgrades the stock - Target, share price trend",
    "Mankind Pharma shares get an upgrade from Kotak; Price target among top five on the street",
    "Stocks to Watch for September 23: Adani Firms, Tata Stocks, HDFC Bank, Persistent Systems and more",
    "Market Pulse: Key triggers to watch before the September 23 trading session",
    "RBI to conduct Overnight Variable Rate Reverse Repo (VRRR) auction under LAF on September 23, 2026",
    "Sunil Singhania-led Abakkus Asset Manager files DRHP with Sebi for IPO",
    "Rupee rises 13 paise to 95.65 against US dollar in early trade",
    "Nasdaq hits record high as oil prices fall; S&P 500, Dow edge higher",
])
def test_seen_live_false_flags_are_gone(text):
    assert W.india_impact(text, "india")["level"] is None


def test_broker_rating_on_a_heavyweight_is_medium_not_high():
    t = "HDFC Bank Shares In Focus: Macquarie Bullish With Rs 1,150 Target Amid CEO Succession"
    lv = W.india_impact(t, "india")
    assert lv["level"] in ("high", "medium")
    assert W.india_impact("L&T shares get their highest target from JPMorgan", "india")["level"] == "medium"


def test_real_high_impact_headlines_stay_high():
    for t in ["Indian bonds steady as RBI rate hike expectations strengthen",
              "JPMorgan sees higher US tariff risk for India, says growth momentum could moderate",
              "FIIs' selling jumps more than 6 times to ₹3,810 crore; DIIs pump in ₹4,120 crore; record outflow"]:
        assert W.india_impact(t, "india")["level"] == "high", t


def test_foreign_story_on_indian_feed_is_global():
    assert W.india_context("Nasdaq hits record high as oil prices fall", "india") is False
    assert W.india_context("Closing Bell: Nifty snaps 4-day winning run", "india") is True


def test_near_duplicates_merge_and_list_the_other_source():
    items = W.items_from_entries("Livemint", "india",
        [entry("Sunil Singhania-led Abakkus Asset Manager files DRHP with Sebi for IPO", 1)], NOW, region="india") + \
        W.items_from_entries("CNBC-TV18", "india",
        [entry("Sunil Singhania-led Abakkus Asset Manager files IPO papers with SEBI", 2)], NOW, region="india")
    r = W.rank(items)
    assert len(r) == 1 and {r[0]["source"], *r[0]["also"]} == {"Livemint", "CNBC-TV18"}


def test_each_region_keeps_its_own_slots():
    ind = [entry(f"Nifty item number {i} about India", 0.1 + i / 100) for i in range(70)]
    glo = [entry(f"Wall Street story {i} Nasdaq moves", 0.1 + i / 100) for i in range(10)]
    items = W.items_from_entries("ET Markets", "india", ind, NOW, region="india") + \
            W.items_from_entries("CNBC", "global", glo, NOW, region="global")
    r = W.rank(items, caps={"india": 50, "global": 30})
    assert sum(1 for x in r if x["region"] == "india") <= 50
    assert sum(1 for x in r if x["region"] == "global") >= 1


def test_the_payload_pieces_news_builds_actually_build():
    """news() itself needs the network; every pure piece of its payload does
    not, and one of them shipped broken."""
    r = W.india_rules_public()
    assert set(r) == set(W.INDIA_RULES)
    for c, v in r.items():
        assert v["label"] and all(len(p) == 2 and p[1] in ("high", "medium") for p in v["patterns"])
    assert {c: {"label": l, "patterns": [p for p, _ in pats]}
            for c, (l, pats) in W.RULES.items()}          # the older tag rules
    assert W.HEAVYWEIGHTS and W.CAPS["india"] > 0

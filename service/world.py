"""What is happening outside this chain that could move it.

Three things, each labelled for what it is and is not:

  prices   Global markets and macro drivers from Yahoo Finance (yfinance).
           Free, keyless, UNOFFICIAL: it can be delayed ~15 minutes on some
           symbols and can stop working without notice. Every row carries the
           time of its last bar, so a stale number says so. India VIX is not
           here -- the page already has it from Kite.

  news     Headlines from public RSS feeds, tagged by KEYWORD RULES. A tag
           means a word matched, nothing more: it is not a judgement that the
           story will move NIFTY, and the rule that fired is shipped with it
           so it can be checked. Feeds that fail are listed, not hidden.

  events   data/events.json, kept by hand, every entry with its source.
           Nothing is scraped.

Nothing here feeds a gate, a POP or an EV. It is context.

Usage:
    python world.py --probe      try every symbol and feed once, print what worked
"""

import project_paths
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = project_paths.ROOT
EVENTS_FILE = os.path.join(ROOT, "data", "events.json")
IST = timezone(timedelta(hours=5, minutes=30))

# ------------------------------------------------------------------ prices

# (key, label, yahoo symbol, group, unit). None symbol = no free source known;
# the row is shown as such rather than silently dropped.
SYMBOLS = [
    ("es",         "S&P 500 fut",       "ES=F",     "lead",  "pts"),
    ("nq",         "Nasdaq 100 fut",    "NQ=F",     "lead",  "pts"),
    ("n225",       "Nikkei 225",        "^N225",    "asia",  "pts"),
    ("hsi",        "Hang Seng",         "^HSI",     "asia",  "pts"),
    ("sse",        "Shanghai Comp",     "000001.SS", "asia", "pts"),
    ("kospi",      "Kospi",             "^KS11",    "asia",  "pts"),
    ("brent",      "Brent crude",       "BZ=F",     "macro", "$"),
    ("usdinr",     "USD/INR",           "USDINR=X", "macro", "₹"),
    ("us10y",      "US 10Y yield",      "^TNX",     "macro", "%"),
    ("dxy",        "Dollar index",      "DX-Y.NYB", "macro", ""),
    ("gold",       "Gold",              "GC=F",     "macro", "$"),
    ("us_vix",     "US VIX",            "^VIX",     "fear",  ""),
]


def _last_valid(series):
    s = series.dropna()
    if s.empty:
        return None, None
    return float(s.iloc[-1]), s.index[-1]


def rows_from_frames(daily, intra, now=None):
    """Price rows from two yfinance frames (multi-ticker, column level 1 = ticker).

    last   latest 5-minute close (pre/post included), else latest daily close
    prev   the daily close BEFORE the session the last price belongs to
    Pure: no network, so it is what the tests exercise.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for key, label, sym, group, unit in SYMBOLS:
        row = {"key": key, "label": label, "symbol": sym, "group": group,
               "unit": unit, "last": None, "prev": None, "chg_pct": None,
               "as_of": None, "age_min": None}
        if sym is None:
            row["note"] = "no free source configured"
            out.append(row)
            continue
        try:
            d = daily["Close"][sym] if daily is not None else None
            i = intra["Close"][sym] if intra is not None else None
        except KeyError:
            d = i = None
        last, ts = _last_valid(i) if i is not None else (None, None)
        if last is None and d is not None:
            last, ts = _last_valid(d)
        if last is None:
            row["note"] = "no data returned"
            out.append(row)
            continue
        prev = None
        if d is not None:
            dd = d.dropna()
            day_of_last = ts.date() if hasattr(ts, "date") else None
            before = dd[[x.date() < day_of_last for x in dd.index]] if day_of_last else dd
            if len(before):
                prev = float(before.iloc[-1])
        t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        row.update({
            "last": round(last, 4 if unit == "%" else 2),
            "prev": round(prev, 4) if prev else None,
            "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
            "as_of": t.astimezone(IST).isoformat(timespec="minutes"),
            "age_min": int((now - t).total_seconds() // 60),
        })
        out.append(row)
    return out


def prices():
    import yfinance as yf
    syms = [s for _, _, s, _, _ in SYMBOLS if s]
    kw = dict(group_by="column", auto_adjust=False, progress=False, threads=True)
    try:
        daily = yf.download(syms, period="10d", interval="1d", **kw)
    except Exception:
        daily = None
    try:
        intra = yf.download(syms, period="2d", interval="5m", prepost=True, **kw)
    except Exception:
        intra = None
    return {"source": "Yahoo Finance via yfinance (unofficial, may be delayed)",
            "at": datetime.now(IST).isoformat(timespec="seconds"),
            "rows": rows_from_frames(daily, intra)}


# -------------------------------------------------------------------- news

FEEDS = [
    # (name, group, url, region). region "india" means every item from the
    # feed is Indian context; "global" items only count as Indian if the text
    # itself says so (see india_context).
    ("Moneycontrol",      "india",    "https://www.moneycontrol.com/rss/marketreports.xml", "india"),
    ("ET Markets",        "india",    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "india"),
    ("Business Standard", "india",    "https://www.business-standard.com/rss/markets-106.rss", "india"),
    ("Livemint",          "india",    "https://www.livemint.com/rss/markets", "india"),
    ("CNBC-TV18",         "india",    "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml", "india"),
    ("NDTV Profit",       "india",    "https://feeds.feedburner.com/ndtvprofit-latest", "india"),
    ("CNBC",              "global",   "https://www.cnbc.com/id/100003114/device/rss/rss.html", "global"),
    ("BBC Business",      "global",   "https://feeds.bbci.co.uk/news/business/rss.xml", "global"),
    ("Al Jazeera",        "global",   "https://www.aljazeera.com/xml/rss/all.xml", "global"),
    ("RBI",               "official", "https://www.rbi.org.in/pressreleases_rss.xml", "india"),
    ("Federal Reserve",   "official", "https://www.federalreserve.gov/feeds/press_all.xml", "global"),
    ("SEBI",              "official", "https://www.sebi.gov.in/sebirss.xml", "india"),
    ("PIB",               "official", "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", "india"),
    # Official but noisy: thousands of filings a day. Kept only when the
    # headline names one of the heavyweights below -- see FILTERED_FEEDS.
    ("NSE announcements", "official", "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml", "india"),
]

# Feeds whose items are kept only when they name a HEAVYWEIGHT company by its
# exact listed name (see NSE_NAMES) -- the NSE feed is thousands of routine
# filings a day, and its <title> is just the company name.
FILTERED_FEEDS = {"NSE announcements"}

# The largest NIFTY 50 weights, HAND-KEPT. Check against NSE's monthly index
# factsheet now and then -- weights drift and constituents change. Each entry
# is a regex alternation of the names headlines actually use.
HEAVYWEIGHTS = [
    r"HDFC Bank", r"Reliance(?: Industries)?|RIL", r"ICICI Bank",
    r"Infosys", r"Bharti Airtel|Airtel", r"TCS|Tata Consultancy",
    r"Larsen(?: & | and )Toubro|L&T", r"ITC", r"Kotak Mahindra Bank|Kotak Bank",
    r"Axis Bank", r"State Bank of India|SBI", r"Bajaj Finance",
    r"Hindustan Unilever|HUL", r"Mahindra(?: & | and )Mahindra|M&M",
    r"Maruti(?: Suzuki)?", r"Sun Pharma(?:ceutical)?", r"HCL ?Tech(?:nologies)?",
    r"NTPC", r"Tata Motors", r"Titan",
]
# Exact listed names, for the NSE filings feed only.
NSE_NAMES = {
    "HDFC Bank Limited", "Reliance Industries Limited", "ICICI Bank Limited",
    "Infosys Limited", "Bharti Airtel Limited", "Tata Consultancy Services Limited",
    "Larsen & Toubro Limited", "ITC Limited", "Kotak Mahindra Bank Limited",
    "Axis Bank Limited", "State Bank of India", "Bajaj Finance Limited",
    "Hindustan Unilever Limited", "Mahindra & Mahindra Limited",
    "Maruti Suzuki India Limited", "Sun Pharmaceutical Industries Limited",
    "HCL Technologies Limited", "NTPC Limited", "Tata Motors Limited",
    "Titan Company Limited",
}
_HW = "|".join(f"(?:{h})" for h in HEAVYWEIGHTS)
# Events that move a heavyweight -- and, at NIFTY weights, the index. Broker
# ratings and targets are deliberately NOT here: they are MEDIUM at most.
# (?i:...) -- the events are case-insensitive ("Q2 Results", "Shares"), the
# company names are not ("ITC", "Titan" must stay capitalised).
_HW_EVENT = (r"(?i:Q[1-4](?: FY\d\d)? (?:results?|earnings|profit|net profit)|results|"
             r"quarterly (?:results|earnings)|net profit|profit (?:rises|falls|jumps|drops|"
             r"surges|slumps|beats|misses)|earnings (?:beat|miss)|guidance|merger|"
             r"acquisitions?|acquires|demerger|probe|penalty|raids?|default|"
             r"CEO (?:resigns|quits|appointed|succession)|block deal|stake sale)")
_HW_BROKER = (r"(?i:upgrades?|downgrades?|target|rating|bullish|bearish|buy call|sell call|"
              r"top picks?|initiates? coverage|wins?|bags?|deal|orders?|shares?|m-cap|"
              r"(?:7|52)-week high|record high)")

# A broker or group company that shares a heavyweight's name ("Kotak upgrades
# Mankind", "ICICI Securities explains", "SBI Life", "Kotak Nifty ETF"). Cut
# out of the text before any heavyweight rule looks at it.
_NOT_THE_COMPANY = re.compile(
    r"\b(?:Kotak|ICICI|HDFC|Axis|SBI|Bajaj|Tata|Mahindra|Reliance|Motilal|Nomura|"
    r"JPMorgan|Jefferies|Macquarie|CLSA|Morgan Stanley|Goldman|Citi|Nuvama|Emkay|"
    r"Chola(?:mandalam)?)(?:\s+(?:Securities|Institutional Equities|Mutual Fund|"
    r"Mahindra Mutual Fund|MF|AMC|Asset Management|Life(?: Insurance)?|General|"
    r"ERGO|Cards|Funds Management|Capital|Direct|Sec|Nifty[\w\s]*ETF|[\w\s]*ETF|"
    r"Hotels|Consumer|Housing|Finserv))\b"
    r"|\b(?:Kotak|JPMorgan|Jefferies|Macquarie|CLSA|Morgan Stanley|Goldman|Citi|"
    r"Nomura|Bernstein|UBS|BofA|Bank of America)\s+(?:upgrades?|downgrades?|"
    r"initiates|maintains|raises|cuts|sees|is (?:bullish|cautious|bearish))")

# Round-ups name a dozen companies and move nothing on their own.
_ROUNDUP = re.compile(
    r"\b(?:stocks? to watch|stocks? in focus|top stocks|market pulse|key triggers|"
    r"closing bell|opening bell|stock market (?:prediction|today)|nifty outlook|"
    r"market outlook|trade setup|buzzing stocks|what to expect)", re.I)

# A headline about another country's market or central bank, even when an
# Indian outlet ran it.
_FOREIGN = re.compile(
    r"\b(?:Fed|FOMC|Powell|Williams|ECB|BoJ|Bank of Japan|Bank of England|"
    r"Nasdaq|S&P ?500|Dow|Wall Street|US stock|Eurozone|Europe(?:an)? (?:stocks|shares)|"
    r"Nikkei|Hang Seng|Kospi|China's|Chinese stocks)\b")

# category -> (label, [(pattern, case_sensitive, level, needs_india_word)])
# needs_india_word: a generic phrase ("GDP growth", "rate hike", "elections")
# only counts when the HEADLINE itself names India or an Indian market/body.
INDIA_RULES = {
    "in_policy": ("Policy & regulators", [
        (r"RBI\b.{0,60}\b(?:policy|repo|rates?|MPC|CRR|SLR|hikes?|cuts?|governor|"
         r"decision|penalty|bans?|restrictions?)", True, "high", False),
        (r"repo rate", False, "high", False), (r"MPC", True, "high", False),
        (r"monetary policy", False, "high", True),
        (r"(?:SEBI|Sebi)\b.{0,80}\b(?:F&O|derivatives?|futures|options?|expir(?:y|ies)|"
         r"lot sizes?|margins?)", True, "high", False),
        (r"(?:F&O|derivatives?)\b.{0,60}\b(?:SEBI|Sebi)", True, "high", False),
        (r"Union Budget|Budget 20\d\d", False, "high", False),
        (r"GST Council", False, "high", False), (r"STT", True, "high", False),
        (r"securities transaction tax", False, "high", False),
        (r"capital gains tax|LTCG|STCG", False, "high", False),
        (r"(?:SEBI|Sebi)\b.{0,60}\b(?:orders?|bans?|penalt(?:y|ies)|settles?|circular|"
         r"rules?|framework|consultation|foreign investors?|FPIs?|ease|tighten)",
         True, "medium", False),
        (r"RBI\b.{0,60}\b(?:liquidity|bonds?|FX|forex|dollar|rupee|inflation|growth|"
         r"intervention)",
         True, "medium", False),
        (r"fiscal deficit", False, "medium", True),
        (r"disinvestment", False, "medium", False),
    ]),
    "in_macro": ("Macro & flows", [
        (r"retail inflation", False, "high", False),
        (r"India(?:'s)? (?:CPI|inflation|GDP|IIP|trade deficit)", False, "high", False),
        (r"GDP (?:growth|data|print)", False, "high", True),
        (r"rupee\b.{0,50}\b(?:record low|all-time low|lifetime low|hits? (?:a )?(?:new )?low|"
         r"biggest (?:fall|drop)|slumps?|plunges?)", False, "high", False),
        (r"(?:crude|oil)\b.{0,30}\b(?:surges?|spikes?|soars?|jumps?|above \$1\d\d)",
         False, "high", True),
        (r"(?:FIIs?|FPIs?)\b.{0,40}\b(?:sell|sold|selling|offload|outflows?|pull(?:ed)? out|"
         r"dump|exit|buy|bought|inflows?)", True, "medium", False),
        (r"WPI", True, "medium", False), (r"IIP|industrial output", False, "medium", True),
        (r"trade deficit", False, "medium", True),
        (r"current account deficit", False, "medium", True),
        (r"forex reserves", False, "medium", True), (r"PMI", True, "medium", True),
        (r"crude|oil|Hormuz|energy costs", False, "medium", True),
    ]),
    "in_heavyweights": ("Heavyweight stock", [
        (rf"(?:{_HW})\b.{{0,70}}\b(?:{_HW_EVENT})", True, "high", False),
        (rf"Q[1-4] (?:FY\d\d )?[Rr]esults?:?\s+(?:{_HW})", True, "high", False),
        (rf"(?:{_HW})\b.{{0,70}}\b(?:{_HW_BROKER})", True, "medium", False),
    ]),
    "in_geopolitics": ("Politics & geopolitics", [
        (r"India[- ]Pak(?:istan)?|Indo-Pak", False, "high", False),
        (r"Pakistan\b.{0,50}\b(?:attacks?|strikes?|border|military|army|ceasefire|drones?|"
         r"missiles?)", False, "high", False),
        (r"LoC|LAC", True, "high", False),
        (r"(?:India|Indian)\b.{0,50}\bChina\b.{0,50}\b(?:border|clash|standoff)",
         False, "high", False),
        (r"terror(?:ist)? attack", False, "high", True),
        (r"(?:Lok Sabha|assembly|general) (?:poll|election) results?|exit polls?",
         False, "high", False),
        (r"(?:US|American|Trump)\b.{0,30}\btariffs?\b.{0,40}\bIndia", False, "high", False),
        (r"(?:Lok Sabha|assembly|state) (?:polls?|elections?)", False, "medium", False),
        (r"India[- ](?:US|EU|UK) trade (?:deal|talks|pact)", False, "medium", False),
        (r"(?:sanctions?|tariffs?)\b.{0,40}\bIndia", False, "medium", False),
    ]),
}

_INDIA_COMPILED = {
    cat: (label, [(re.compile(rf"\b(?:{p})\b", 0 if cs else re.IGNORECASE), p, lvl, ctx)
                  for p, cs, lvl, ctx in pats])
    for cat, (label, pats) in INDIA_RULES.items()
}
_INDIA_CTX = re.compile(r"\b(?:India|India's|Indian|Nifty|NIFTY|Sensex|SENSEX|Dalal Street|"
                        r"RBI|SEBI|Sebi|NSE|BSE|rupee|INR|Modi|New Delhi|Mumbai)\b")
_HW_RX = re.compile(rf"\b(?:{_HW})\b")
LEVEL_RANK = {"high": 0, "medium": 1, None: 2}

# NSE filings that actually matter; everything else in that feed is routine.
_NSE_MATERIAL = re.compile(
    r"financial results|outcome of board meeting|dividend|buy ?back|merger|amalgamation|"
    r"acquisition|scheme of arrangement|demerger|resignation of (?:MD|CEO|managing director|"
    r"chief executive)|appointment of (?:MD|CEO|managing director|chief executive)|"
    r"penalty|order passed|credit rating", re.I)


def india_context(text, region):
    """Is this headline about India? A headline that names India (or an
    Indian market, regulator or the rupee) always is. Otherwise an Indian
    feed's headline is, UNLESS it is about another country's market or
    central bank -- Indian outlets run plenty of Fed and Nasdaq stories."""
    if _INDIA_CTX.search(text or ""):
        return True
    return region == "india" and not _FOREIGN.search(text or "")


def india_impact(text, region="india"):
    """{"level": "high"|"medium"|None, "rules": [{cat,label,level,matched}]}

    Run on the HEADLINE only. Per category, the strongest rule that fires is
    reported with the exact words that fired it. Round-ups get nothing; broker
    and group-company names are cut out before the heavyweight rules run."""
    text = text or ""
    if not india_context(text, region) or _ROUNDUP.search(text):
        return {"level": None, "rules": []}
    has_word = bool(_INDIA_CTX.search(text))
    clean = _NOT_THE_COMPANY.sub(" ", text)
    rules = []
    for cat, (label, pats) in _INDIA_COMPILED.items():
        src = clean if cat == "in_heavyweights" else text
        best = None
        for rx, _p, lvl, ctx in pats:
            if ctx and not has_word:
                continue
            m = rx.search(src)
            if m and (best is None or LEVEL_RANK[lvl] < LEVEL_RANK[best["level"]]):
                best = {"cat": cat, "label": label, "level": lvl,
                        "matched": m.group(0)[:80]}
                if lvl == "high":
                    break
        if best:
            rules.append(best)
    # Foreign flows: a routine day's net figure is MEDIUM; a big one is HIGH.
    # Rs 5,000 crore in a session is roughly the top decile of daily FPI flows.
    for r in rules:
        if r["cat"] == "in_macro" and r["level"] == "medium" and re.search(r"FIIs?|FPIs?", r["matched"]):
            m = _CRORE.search(text)
            amt = float(m.group(1).replace(",", "")) if m else 0.0
            if amt >= 5000 or re.search(r"record|heaviest|biggest", text, re.I):
                r["level"] = "high"
                r["matched"] = (r["matched"] + (f" ({m.group(0)})" if m else ""))[:80]
    level = min((r["level"] for r in rules), key=LEVEL_RANK.get, default=None)
    return {"level": level, "rules": rules}


_CRORE = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+(?:\.\d+)?)\s?(?:crore|cr)\b", re.I)


def india_rules_public():
    """The India rules as the page shows them. A function, and tested: it was
    a comprehension unpacking three values out of a four-value pattern, which
    took the whole headlines node down with
    "too many values to unpack (expected 3)" -- and nothing caught it because
    nothing called it."""
    return {c: {"label": l, "patterns": [[p, lvl] for p, _cs, lvl, _ctx in pats]}
            for c, (l, pats) in INDIA_RULES.items()}


def names_heavyweight(text):
    return bool(_HW_RX.search(_NOT_THE_COMPANY.sub(" ", text or "")))


# category -> (label, [(pattern, case_sensitive)]). Word-bounded. A pattern
# that is case-sensitive is one whose lower-case form is an ordinary word
# ("Fed" vs "fed up").
RULES = {
    "india_policy": ("India policy", [
        (r"RBI", True), (r"repo rate", False), (r"MPC", True),
        (r"monetary policy", False), (r"SEBI", True), (r"Union Budget", False),
        (r"GST", True), (r"fiscal deficit", False)]),
    "global_rates": ("Global rates", [
        (r"Fed", True), (r"FOMC", True), (r"Powell", True),
        (r"Treasury yields?", False), (r"ECB", True), (r"BoJ|Bank of Japan", True),
        (r"rate (?:hike|cut)s?", False)]),
    "oil": ("Oil", [
        (r"crude", False), (r"Brent", True), (r"OPEC\+?", True), (r"oil prices?", False)]),
    "geopolitics": ("Geopolitics", [
        (r"wars?", False), (r"missiles?", False), (r"airstrikes?", False),
        (r"sanctions?", False), (r"ceasefire", False), (r"invasion", False),
        (r"military", False), (r"border (?:clash|tension)s?", False)]),
    "trade": ("Trade", [
        (r"tariffs?", False), (r"trade war", False), (r"export (?:curbs?|ban)", False)]),
    "flows_fx": ("Flows / FX", [
        (r"rupee", False), (r"FIIs?|FPIs?", True), (r"outflows?", False),
        (r"dollar index", False)]),
    "inflation": ("Inflation", [
        (r"CPI", True), (r"inflation", False), (r"WPI", True)]),
    "us_data": ("US data", [
        (r"payrolls?", False), (r"jobs report", False), (r"US GDP", False),
        (r"jobless claims", False)]),
    "china": ("China", [
        (r"China", True), (r"Chinese", True), (r"yuan", False), (r"Beijing", True)]),
    "stress": ("Market stress", [
        (r"crash(?:es|ed)?", False), (r"sell-?off", False), (r"plunges?", False),
        (r"circuit breaker", False), (r"defaults?", False), (r"recession", False),
        (r"bank (?:run|failure|collapse)", False)]),
}

_COMPILED = {
    cat: (label, [(re.compile(rf"\b(?:{p})\b", 0 if cs else re.IGNORECASE), p)
                  for p, cs in pats])
    for cat, (label, pats) in RULES.items()
}


def tag(text):
    """[{cat, label, matched}] -- every category whose rule fires, with the
    exact text that fired it."""
    out = []
    for cat, (label, pats) in _COMPILED.items():
        for rx, _ in pats:
            m = rx.search(text or "")
            if m:
                out.append({"cat": cat, "label": label, "matched": m.group(0)})
                break
    return out


def _when(entry):
    for k in ("published", "updated"):
        v = entry.get(k)
        if v:
            try:
                t = parsedate_to_datetime(v)
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            return datetime(*v[:6], tzinfo=timezone.utc)
    return None


def _norm(title):
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def _nse_item(title, summary):
    """NSE filings: the <title> is just the company. Kept only for the exact
    listed name of a heavyweight, and rewritten as "Company: subject" so the
    headline says what was filed. Returns (headline, material?) or None."""
    name = title.strip()
    if name not in NSE_NAMES:
        return None
    subj = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", summary or "")).strip()
    head = f"{name.replace(' Limited', '')}: {subj[:140]}" if subj else name
    return head, bool(_NSE_MATERIAL.search(subj))


def items_from_entries(feed_name, group, entries, now, max_age_h=36,
                       region=None, heavyweights_only=False):
    region = region or ("india" if group == "india" else "global")
    out = []
    for e in entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        t = _when(e)
        if t and (now - t) > timedelta(hours=max_age_h):
            continue
        summary = (e.get("summary") or "")[:300]
        material = None
        if heavyweights_only:
            got = _nse_item(title, summary)
            if got is None:
                continue
            title, material = got
        # Topic tags may read the summary; the India flag reads the HEADLINE
        # only -- summaries mention crude, the Fed and inflation in passing,
        # and flagging on a passing mention is how 70 of 80 became MED.
        tags = tag(title + " " + summary)
        if material is None:
            imp = india_impact(title, region)
        elif material:
            imp = {"level": "high", "rules": [{"cat": "in_heavyweights",
                   "label": "Heavyweight filing", "level": "high",
                   "matched": _NSE_MATERIAL.search(title).group(0)}]}
        else:
            imp = {"level": None, "rules": []}      # routine filing
        out.append({"title": title, "link": e.get("link"), "source": feed_name,
                    "group": group,
                    "region": "india" if india_context(title, region) else "global",
                    "impact": imp["level"], "impact_rules": imp["rules"],
                    "at": t.astimezone(IST).isoformat(timespec="minutes") if t else None,
                    "age_min": int((now - t).total_seconds() // 60) if t else None,
                    "tags": tags})
    return out


_STOP = set("a an the of to in on for and as at by with from is are be its this that "
            "after amid over into up down says say".split())


def _tokens(title):
    return {w for w in _norm(title).split() if w not in _STOP and len(w) > 2}


def _same_story(a, b):
    """Two headlines about the same event: most of their words overlap."""
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.6


# Slots per region. Indian feeds alone return ~500 items a day; without a cap
# of its own the global list was pushed out entirely.
CAPS = {"india": 50, "global": 30}


def rank(items, limit=None, caps=None):
    """Merge near-duplicates (keeping the most flagged, then the earliest, and
    listing the other sources), then within each region: India impact (high,
    medium), flagged, newest. Each region keeps its own slots."""
    caps = caps or CAPS
    order = sorted(items, key=lambda x: (LEVEL_RANK.get(x.get("impact"), 2),
                                         0 if x["tags"] else 1,
                                         x["age_min"] if x["age_min"] is not None else 10**9))
    kept = []
    for it in order:
        toks = _tokens(it["title"])
        dup = next((k for k in kept if _norm(k["title"]) == _norm(it["title"])
                    or _same_story(k["_t"], toks)), None)
        if dup is not None:
            if it["source"] != dup["source"] and it["source"] not in dup["also"]:
                dup["also"].append(it["source"])
            continue
        kept.append({**it, "_t": toks, "also": []})
    out = []
    for reg, n in caps.items():
        out += [k for k in kept if k.get("region", "global") == reg][:n]
    for k in out:
        k.pop("_t", None)
    return out[:limit] if limit else out


def news(timeout=10):
    import feedparser
    import requests
    now = datetime.now(timezone.utc)
    items, status = [], []
    for name, group, url, region in FEEDS:
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 nifty-desk"})
            if r.status_code != 200:
                status.append({"source": name, "ok": False, "why": f"HTTP {r.status_code}"})
                continue
            f = feedparser.parse(r.content)
            got = items_from_entries(name, group, f.entries, now, region=region,
                                     heavyweights_only=name in FILTERED_FEEDS)
            items.extend(got)
            # Answered but nothing from the last 36 hours: a feed can freeze
            # and keep returning 200. Reported as its own state, not as OK.
            status.append({"source": name, "ok": True, "n": len(got),
                           "empty": len(got) == 0,
                           "total": len(f.entries)})
        except Exception as e:
            status.append({"source": name, "ok": False, "why": type(e).__name__})
    ranked = rank(items)
    return {"at": datetime.now(IST).isoformat(timespec="seconds"),
            "method": "keyword rules on headline + first 300 chars of summary; "
                      "a tag means a word matched, not that the story matters",
            "rules": {c: {"label": l, "patterns": [p for p, _ in pats]}
                      for c, (l, pats) in RULES.items()},
            "feeds": status,
            "flagged": sum(1 for x in ranked if x["tags"]),
            "india_high": sum(1 for x in ranked if x.get("impact") == "high"),
            "caps": CAPS,
            "india_medium": sum(1 for x in ranked if x.get("impact") == "medium"),
            "india_method": "India-impact keyword rules (high/medium), applied "
                            "only to Indian-context items; each flag carries the "
                            "rule and the words that fired it",
            "india_rules": india_rules_public(),
            "heavyweights": HEAVYWEIGHTS,
            "items": ranked}


# ------------------------------------------------------------------ events

def events(today=None, days=45, path=EVENTS_FILE):
    today = today or datetime.now(IST).date()
    try:
        with open(path) as f:
            raw = json.load(f)["events"]
    except (OSError, KeyError, ValueError) as e:
        return {"error": f"events.json unreadable: {type(e).__name__}", "events": []}
    horizon = today + timedelta(days=days)
    up = [e for e in raw if today <= date.fromisoformat(e["date"]) <= horizon]
    up.sort(key=lambda e: (e["date"], e.get("time_ist") or "99:99"))
    last = max((e["date"] for e in raw), default=None)
    return {"today": str(today), "horizon_days": days, "events": up,
            "last_entry": last,
            "stale": last is not None and date.fromisoformat(last) < horizon,
            "note": "Hand-kept file; every entry links its source."}


# --------------------------------------------------------------- api cache

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _cache[key] = (time.time(), v)
    return v


def world():
    """Everything at once, for the API route. Same cadences as the publisher."""
    return {"prices": cached("prices", 60, prices),
            "news": cached("news", 600, news),
            "events": cached("events", 3600, events)}


def probe():
    p = prices()
    print("PRICES")
    for r in p["rows"]:
        print(f"  {r['label']:<16} {str(r['symbol']):<10} last {r['last']}  "
              f"chg {r['chg_pct']}%  as_of {r['as_of']}  {r.get('note','')}")
    n = news()
    print("\nFEEDS")
    for s in n["feeds"]:
        state = ('EMPTY (0 recent of ' + str(s.get('total')) + ')' if s.get('empty')
                 else 'OK ' + str(s.get('n'))) if s['ok'] else 'FAIL ' + s['why']
        print(f"  {s['source']:<18} {state}")
    print(f"\n{len(n['items'])} headlines, {n['flagged']} flagged, "
          f"India high {n['india_high']} / medium {n['india_medium']}. Top 15:")
    for it in n["items"][:15]:
        why = "; ".join(f"{r['label']}={r['matched']!r}" for r in it.get("impact_rules", []))
        print(f"  [{it.get('impact') or '-':6}] {it['title'][:80]}  ({it['source']})  {why}")
    e = events()
    print(f"\nEVENTS next {e['horizon_days']}d: {len(e['events'])}  stale={e.get('stale')}")
    for x in e["events"]:
        print(f"  {x['date']} {x.get('time_ist') or '     '}  {x['title']}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        print(__doc__)

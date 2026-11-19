"""FII/FPI and DII flows from NSE, kept as a small daily history.

Two NSE publications, both free and both published after the close:

  1. Cash market: FII/FPI and DII gross buy, sell and net, in Rs crore
     (www.nseindia.com/api/fiidiiTradeReact, provisional, ~18:00 IST).
  2. F&O positioning: participant-wise open interest (contracts) for Client,
     DII, FII and Pro -- index futures long/short and index options
     call/put long/short (nsearchives fao_participant_oi_DDMMYYYY.csv).

History lives in data/fii_dii.json as {"days": {"YYYY-MM-DD": {...}}}. A
failed fetch never touches it. refresh() is cheap to call often: it tries
NSE at most once every RETRY_MIN minutes, only after 17:30 IST, and stops
for the day once both parts are in.

    python fii_dii.py          fetch now (respects the limits above)
    python fii_dii.py --force  fetch regardless
"""
import csv
import io
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import project_paths

IST = timezone(timedelta(hours=5, minutes=30))
OUT = os.path.join(project_paths.ROOT, "data", "fii_dii.json")
HOME = "https://www.nseindia.com/reports/fii-dii"
CASH_API = "https://www.nseindia.com/api/fiidiiTradeReact"
PART_OI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
AFTER = (17, 30)          # IST; NSE posts the day's numbers in the evening
RETRY_MIN = 30
KEEP_DAYS = 120
_last_try = {"at": 0.0}


# ------------------------------------------------------------------ parsing

def _f(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_cash(rows):
    """[{category, date, buyValue, sellValue, netValue}] -> (iso date, {fii, dii})."""
    out, day = {}, None
    for r in rows or []:
        cat = str(r.get("category", "")).upper()
        key = "fii" if cat.startswith("FII") else "dii" if cat.startswith("DII") else None
        if not key:
            continue
        try:
            day = datetime.strptime(r["date"].strip(), "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        out[key] = {"buy_cr": _f(r.get("buyValue")), "sell_cr": _f(r.get("sellValue")),
                    "net_cr": _f(r.get("netValue"))}
    return (day, out) if day and out else (None, {})


COLS = {"future index long": "fut_long", "future index short": "fut_short",
        "option index call long": "call_long", "option index put long": "put_long",
        "option index call short": "call_short", "option index put short": "put_short"}


def parse_participant_oi(text):
    """NSE's participant OI CSV -> {fii|dii|pro|client: {fut_long, ..., put_short}}.

    The first line is a title; the header row is the one starting 'Client Type'.
    Columns are matched by name, so a reordered file still parses."""
    lines = [l for l in text.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if l.lower().lstrip('"').startswith("client type")), None)
    if start is None:
        return {}
    rd = csv.reader(io.StringIO("\n".join(lines[start:])))
    head = [h.strip().lower() for h in next(rd)]
    idx = {COLS[h]: i for i, h in enumerate(head) if h in COLS}
    out = {}
    for row in rd:
        if not row:
            continue
        who = row[0].strip().lower()
        if who not in ("client", "dii", "fii", "pro"):
            continue
        out[who] = {k: _f(row[i]) for k, i in idx.items() if i < len(row)}
    return out


def positioning(p):
    """Readable summary of one participant: net futures, long share, net options."""
    if not p:
        return None
    fl, fs = p.get("fut_long") or 0, p.get("fut_short") or 0
    out = {"fut_net": fl - fs,
           "fut_long_pct": round(100 * fl / (fl + fs), 1) if fl + fs else None,
           "calls_net": (p.get("call_long") or 0) - (p.get("call_short") or 0),
           "puts_net": (p.get("put_long") or 0) - (p.get("put_short") or 0)}
    out.update(p)
    return out


# ------------------------------------------------------------------ storage

def load():
    try:
        return json.load(open(OUT)).get("days", {})
    except (OSError, ValueError):
        return {}


def save(days):
    keep = dict(sorted(days.items())[-KEEP_DAYS:])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump({"days": keep}, open(tmp, "w"), indent=0)
    os.replace(tmp, OUT)


def summary(days, n=20):
    """What the Pulse page shows: the latest day, the last n days, F&O change."""
    if not days:
        return {"available": False, "reason": "no FII/DII data fetched yet (NSE posts it after ~18:00 IST)"}
    ks = sorted(days)
    last = days[ks[-1]]
    hist = [{"date": k, "fii_net_cr": (days[k].get("cash") or {}).get("fii", {}).get("net_cr"),
             "dii_net_cr": (days[k].get("cash") or {}).get("dii", {}).get("net_cr")} for k in ks[-n:]]
    sum5 = lambda key: round(sum(h[key] or 0 for h in hist[-5:]), 0)
    fii_fo = positioning((last.get("oi") or {}).get("fii"))
    prev_oi = next((days[k].get("oi") for k in reversed(ks[:-1]) if days[k].get("oi")), None)
    if fii_fo and prev_oi and prev_oi.get("fii"):
        pf = positioning(prev_oi["fii"])
        fii_fo["fut_net_chg"] = fii_fo["fut_net"] - pf["fut_net"]
    return {"available": True, "date": ks[-1], "cash": last.get("cash"),
            "fii_5d_cr": sum5("fii_net_cr"), "dii_5d_cr": sum5("dii_net_cr"),
            "history": hist, "fii_fo": fii_fo,
            "dii_fo": positioning((last.get("oi") or {}).get("dii")),
            "oi_date": ks[-1] if last.get("oi") else None}


# ------------------------------------------------------------------ fetching

def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/csv,*/*",
                      "Accept-Language": "en-US,en;q=0.9", "Referer": HOME})
    s.get(HOME, timeout=(5, 10))          # sets the cookies the API insists on
    return s


def fetch(force=False, now=None):
    """Try NSE if it is time to; merge whatever arrives. Returns the day map."""
    now = now or datetime.now(IST)
    days = load()
    today = now.date().isoformat()
    have = days.get(today, {})
    if not force:
        if (now.hour, now.minute) < AFTER or now.weekday() >= 5:
            return days
        if have.get("cash") and have.get("oi"):
            return days
        if time.time() - _last_try["at"] < RETRY_MIN * 60:
            return days
    _last_try["at"] = time.time()
    try:
        s = _session()
    except Exception as e:  # noqa: BLE001 -- NSE down or blocking: keep history
        print(f"fii_dii: NSE unreachable ({type(e).__name__})")
        return days
    changed = False
    try:
        d, cash = parse_cash(s.get(CASH_API, timeout=(5, 10)).json())
        if d and cash:
            days.setdefault(d, {})["cash"] = cash
            changed = True
    except Exception as e:  # noqa: BLE001
        print(f"fii_dii: cash flows failed ({type(e).__name__}: {e})")
    try:
        r = s.get(PART_OI.format(d=now.date()), timeout=(5, 10))
        oi = parse_participant_oi(r.text) if r.status_code == 200 else {}
        if oi:
            days.setdefault(today, {})["oi"] = oi
            changed = True
    except Exception as e:  # noqa: BLE001
        print(f"fii_dii: participant OI failed ({type(e).__name__}: {e})")
    if changed:
        save(days)
    return days


_bg = {"thread": None}


def fetch_background():
    """Start fetch() in a daemon thread and return at once.

    NSE often holds connections from cloud servers open without answering, and
    a timeout does not always save a blocked read. The publisher must never wait
    on that, so it only ever calls this; a hung fetch costs one idle thread,
    and no second one starts while it is still alive."""
    t = _bg["thread"]
    if t is not None and t.is_alive():
        return False
    t = threading.Thread(target=_safe_fetch, name="fii_dii", daemon=True)
    _bg["thread"] = t
    t.start()
    return True


def _safe_fetch():
    try:
        fetch()
    except Exception as e:  # noqa: BLE001
        print(f"fii_dii: background fetch failed ({type(e).__name__}: {e})")


if __name__ == "__main__":
    d = fetch(force="--force" in sys.argv)
    print(json.dumps(summary(d), indent=1)[:1500])

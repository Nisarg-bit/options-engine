"""Stock -> sector map for the website's market pages (Pulse).

Built once a day from NSE's own published index constituent lists
(niftyindices.com). Two layers:

  1. Sector indices, in a fixed priority order. A stock in NIFTY PSU BANK is
     "PSU Bank" even though it is also in NIFTY BANK and NIFTY FINANCIAL
     SERVICES -- the narrowest true label wins.
  2. For everything else, the "Industry" column of the NIFTY 500 list, mapped
     to the same short names.

A stock found in neither gets "Others". The map is written to
data/sectors.json and reused all day; a failed download keeps the last good
file, so a website outage at niftyindices never blanks the sector view.

    python sectors.py          refresh now (skips if today's file exists)
    python sectors.py --force  refresh even if it does
"""
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import project_paths

IST = timezone(timedelta(hours=5, minutes=30))
OUT = os.path.join(project_paths.ROOT, "data", "sectors.json")
BASE = "https://niftyindices.com/IndexConstituent/"

# (file stem, sector label) -- order is priority, narrowest first.
SECTOR_INDICES = [
    ("ind_niftypsubanklist", "PSU Bank"),
    ("ind_niftybanklist", "Bank"),
    ("ind_nifty_privatebanklist", "Bank"),
    ("ind_niftyitlist", "IT"),
    ("ind_niftypharmalist", "Pharma"),
    ("ind_niftyautolist", "Auto"),
    ("ind_niftyfmcglist", "FMCG"),
    ("ind_niftymetallist", "Metal"),
    ("ind_niftyrealtylist", "Realty"),
    ("ind_niftyoilgaslist", "Oil & Gas"),
    ("ind_niftyindiadefence_list", "Defence"),
    ("ind_niftyconsumerdurableslist", "Consumer"),
    ("ind_niftymedialist", "Media"),
    ("ind_niftyhealthcarelist", "Healthcare"),
]
BROAD = "ind_nifty500list"

# NIFTY 500 "Industry" -> the same short labels.
INDUSTRY = {
    "Financial Services": "Financials",
    "Information Technology": "IT",
    "Healthcare": "Healthcare",
    "Automobile and Auto Components": "Auto",
    "Fast Moving Consumer Goods": "FMCG",
    "Metals & Mining": "Metal",
    "Realty": "Realty",
    "Oil Gas & Consumable Fuels": "Oil & Gas",
    "Consumer Durables": "Consumer",
    "Media Entertainment & Publication": "Media",
    "Capital Goods": "Cap goods",
    "Construction": "Infra",
    "Construction Materials": "Cement",
    "Power": "Power",
    "Chemicals": "Chem",
    "Telecommunication": "Telecom",
    "Consumer Services": "New-age",
    "Services": "Services",
    "Textiles": "Textiles",
    "Diversified": "Others",
}


def parse(text):
    """[(symbol, industry or None)] from one constituent CSV."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for r in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if r.get("Symbol"):
            out.append((r["Symbol"], r.get("Industry") or None))
    return out


def classify(lists):
    """{symbol: {"sector": ..., "industry": ...}} from {stem: csv text}.

    Pure: takes the downloaded texts, so it is tested without a network.
    """
    out = {}
    if BROAD in lists:
        for sym, ind in parse(lists[BROAD]):
            out[sym] = {"sector": INDUSTRY.get(ind or "", "Others"), "industry": ind}
    # Sector indices override, lowest priority first so the narrowest wins.
    for stem, label in reversed(SECTOR_INDICES):
        if stem in lists:
            for sym, _ in parse(lists[stem]):
                rec = out.setdefault(sym, {"industry": None})
                rec["sector"] = label
    return out


def sector_of(m, symbol):
    rec = m.get(symbol)
    return rec["sector"] if rec else "Others"


def _get(stem, timeout=10):
    req = urllib.request.Request(BASE + stem + ".csv",
                                 headers={"User-Agent": "Mozilla/5.0 options-engine"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    if "Symbol" not in text[:400]:
        raise ValueError(f"{stem}: not a constituent file")
    return text


def load():
    """The last good map ({} if none yet)."""
    try:
        return json.load(open(OUT)).get("map", {})
    except (OSError, ValueError):
        return {}


def refresh(force=False):
    today = str(datetime.now(IST).date())
    if not force and os.path.exists(OUT):
        try:
            if json.load(open(OUT)).get("date") == today:
                return load()
        except ValueError:
            pass
    lists, failed = {}, []
    for stem in [BROAD] + [s for s, _ in SECTOR_INDICES]:
        try:
            lists[stem] = _get(stem)
        except Exception as e:  # noqa: BLE001 -- one bad file must not stop the rest
            failed.append(f"{stem}: {type(e).__name__}")
    if BROAD not in lists:
        print("sectors: NIFTY 500 list unavailable -- keeping the last good map")
        return load()
    m = classify(lists)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump({"date": today, "failed": failed, "count": len(m), "map": m},
              open(tmp, "w"), indent=0)
    os.replace(tmp, OUT)
    print(f"sectors: {len(m)} symbols mapped" + (f", {len(failed)} lists failed" if failed else ""))
    return m


if __name__ == "__main__":
    refresh(force="--force" in sys.argv)

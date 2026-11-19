"""52-week high and low for the F&O stocks, from NSE's daily equity bhavcopy.

The collector keeps only a few sessions of minute bars, so a year of daily
highs and lows comes from NSE's public archive instead: one ~200 KB zip per
trading day (UDiFF CM format). Only the EQ-series rows of stocks that have
futures are kept, in one small Parquet file:

    data/hl52/daily.parquet   date, symbol, high, low, close

    python hl52.py --update            # fetch any missing day of the last year
    python hl52.py --update --days 30  # a shorter window

A day NSE has no file for (holiday, weekend) is remembered in
data/hl52/absent.json once it is more than three days old, so it is not asked
for again. Prices are NOT adjusted for splits or bonuses: right after a split a
stock can look far below its "52-week high" until the old prices age out.
"""
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, timedelta

import pandas as pd

import project_paths

DIR = os.path.join(project_paths.ROOT, "data", "hl52")
STORE = os.path.join(DIR, "daily.parquet")
ABSENT = os.path.join(DIR, "absent.json")
INSTR = os.path.join(project_paths.ROOT, "data", "instruments")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
WEEKS = 52


def _url(d):
    return ("https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def parse(csv_bytes, symbols=None):
    """EQ rows of one bhavcopy -> DataFrame(symbol, high, low, close)."""
    df = pd.read_csv(io.BytesIO(csv_bytes), usecols=["TckrSymb", "SctySrs", "HghPric", "LwPric", "ClsPric"])
    df = df[df["SctySrs"] == "EQ"]
    if symbols:
        df = df[df["TckrSymb"].isin(symbols)]
    return pd.DataFrame({"symbol": df["TckrSymb"].astype(str), "high": df["HghPric"].astype(float),
                         "low": df["LwPric"].astype(float), "close": df["ClsPric"].astype(float)})


def fetch(d, session=None, symbols=None):
    """One day's rows, or None when NSE has no file for that day."""
    import requests
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*", "Referer": "https://www.nseindia.com/all-reports"})
    r = s.get(_url(d), timeout=30)
    if r.status_code != 200 or len(r.content) < 1000:
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    out = parse(z.read(z.namelist()[0]), symbols)
    out.insert(0, "date", pd.Timestamp(d))
    return out


def fo_symbols(day=None):
    """Stocks with futures, from the newest instrument master (kind == 'stock')."""
    files = sorted(f for f in os.listdir(INSTR) if f.endswith(".json")) if os.path.isdir(INSTR) else []
    if day:
        files = [f for f in files if f[:10] <= str(day)]
    for f in reversed(files):
        try:
            rows = json.load(open(os.path.join(INSTR, f))).get("instruments", [])
        except (OSError, ValueError):
            continue
        syms = {r["tradingsymbol"] for r in rows if r.get("kind") == "stock"}
        if syms:
            return syms
    return set()


def load(path=STORE):
    try:
        return pd.read_parquet(path)
    except (OSError, ValueError):
        return pd.DataFrame(columns=["date", "symbol", "high", "low", "close"])


def update(days=372, today=None, fetcher=fetch, symbols=None, pause=0.4, path=STORE, absent_path=ABSENT):
    """Fetch every missing weekday of the last `days` days. Returns (added_days, missing_days)."""
    today = today or date.today()
    symbols = symbols if symbols is not None else fo_symbols()
    have = load(path)
    known = {d.date() for d in pd.to_datetime(have["date"]).unique()} if len(have) else set()
    try:
        absent = set(json.load(open(absent_path)))
    except (OSError, ValueError):
        absent = set()
    start = today - timedelta(days=days)
    want = [start + timedelta(days=i) for i in range(days + 1)]
    want = [d for d in want if d.weekday() < 5 and d not in known and str(d) not in absent]
    frames, added, missing = [], 0, 0
    session = None
    if want and fetcher is fetch:
        import requests
        session = requests.Session()
    for d in want:
        try:
            got = fetcher(d, session=session, symbols=symbols)
        except Exception as e:  # noqa: BLE001 -- one bad day must not stop the backfill
            print(f"  {d}: {type(e).__name__}: {e}", flush=True)
            got = None
        if got is None or got.empty:
            missing += 1
            if (today - d).days > 3:
                absent.add(str(d))
        else:
            frames.append(got)
            added += 1
        if pause and fetcher is fetch:
            time.sleep(pause)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if frames:
        out = pd.concat([have] + frames, ignore_index=True) if len(have) else pd.concat(frames, ignore_index=True)
        out["date"] = pd.to_datetime(out["date"])
        out = out[out["date"] >= pd.Timestamp(today - timedelta(days=days + 7))]
        out = out.drop_duplicates(["date", "symbol"], keep="last").sort_values(["date", "symbol"])
        tmp = path + ".tmp"
        out.reset_index(drop=True).to_parquet(tmp, index=False)
        os.replace(tmp, path)
    tmp = absent_path + ".tmp"
    json.dump(sorted(absent), open(tmp, "w"))
    os.replace(tmp, absent_path)
    return added, missing


_levels = {"key": None, "val": {}}


def levels(day, path=STORE):
    """{symbol: (high_52w, low_52w, sessions)} from completed sessions BEFORE `day`
    within the last 52 weeks. Cached on the file's mtime and the day."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    key = (mtime, str(day))
    if _levels["key"] == key:
        return _levels["val"]
    df = load(path)
    if df.empty:
        return {}
    d = pd.Timestamp(day)
    df = df[(df["date"] < d) & (df["date"] >= d - pd.Timedelta(weeks=WEEKS))]
    g = df.groupby("symbol")
    val = {s: (float(h), float(lo), int(n)) for s, h, lo, n in
           zip(g["high"].max().index, g["high"].max(), g["low"].min(), g["high"].size())}
    _levels.update(key=key, val=val)
    return val


if __name__ == "__main__":
    if "--update" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 372
        t0 = time.time()
        a, m = update(days=n)
        print(f"hl52: {a} days added, {m} without a file, {time.time() - t0:.0f}s -> {STORE}", flush=True)
    else:
        print(__doc__)

"""NSE F&O bhavcopy: download, cache, parse. UDiFF format (July 2024 onward)."""
import io, sys, zipfile, os, time
from datetime import date, timedelta
import requests
import pandas as pd

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CACHE = "data/bhavcopy"

KEEP = ["TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric",
        "LwPric", "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst",
        "ChngInOpnIntrst", "TtlTradgVol", "NewBrdLotQty"]


def _url(d):
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def fetch(d, session=None, quiet=False):
    """Raw dataframe for one date, cached on disk. None if unavailable."""
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, f"{d}.csv")
    if os.path.exists(cached):
        return pd.read_csv(cached)

    s = session or requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*",
                      "Referer": "https://www.nseindia.com/all-reports-derivatives"})
    try:
        r = s.get(_url(d), timeout=30)
        if r.status_code != 200 or len(r.content) < 1000:
            if not quiet:
                print(f"  {d}  no file ({r.status_code})")
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
        df.to_csv(cached, index=False)
        return df
    except Exception as e:
        if not quiet:
            print(f"  {d}  FAIL {type(e).__name__}")
        return None


def options(d, symbol="NIFTY"):
    """Just the option rows for one symbol, typed and trimmed."""
    df = fetch(d, quiet=True)
    if df is None:
        return None
    df = df[(df["TckrSymb"] == symbol) & (df["OptnTp"].isin(["CE", "PE"]))]
    if df.empty:
        return None
    df = df[KEEP].copy()
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    df["traded"] = df["TtlTradgVol"] > 0
    return df.reset_index(drop=True)


def download_range(start, end, pause=0.8):
    """Bulk download. Skips weekends and anything already cached."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*",
                      "Referer": "https://www.nseindia.com/all-reports-derivatives"})
    try:
        s.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass

    d, got, miss, cached = start, 0, 0, 0
    while d <= end:
        if d.weekday() < 5:
            if os.path.exists(os.path.join(CACHE, f"{d}.csv")):
                cached += 1
            else:
                df = fetch(d, session=s, quiet=True)
                if df is None:
                    miss += 1
                else:
                    got += 1
                    print(f"  {d}  {len(df):,} rows")
                time.sleep(pause)
        d += timedelta(days=1)
    print(f"\ndownloaded {got}, already cached {cached}, unavailable {miss}")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        download_range(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]))
    else:
        d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=2)
        df = options(d)
        if df is None:
            raise SystemExit(f"no NIFTY options for {d}")
        print(f"{d}: {len(df):,} NIFTY option rows, {df['traded'].sum():,} traded")
        print(f"spot: {df['UndrlygPric'].iloc[0]:,.2f}   lot: {df['NewBrdLotQty'].iloc[0]}")
        print(f"expiries: {sorted(df['XpryDt'].unique())[:6]}")
        print(df[df['traded']].head(5).to_string(index=False))
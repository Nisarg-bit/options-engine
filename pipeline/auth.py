"""Automated Kite login. Returns a fresh access_token with no human present."""
import os, time, requests, pyotp
from urllib.parse import urlparse, parse_qs, urljoin
from kiteconnect import KiteConnect
from dotenv import load_dotenv

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _totp(seed):
    t = pyotp.TOTP(seed)
    remaining = t.interval - (time.time() % t.interval)
    if remaining < 5:
        time.sleep(remaining + 1)
    return t.now()


def login():
    load_dotenv()
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id    = os.environ["KITE_USER_ID"]
    password   = os.environ["KITE_PASSWORD"]
    seed       = os.environ["KITE_TOTP_SEED"].strip()

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "X-Kite-Version": "3"})

    r = s.post("https://kite.zerodha.com/api/login",
               data={"user_id": user_id, "password": password}, timeout=15)
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(f"password step failed: {j}")
    request_id = j["data"]["request_id"]

    r = s.post("https://kite.zerodha.com/api/twofa",
               data={"user_id": user_id, "request_id": request_id,
                     "twofa_value": _totp(seed), "twofa_type": "totp",
                     "skip_session": ""}, timeout=15)
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(f"totp step failed: {j}")

    url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    request_token = None
    for _ in range(10):
        r = s.get(url, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location")
        if not loc:
            break
        q = parse_qs(urlparse(loc).query)
        if "request_token" in q:
            request_token = q["request_token"][0]
            break
        url = urljoin(url, loc)

    if not request_token:
        raise RuntimeError("no request_token found in redirect chain")

    kite = KiteConnect(api_key=api_key)
    return kite.generate_session(request_token, api_secret=api_secret)["access_token"]


if __name__ == "__main__":
    token = login()
    with open(".kite_token", "w") as f:
        f.write(token)

    kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kite.set_access_token(token)
    print("logged in as :", kite.profile()["user_name"])
    print("NIFTY spot   :", kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"])
    print("token saved to .kite_token")
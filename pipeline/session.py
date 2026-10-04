"""Returns a ready KiteConnect, reusing the cached token when it's still valid."""
import os
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import auth

TOKEN_FILE = ".kite_token"


def get_kite():
    load_dotenv()
    api_key = os.environ["KITE_API_KEY"]

    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
        if token:
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(token)
            try:
                kite.profile()
                return kite
            except Exception:
                print("cached token rejected, logging in again")

    token = auth.login()
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    kite.profile()
    return kite
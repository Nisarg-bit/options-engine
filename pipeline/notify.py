"""Outbound alerts and heartbeat. Must never be able to crash its caller."""
import os, json
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv()
HEARTBEAT = "data/heartbeat.json"


def send(text, silent=False):
    """Returns True/False. Never raises - a broken alert must not stop the collector."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[notify disabled]", text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_notification": silent},
            timeout=10)
        if not r.ok:
            print("notify rejected:", r.text[:200])
        return r.ok
    except Exception as e:
        print("notify failed:", e)
        return False


def heartbeat(**fields):
    try:
        os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
        fields["written_at"] = datetime.now(timezone.utc).isoformat()
        with open(HEARTBEAT, "w") as f:
            json.dump(fields, f, default=str)
    except Exception:
        pass


def read_heartbeat():
    try:
        with open(HEARTBEAT) as f:
            return json.load(f)
    except Exception:
        return None
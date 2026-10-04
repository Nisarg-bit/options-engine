"""Telegram command bot: /status /gaps /token /help. Long-polls; no inbound port."""
import os, sys, time, glob, json, shutil
from datetime import datetime, timezone, date
import requests
from dotenv import load_dotenv
import notify

load_dotenv()
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"


def cmd_status():
    lines = ["<b>STATUS</b>"]

    hb = notify.read_heartbeat()
    if hb:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(hb["written_at"])).total_seconds()
        lines.append(f"last flush : {hb.get('ts')} ({age:.0f}s ago)")
        lines.append(f"bars total : {hb.get('bars_total', 0):,}")
        lines.append(f"ticks      : {hb.get('ticks_total', 0):,}")
        lines.append(f"reconnects : {hb.get('reconnects', 0)}")
    else:
        lines.append("collector  : no heartbeat (not running?)")

    inst = sorted(glob.glob("data/instruments/*.json"))
    if inst:
        with open(inst[-1]) as f:
            d = json.load(f)
        lines.append(f"universe   : {d['count']:,} ({d['date']})")

    if os.path.exists(".kite_token"):
        age = (time.time() - os.path.getmtime(".kite_token")) / 3600
        lines.append(f"token age  : {age:.1f}h")

    files = glob.glob(f"data/bars_1m/date={date.today()}/**/*.parquet", recursive=True)
    lines.append(f"files today: {len(files)}")

    du = shutil.disk_usage(".")
    lines.append(f"disk free  : {du.free / 1e9:.1f} GB")
    return "\n".join(lines)


def cmd_gaps():
    try:
        import pyarrow.parquet as pq
        files = glob.glob(f"data/bars_1m/date={date.today()}/**/*.parquet", recursive=True)
        if not files:
            return "no bars written today"
        total = interp = 0
        for f in files:
            t = pq.read_table(f, columns=["is_interpolated"])
            total += t.num_rows
            interp += sum(1 for v in t.column("is_interpolated").to_pylist() if v)
        pct = 100 * interp / total if total else 0
        return (f"<b>GAPS</b>\nbars today   : {total:,}\n"
                f"interpolated : {interp:,} ({pct:.1f}%)")
    except Exception as e:
        return f"gaps failed: {e}"


def cmd_token(arg):
    arg = (arg or "").strip()
    if len(arg) < 20:
        return "usage: /token &lt;access_token&gt;"
    with open(".kite_token", "w") as f:
        f.write(arg)
    try:
        os.chmod(".kite_token", 0o600)
    except Exception:
        pass
    return "token written. restart the collector to pick it up."


HELP = ("<b>COMMANDS</b>\n"
        "/status - collector health, universe, disk\n"
        "/gaps   - interpolated bar count today\n"
        "/token  - paste an access token when auto-login fails\n"
        "/help   - this")


def handle(text):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else None
    if cmd == "/status":
        return cmd_status()
    if cmd == "/gaps":
        return cmd_gaps()
    if cmd == "/token":
        return cmd_token(arg)
    if cmd in ("/help", "/start"):
        return HELP
    return None


def main():
    print("bot polling. ctrl+c to stop.")
    notify.send("Bot online. /help for commands.")
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 30, "offset": offset},
                             timeout=40).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg or "text" not in msg:
                    continue
                if str(msg["chat"]["id"]) != CHAT:
                    continue
                reply = handle(msg["text"])
                if reply:
                    notify.send(reply)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)
    notify.send("Bot offline.")


if __name__ == "__main__":
    main()
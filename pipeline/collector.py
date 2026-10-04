"""Live tick collector: KiteTicker -> Aggregator -> BarWriter."""
import os, sys, time, glob, signal, threading, random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from kiteconnect import KiteTicker
from session import get_kite
from aggregator import Aggregator
from writer import BarWriter, load_instrument_map
import market
import notify

RUNNING = True


def _stop(*_):
    global RUNNING
    RUNNING = False
    print("\nstopping - will flush before exit")


def latest_instrument_file():
    files = sorted(glob.glob("data/instruments/*.json"))
    if not files:
        sys.exit("no instrument file - run: python instruments.py")
    return files[-1]


def run_live(dry_run=False):
    signal.signal(signal.SIGINT, _stop)

    path = latest_instrument_file()
    imap = load_instrument_map(path)
    tokens = list(imap)
    print(f"instruments : {len(tokens):,} from {path}")

    get_kite()                       # validates or refreshes the token
    load_dotenv()
    access_token = open(".kite_token").read().strip()

    agg = Aggregator(tokens)
    # flush_every=1: one Parquet part per MINUTE, not per five.
    #
    # The five-minute buffer was the whole of the site's lag. Ticks become a
    # bar on the minute boundary, but nothing downstream can see that bar
    # until the part is on disk -- so the chain, the structures and every
    # number built on them ran 0-5 minutes behind, and the publisher's
    # 30-second cycle had nothing new to send in between.
    #
    # The cost is file count, not bytes: about 375 small parts per session
    # per underlying instead of 75. api.read_parts caches each part by
    # (mtime, size) and re-reads only what changed, so a longer list of
    # already-read files costs a stat() each, not a re-read.
    wr = BarWriter(imap, flush_every=1)
    lock = threading.Lock()
    stats = {"ticks": 0, "bars": 0, "reconnects": 0}

    kws = KiteTicker(os.environ["KITE_API_KEY"], access_token)

    def on_ticks(ws, ticks):
        stats["ticks"] += len(ticks)
        with lock:
            for t in ticks:
                agg.add_tick(t)

    def on_connect(ws, response):
        print(f"connected - subscribing to {len(tokens):,} instruments")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_reconnect(ws, attempt):
        stats["reconnects"] += 1
        print(f"reconnecting, attempt {attempt}")

    def on_error(ws, code, reason):
        print(f"error {code}: {reason}")

    def on_close(ws, code, reason):
        print(f"closed {code}: {reason}")

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_reconnect = on_reconnect
    kws.on_error = on_error
    kws.on_close = on_close

    kws.connect(threaded=True)

    idle_announced = False
    while RUNNING:
        if not market.in_session():
            if not idle_announced:
                print("outside session window - idling")
                idle_announced = True
            time.sleep(10)
            continue
        idle_announced = False

        nxt = market.next_minute()
        while RUNNING and datetime.now(timezone.utc) < nxt:
            time.sleep(0.2)
        if not RUNNING:
            break

        bar_ts = nxt - timedelta(minutes=1)
        with lock:
            bars = agg.flush(bar_ts)
        stats["bars"] += len(bars)
        interp = sum(1 for b in bars if b["is_interpolated"])
        if not dry_run:
            wr.add(bars)
        print(f"{bar_ts:%H:%M}Z  bars={len(bars):>5,}  interp={interp:>5,}  "
              f"ticks={stats['ticks']:>9,}")
        notify.heartbeat(ts=bar_ts, bars=len(bars), interpolated=interp,
                            ticks_total=stats["ticks"], bars_total=stats["bars"],
                            reconnects=stats["reconnects"])

    print("final flush")
    if not dry_run:
        wr.flush()
    try:
        kws.close()
    except Exception:
        pass
    print(f"done  ticks={stats['ticks']:,}  bars={stats['bars']:,}  "
          f"reconnects={stats['reconnects']}")


def run_replay(minutes=3, sample=50):
    """No network. Proves instruments -> aggregator -> writer end to end."""
    path = latest_instrument_file()
    full = load_instrument_map(path)
    tokens = list(full)[:sample]
    imap = {t: full[t] for t in tokens}

    agg = Aggregator(tokens)
    wr = BarWriter(imap, root="data_replay", flush_every=999)

    base = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    cum = {t: 0 for t in tokens}
    price = {t: 100.0 for t in tokens}

    for m in range(minutes):
        for _ in range(200):
            t = random.choice(tokens)
            price[t] = max(0.05, price[t] + random.uniform(-1, 1))
            cum[t] += random.randint(0, 75)
            p = round(price[t], 2)
            agg.add_tick({"instrument_token": t, "last_price": p,
                          "volume_traded": cum[t], "oi": 1000 + m,
                          "depth": {"buy": [{"price": p - 0.5, "quantity": 25}],
                                    "sell": [{"price": p + 0.5, "quantity": 25}]}})
        ts = base + timedelta(minutes=m)
        bars = agg.flush(ts)
        wr.add(bars)
        print(f"{ts:%H:%M}Z  bars={len(bars):>4}  "
              f"interp={sum(1 for b in bars if b['is_interpolated']):>4}")

    for p in wr.flush():
        print("written:", p)


if __name__ == "__main__":
    if "--replay" in sys.argv:
        run_replay()
    else:
        run_live(dry_run="--dry-run" in sys.argv)
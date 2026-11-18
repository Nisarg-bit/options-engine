"""Live tick collector: KiteTicker -> Aggregator -> BarWriter."""
import os, sys, time, glob, signal, threading, random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from session import get_kite
from aggregator import Aggregator
from writer import BarWriter, load_instrument_map
import market
import notify

RUNNING = True

# Kite refuses more than this many instruments on ONE websocket ("Can't
# subscribe to more than 4000 instruments") and allows up to 3 websockets per
# API key. The universe (5 NIFTY + 3 BANKNIFTY + 2 SENSEX expiries, plus the
# stock layer) is over 6,000, so it is split across connections. 3,500 per
# connection stays clear of the 4,000 Kite reports and needs only two
# connections for today's ~6,100, leaving the third spare.
SHARD_SIZE = 3500
MAX_CONNECTIONS = 3


def plan_shards(tokens, size=SHARD_SIZE, most=MAX_CONNECTIONS):
    """Split tokens into at most `most` lists of at most `size`. Anything past
    most*size is dropped and reported -- instruments.py keeps the universe
    under that cap, so this is a last line of defence, not the plan."""
    tokens = list(tokens)
    shards = [tokens[i:i + size] for i in range(0, len(tokens), size)]
    dropped = sum(len(x) for x in shards[most:])
    return shards[:most], dropped


def open_connections(api_key, access_token, shards, stk_set, on_ticks, log=print,
                     ticker_cls=None, reactor=None, on_reconnect=None, full_set=frozenset()):
    """One KiteTicker per shard, all feeding the same on_ticks.

    The first connect() starts Twisted's reactor in a background thread; the
    rest are handed to that thread with callFromThread, because Twisted is not
    thread-safe and a second connect() racing the first can try to start a
    second reactor."""
    if ticker_cls is None:
        from kiteconnect import KiteTicker as ticker_cls
    if reactor is None:
        from twisted.internet import reactor
    conns = []
    for n, shard in enumerate(shards, 1):
        kws = ticker_cls(api_key, access_token)
        # FULL for options AND stock futures: Kite only sends open interest in
        # full-mode packets, and futures OI is the whole point of the OI page
        # (12 Nov ran futures in QUOTE mode: every build-up count came back 0).
        # Cash shares have no OI, so they stay in the lighter QUOTE mode.
        opt = [t for t in shard if t not in stk_set or t in full_set]
        stk = [t for t in shard if t in stk_set and t not in full_set]

        def on_connect(ws, response, shard=shard, opt=opt, stk=stk, n=n):
            log(f"connection {n}/{len(shards)}: subscribing to {len(shard):,} instruments")
            ws.subscribe(shard)
            if opt:
                ws.set_mode(ws.MODE_FULL, opt)
            if stk:
                ws.set_mode(ws.MODE_QUOTE, stk)

        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        def _recon(ws, attempt, n=n):
            if on_reconnect:
                on_reconnect()
            log(f"connection {n}: reconnecting, attempt {attempt}")
        kws.on_reconnect = _recon
        kws.on_error = lambda ws, code, reason, n=n: log(f"connection {n}: error {code}: {reason}")
        kws.on_close = lambda ws, code, reason, n=n: log(f"connection {n}: closed {code}: {reason}")
        conns.append(kws)
    for i, kws in enumerate(conns):
        if i == 0:
            kws.connect(threaded=True)
            for _ in range(100):                     # up to 10 s for the reactor to come up
                if reactor.running:
                    break
                time.sleep(0.1)
        else:
            reactor.callFromThread(kws.connect, threaded=True)
    return conns


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
    # Options and indices keep their tier and FULL mode (depth = bid/ask).
    # Stocks go to their own tier in QUOTE mode: price, volume and OI is all
    # the market pages need, at a fraction of FULL's bandwidth.
    stk_map = {t: m for t, m in imap.items() if m.get("kind") in ("stock", "stock_fut", "index_fut")}
    opt_map = {t: m for t, m in imap.items() if t not in stk_map}
    opt_tokens, stk_tokens = list(opt_map), list(stk_map)
    print(f"instruments : {len(tokens):,} from {path} "
          f"({len(opt_tokens):,} options/indices, {len(stk_tokens):,} stocks)")

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
    wr = BarWriter(opt_map, flush_every=1)
    wr_stk = BarWriter(stk_map, tier="stocks_1m", flush_every=1)
    lock = threading.Lock()
    stats = {"ticks": 0, "bars": 0, "reconnects": 0}

    def on_ticks(ws, ticks):
        stats["ticks"] += len(ticks)
        with lock:
            for t in ticks:
                agg.add_tick(t)

    shards, dropped = plan_shards(tokens)
    print(f"websockets  : {len(shards)} connection(s) of up to {SHARD_SIZE:,}"
          + (f" -- WARNING {dropped:,} instruments over the cap, not subscribed" if dropped else ""))
    def bump():
        stats["reconnects"] += 1
    futs = {t for t, m in stk_map.items() if m.get("kind") in ("stock_fut", "index_fut")}
    conns = open_connections(os.environ["KITE_API_KEY"], access_token, shards,
                             set(stk_tokens), on_ticks, on_reconnect=bump, full_set=futs)

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
            wr.add(bars)          # each writer keeps only the tokens in its map
            try:
                wr_stk.add(bars)
            except Exception as e:  # noqa: BLE001 -- stocks must never stop the chains
                print(f"WARNING stock bars not written: {type(e).__name__}: {e}")
        print(f"{bar_ts:%H:%M}Z  bars={len(bars):>5,}  interp={interp:>5,}  "
              f"ticks={stats['ticks']:>9,}")
        notify.heartbeat(ts=bar_ts, bars=len(bars), interpolated=interp,
                            ticks_total=stats["ticks"], bars_total=stats["bars"],
                            reconnects=stats["reconnects"])

    print("final flush")
    if not dry_run:
        wr.flush()
        try:
            wr_stk.flush()
        except Exception as e:  # noqa: BLE001
            print(f"WARNING stock flush failed: {type(e).__name__}: {e}")
    for kws in conns:
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
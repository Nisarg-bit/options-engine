"""Bars -> partitioned Parquet. 1m is canonical; nothing else is written live."""
import os, json, glob
from datetime import date, datetime
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = "data"

SCHEMA = pa.schema([
    ("ts",              pa.timestamp("us", tz="UTC")),
    ("instrument_token", pa.int64()),
    ("tradingsymbol",   pa.string()),
    ("underlying",      pa.string()),
    ("expiry",          pa.date32()),
    ("strike",          pa.float64()),
    ("opt_type",        pa.string()),
    ("open",            pa.float64()),
    ("high",            pa.float64()),
    ("low",             pa.float64()),
    ("close",           pa.float64()),
    ("volume",          pa.int64()),
    ("pv",              pa.float64()),
    ("oi",              pa.int64()),
    ("bid",             pa.float64()),
    ("ask",             pa.float64()),
    ("bid_qty",         pa.int64()),
    ("ask_qty",         pa.int64()),
    ("tick_count",      pa.int32()),
    ("is_interpolated", pa.bool_()),
])


def load_instrument_map(path):
    """token -> metadata, from an instruments.py output file."""
    with open(path) as f:
        data = json.load(f)
    m = {}
    for i in data["instruments"]:
        if i.get("kind") == "index":
            m[i["instrument_token"]] = {
                "tradingsymbol": i["tradingsymbol"], "underlying": "INDEX",
                "expiry": None, "strike": None, "opt_type": None}
        else:
            m[i["instrument_token"]] = {
                "tradingsymbol": i["tradingsymbol"], "underlying": i["name"],
                "expiry": date.fromisoformat(i["expiry"]),
                "strike": i["strike"], "opt_type": i["instrument_type"]}
    return m


class BarWriter:
    """Buffers enriched bars, flushes to date/underlying partitions."""

    def __init__(self, instrument_map, root=ROOT, tier="bars_1m", flush_every=5):
        # flush_every is how many distinct MINUTES are buffered before a part
        # is written. The live collector passes 1 -- see the note there; the
        # default is left at the historical 5 so replays and the tests keep
        # producing the files they were written against.
        self.imap = instrument_map
        self.root = root
        self.tier = tier
        self.flush_every = flush_every
        self.buf = []
        self.part = 0

    def add(self, bars):
        for b in bars:
            meta = self.imap.get(b["instrument_token"])
            if meta is None:
                continue
            self.buf.append({**b, **meta})
        if len({b["ts"] for b in self.buf}) >= self.flush_every:
            self.flush()

    def flush(self):
        if not self.buf:
            return []
        written = []
        by_key = {}
        for r in self.buf:
            by_key.setdefault((r["ts"].date(), r["underlying"]), []).append(r)

        # NEVER write over an existing part. Every process used to start at
        # part-00000, so a collector restarted mid-session wrote its minutes
        # straight over the morning's files -- one per minute, silently. That
        # is how 1, 23 and 25 Sep 2026 lost everything before 11:33, 12:36 and
        # 12:07 IST while the logs showed a healthy collector all morning.
        # Now each partition skips to its next free number, so a restart
        # carries on after what is already on disk and names still sort in
        # time order.
        top = self.part
        for (d, und), rows in by_key.items():
            outdir = os.path.join(self.root, self.tier, f"date={d}", f"underlying={und}")
            os.makedirs(outdir, exist_ok=True)
            n = self.part
            while os.path.exists(os.path.join(outdir, f"part-{n:05d}.parquet")):
                n += 1
            path = os.path.join(outdir, f"part-{n:05d}.parquet")
            cols = {f.name: [r.get(f.name) for r in rows] for f in SCHEMA}
            pq.write_table(pa.table(cols, schema=SCHEMA), path, compression="zstd")
            written.append(path)
            top = max(top, n)

        self.part = top + 1
        self.buf = []
        return written
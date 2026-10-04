"""Nightly job: compact 1m parts, then derive 5m / 15m / daily from them."""
import os, glob, sys
from datetime import date, timedelta
import pyarrow as pa
import pyarrow.parquet as pq
from aggregator import rollup
from writer import SCHEMA, ROOT

DERIVED = pa.schema([f for f in SCHEMA if f.name != "is_interpolated"]
                    + [pa.field("interpolated_bars", pa.int32())])

TIERS = {"bars_5m": 5, "bars_15m": 15, "bars_1d": None}


def _read_day(tier, d, und):
    files = sorted(glob.glob(os.path.join(
        ROOT, tier, f"date={d}", f"underlying={und}", "*.parquet")))
    if not files:
        return []
    tbl = pa.concat_tables([pq.read_table(f) for f in files])
    return tbl.to_pylist()


def _bucket(ts, minutes):
    if minutes is None:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    m = (ts.minute // minutes) * minutes
    return ts.replace(minute=m, second=0, microsecond=0)


def _write(tier, d, und, rows):
    if not rows:
        return
    outdir = os.path.join(ROOT, tier, f"date={d}", f"underlying={und}")
    os.makedirs(outdir, exist_ok=True)
    cols = {f.name: [r.get(f.name) for r in rows] for f in DERIVED}
    pq.write_table(pa.table(cols, schema=DERIVED),
                   os.path.join(outdir, "part-00000.parquet"), compression="zstd")


def build_day(d):
    """Idempotent: re-running for any date reproduces identical output."""
    base = os.path.join(ROOT, "bars_1m", f"date={d}")
    if not os.path.isdir(base):
        print(f"no 1m data for {d}")
        return
    unds = [p.split("=", 1)[1] for p in os.listdir(base)]

    for und in unds:
        src = _read_day("bars_1m", d, und)
        if not src:
            continue
        for tier, minutes in TIERS.items():
            groups = {}
            for r in src:
                groups.setdefault((r["instrument_token"], _bucket(r["ts"], minutes)), []).append(r)
            out = []
            for (_, bucket_ts), bars in groups.items():
                merged = rollup(bars)
                merged["ts"] = bucket_ts
                for k in ("tradingsymbol", "underlying", "expiry", "strike", "opt_type"):
                    merged[k] = bars[0][k]
                out.append(merged)
            _write(tier, d, und, out)
            print(f"{d} {und:10s} {tier:9s} {len(out):>7,} bars")


if __name__ == "__main__":
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    build_day(d)
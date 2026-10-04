import os, glob, shutil
from datetime import datetime, timezone, date
import pyarrow.parquet as pq
from aggregator import Aggregator
from writer import BarWriter, SCHEMA
import rollups

TMP = "data_test"
TOK = 111
IMAP = {TOK: {"tradingsymbol": "NIFTY26SEP24200CE", "underlying": "NIFTY",
              "expiry": date(2026, 9, 29), "strike": 24200.0, "opt_type": "CE"}}


def setup_function():
    shutil.rmtree(TMP, ignore_errors=True)


def teardown_function():
    shutil.rmtree(TMP, ignore_errors=True)


def _bars(n, start_min=15):
    a = Aggregator([TOK])
    out = []
    for i in range(n):
        a.add_tick({"instrument_token": TOK, "last_price": 100 + i,
                    "volume_traded": 100 * (i + 1), "oi": 5000 + i})
        ts = datetime(2026, 9, 1, 9, start_min + i, tzinfo=timezone.utc)
        out.extend(a.flush(ts))
    return out


def test_writes_partitioned_parquet():
    w = BarWriter(IMAP, root=TMP, flush_every=100)
    w.add(_bars(3))
    paths = w.flush()
    assert len(paths) == 1
    assert "date=2026-09-01" in paths[0]
    assert "underlying=NIFTY" in paths[0]

    t = pq.read_table(paths[0])
    assert t.num_rows == 3
    assert t.schema.equals(SCHEMA)
    assert t.column("tradingsymbol")[0].as_py() == "NIFTY26SEP24200CE"


def test_unknown_token_is_dropped_not_guessed():
    w = BarWriter(IMAP, root=TMP, flush_every=100)
    w.add([{**_bars(1)[0], "instrument_token": 999}])
    assert w.flush() == []


def test_rollup_produces_all_tiers_and_is_idempotent():
    w = BarWriter(IMAP, root=TMP, flush_every=100)
    w.add(_bars(15))
    w.flush()

    rollups.ROOT = TMP
    rollups.build_day(date(2026, 9, 1))

    for tier, expected in (("bars_5m", 3), ("bars_15m", 1), ("bars_1d", 1)):
        f = glob.glob(f"{TMP}/{tier}/date=2026-09-01/underlying=NIFTY/*.parquet")
        assert len(f) == 1
        assert pq.read_table(f[0]).num_rows == expected

    p = glob.glob(f"{TMP}/bars_5m/**/*.parquet", recursive=True)[0]
    before = open(p, "rb").read()
    rollups.build_day(date(2026, 9, 1))
    after = open(p, "rb").read()
    assert before == after


# ---------------------------------------------------- restart mid-session

def _files(root, und="NIFTY"):
    return sorted(glob.glob(f"{root}/bars_1m/date=2026-09-01/underlying={und}/*.parquet"))


def _minutes(root):
    ts = []
    for f in _files(root):
        ts += [t.as_py() for t in pq.read_table(f).column("ts")]
    return sorted(ts)


def test_restart_mid_session_never_overwrites_the_morning(tmp_path):
    """The 23/25 Sep 2026 bug: a second process started at part-00000 and
    wrote the afternoon over the morning, one file per minute."""
    root = str(tmp_path)
    morning = BarWriter(IMAP, root=root, flush_every=1)
    for b in _bars(5, start_min=15):          # 09:15-09:19
        morning.add([b])
    morning.flush()
    before = {f: open(f, "rb").read() for f in _files(root)}
    assert len(before) == 5

    afternoon = BarWriter(IMAP, root=root, flush_every=1)   # the restart
    for b in _bars(3, start_min=40):          # 09:40-09:42
        afternoon.add([b])
    afternoon.flush()

    after = _files(root)
    assert len(after) == 8
    for f, blob in before.items():            # morning untouched, byte for byte
        assert open(f, "rb").read() == blob
    mins = [t.minute for t in _minutes(root)]
    assert mins == [15, 16, 17, 18, 19, 40, 41, 42]
    assert os.path.basename(after[-1]) == "part-00007.parquet"


def test_numbering_is_per_partition_and_stays_monotonic(tmp_path):
    root = str(tmp_path)
    imap = {**IMAP, 222: {"tradingsymbol": "NIFTY 50", "underlying": "INDEX",
                          "expiry": None, "strike": None, "opt_type": None}}
    # A partition that already has 3 parts, another that has none.
    first = BarWriter(IMAP, root=root, flush_every=1)
    for b in _bars(3):
        first.add([b])
    w = BarWriter(imap, root=root, flush_every=100)
    b = _bars(1, start_min=30)[0]
    w.add([b, {**b, "instrument_token": 222}])
    paths = w.flush()
    names = sorted(os.path.basename(p) for p in paths)
    assert names == ["part-00000.parquet", "part-00003.parquet"]
    assert w.part == 4                        # next flush starts past both

"""/api/pulse end to end on a tiny fake session: files in, one payload out."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

import pulse_api as PA
import sectors as SEC
from writer import BarWriter, load_instrument_map

DAY = "2026-11-12"


def _bar(tok, ts, c, oi=None):
    return {"ts": ts, "instrument_token": tok, "open": c, "high": c + 0.4, "low": c - 0.4,
            "close": c, "volume": 1000, "pv": c * 1000, "oi": oi, "bid": None, "ask": None,
            "bid_qty": None, "ask_qty": None, "tick_count": 3, "is_interpolated": False}


def make_session(tmp_path):
    master = {"date": DAY, "expiries": {"NIFTY": ["2026-11-18"]}, "instruments": [
        {"instrument_token": 1, "tradingsymbol": "UPCO", "name": "UPCO", "expiry": None, "strike": None,
         "instrument_type": "EQ", "lot_size": 100, "kind": "stock", "prev_close": 100.0},
        {"instrument_token": 2, "tradingsymbol": "DOWNCO", "name": "DOWNCO", "expiry": None, "strike": None,
         "instrument_type": "EQ", "lot_size": 100, "kind": "stock", "prev_close": 50.0},
        {"instrument_token": 3, "tradingsymbol": "UPCO26NOVFUT", "name": "UPCO", "expiry": "2026-11-25",
         "strike": None, "instrument_type": "FUT", "lot_size": 100, "kind": "stock_fut", "prev_close": 100.0}]}
    (tmp_path / "data" / "instruments").mkdir(parents=True)
    p = tmp_path / "data" / "instruments" / f"{DAY}.json"
    p.write_text(json.dumps(master))
    imap = load_instrument_map(str(p))
    w = BarWriter(imap, root=str(tmp_path / "data"), tier="stocks_1m", flush_every=999)
    t0 = datetime(2026, 11, 12, 3, 45, tzinfo=timezone.utc)
    for i in range(40):
        ts = t0 + timedelta(minutes=i)
        w.add([_bar(1, ts, 100 + 0.1 * i), _bar(2, ts, 50 - 0.05 * i), _bar(3, ts, 100.2 + 0.1 * i, 5000 + 10 * i)])
    w.flush()
    (tmp_path / "data" / "sectors.json").write_text(json.dumps(
        {"date": DAY, "map": {"UPCO": {"sector": "IT"}, "DOWNCO": {"sector": "Bank"}}}))


def test_pulse_payload_has_every_block(tmp_path, monkeypatch):
    make_session(tmp_path)
    monkeypatch.setattr(PA, "ROOT", str(tmp_path))
    monkeypatch.setattr(PA, "STOCKS", str(tmp_path / "data" / "stocks_1m"))
    monkeypatch.setattr(PA, "INSTR", str(tmp_path / "data" / "instruments"))
    monkeypatch.setattr(SEC, "OUT", str(tmp_path / "data" / "sectors.json"))
    monkeypatch.setattr(PA, "global_pct", lambda ttl=600: 0.3)
    import fii_dii as FD
    monkeypatch.setattr(FD, "fetch_background", lambda: False)          # no network in tests
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii_none.json"))
    monkeypatch.setattr(PA, "indices", lambda day: {
        "NIFTY": {"last": 25000.0, "chg_pct": 0.4}, "VIX": {"last": 15.0}})
    out = PA.api_pulse()
    assert out["session"] == DAY
    assert out["breadth"]["advancers"] == 1 and out["breadth"]["decliners"] == 1
    assert out["flow"]["top_in"][0]["symbol"] == "UPCO"
    assert out["sectors"]["leaders"][0]["sector"] == "IT"
    assert out["stock_oi"]["rows"][0]["read"] == "long buildup"
    assert out["order_blocks"] and out["order_blocks"][0]["candles"]
    assert out["indices"]["NIFTY"]["range"]["lo"] < 25000 < out["indices"]["NIFTY"]["range"]["hi"]
    assert out["mood"]["available"] and 0 <= out["mood"]["score"] <= 100
    assert out["fii_dii"]["available"] is False
    json.dumps(out)                                  # publishable as-is


def test_pulse_without_stock_data_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "STOCKS", str(tmp_path / "none"))
    monkeypatch.setattr(PA, "INSTR", str(tmp_path / "none"))
    monkeypatch.setattr(SEC, "OUT", str(tmp_path / "none.json"))
    monkeypatch.setattr(PA, "global_pct", lambda ttl=600: None)
    import api
    import fii_dii as FD
    monkeypatch.setattr(FD, "fetch_background", lambda: False)
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii_none.json"))
    def no_sessions():
        raise RuntimeError("no option data either")
    monkeypatch.setattr(api, "latest_session", no_sessions)
    out = PA.api_pulse()
    assert out["session"] is None and out["breadth"]["available"] is False
    assert out["mood"]["available"] is False


def test_pulse_before_stock_data_still_shows_indices_and_mood(tmp_path, monkeypatch):
    from datetime import date as D
    import api
    import fii_dii as FD
    monkeypatch.setattr(PA, "STOCKS", str(tmp_path / "none"))
    monkeypatch.setattr(PA, "INSTR", str(tmp_path / "none"))
    monkeypatch.setattr(SEC, "OUT", str(tmp_path / "none.json"))
    monkeypatch.setattr(PA, "global_pct", lambda ttl=600: None)
    monkeypatch.setattr(FD, "fetch_background", lambda: False)
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii_none.json"))
    monkeypatch.setattr(api, "latest_session", lambda: D(2026, 11, 11))
    monkeypatch.setattr(PA, "vix_history", lambda: [12.0, 13.0, 14.0])
    monkeypatch.setattr(PA, "indices", lambda day: {"NIFTY": {"last": 25800.0, "chg_pct": 0.5},
                                                     "VIX": {"last": 13.0}})
    out = PA.api_pulse()
    assert out["session"] == "2026-11-11" and out["breadth"]["available"] is False
    assert out["indices"]["NIFTY"]["last"] == 25800.0 and out["vix"]["percentile"] == 67
    assert out["mood"]["available"] and "trend" not in out["mood"]["missing"]


def test_vix_history_extends_csv_with_collected_sessions(tmp_path, monkeypatch):
    from datetime import date as D
    import api
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "vix_history.csv").write_text("date,vix\n2026-08-27,11.07\n2026-08-28,10.68\n")
    monkeypatch.setattr(PA, "ROOT", str(tmp_path))
    monkeypatch.setattr(api, "sessions", lambda: [D(2026, 8, 28), D(2026, 11, 11)])
    monkeypatch.setattr(PA, "closes_on", lambda d: {"INDIA VIX": 12.9, "NIFTY 50": 25800.0})
    PA._vix["key"] = None
    assert PA.vix_history() == [11.07, 10.68, 12.9]


def test_closes_on_reads_past_session_without_touching_the_live_cache(tmp_path, monkeypatch):
    from datetime import date as D
    import api
    d = tmp_path / "bars" / "date=2026-11-10" / "underlying=INDEX"
    d.mkdir(parents=True)
    pd.DataFrame({"ts": [1, 2, 1], "tradingsymbol": ["NIFTY 50", "NIFTY 50", "INDIA VIX"],
                  "close": [25000.0, 25100.0, 13.2], "open": [0, 0, 0]}).to_parquet(d / "p1.parquet")
    monkeypatch.setattr(api, "BARS", str(tmp_path / "bars"))
    monkeypatch.setattr(api, "latest_session", lambda: D(2026, 11, 12))
    api._parts["sentinel-today"] = ("sig", "today's frame")
    PA._closes.clear()
    assert PA.closes_on(D(2026, 11, 10)) == {"NIFTY 50": 25100.0, "INDIA VIX": 13.2}
    assert "sentinel-today" in api._parts                     # today's cache left alone
    assert "2026-11-10" in PA._closes                         # remembered: past sessions never change
    api._parts.pop("sentinel-today")


# --- closes_on reads only the newest parts (18 Nov 2026: 16-minute cold starts) ---

ALL4 = ["NIFTY 50", "NIFTY BANK", "SENSEX", "INDIA VIX"]


def _day_parts(tmp_path, n, symbols_for=lambda i: ALL4, day="2026-11-10"):
    d = tmp_path / "bars" / f"date={day}" / "underlying=INDEX"
    d.mkdir(parents=True)
    for i in range(n):
        syms = symbols_for(i)
        pd.DataFrame({"ts": [i] * len(syms), "tradingsymbol": syms,
                      "close": [1000.0 + i + k for k, _ in enumerate(syms)],
                      "open": [0.0] * len(syms)}).to_parquet(d / f"part-{i:05d}.parquet")
    return d


def _setup(tmp_path, monkeypatch):
    from datetime import date as D
    import api
    monkeypatch.setattr(api, "BARS", str(tmp_path / "bars"))
    monkeypatch.setattr(api, "latest_session", lambda: D(2026, 11, 12))
    PA._closes.clear()
    reads = []
    real = PA._read_closes
    monkeypatch.setattr(PA, "_read_closes", lambda p: reads.append(p) or real(p))
    return D(2026, 11, 10), reads


def test_closes_on_reads_only_the_last_parts_and_matches_a_full_read(tmp_path, monkeypatch):
    import glob, os
    d = _day_parts(tmp_path, 375)
    day, reads = _setup(tmp_path, monkeypatch)
    got = PA.closes_on(day)
    assert len(reads) == PA.TAIL_PARTS                      # 5 files, not 375
    full = PA._closes_from(sorted(glob.glob(os.path.join(str(d), "*.parquet"))))
    assert got == full and got["NIFTY 50"] == 1374.0 and got["INDIA VIX"] == 1377.0


def test_closes_on_falls_back_when_a_series_stopped_early(tmp_path, monkeypatch):
    # SENSEX stops publishing at part 200: its last close is only in the early parts.
    _day_parts(tmp_path, 375, symbols_for=lambda i: ALL4 if i < 200 else ALL4[:2] + ALL4[3:])
    day, reads = _setup(tmp_path, monkeypatch)
    got = PA.closes_on(day)
    assert got["SENSEX"] == 1000.0 + 199 + 2                 # found by the full read
    assert len(reads) == PA.TAIL_PARTS + 375


def test_closes_on_falls_back_on_an_unreadable_tail_part(tmp_path, monkeypatch):
    d = _day_parts(tmp_path, 50)
    (d / "part-00049.parquet").write_bytes(b"not parquet")
    day, reads = _setup(tmp_path, monkeypatch)
    got = PA.closes_on(day)
    assert got["NIFTY 50"] == 1048.0                         # last readable part

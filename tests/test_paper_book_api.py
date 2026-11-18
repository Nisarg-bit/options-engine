"""service/paper_book_api.py: chain prices cached per minute, publish never raises."""
from datetime import date, datetime

import api
import paper_book as PB
import paper_book_api as PBA


def test_price_reads_chain_mids_once_a_minute(monkeypatch):
    calls = []
    def chain(day, und, exp):
        calls.append(exp)
        return [{"strike": 25000.0, "CE": {"mid": 101.5}, "PE": {"mid": 88.0}}], {}, 2, None
    monkeypatch.setattr(api, "latest_session", lambda: date(2026, 11, 12))
    monkeypatch.setattr(api, "build_chain", chain)
    PBA._chains.clear()
    now = datetime(2026, 11, 12, 11, 0, tzinfo=PB.IST)
    assert PBA.price("NIFTY", "2026-11-17", 25000, "CE", now) == 101.5
    assert PBA.price("NIFTY", "2026-11-17", 25000.0, "PE", now) == 88.0
    assert PBA.price("NIFTY", "2026-11-17", 25100, "CE", now) is None
    assert calls == [date(2026, 11, 17)]


def test_publish_survives_a_broken_step(tmp_path, monkeypatch):
    monkeypatch.setattr(PB, "OUT", str(tmp_path / "pb.json"))
    def boom(*a, **k):
        raise RuntimeError("no chain")
    monkeypatch.setattr(PB, "step", boom)
    out = PBA.publish()
    assert out["events"][0].startswith("step failed")
    assert set(out["books"]) == {"intraday", "hold"}

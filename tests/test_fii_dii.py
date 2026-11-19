"""pipeline/fii_dii.py: parsing NSE's two files, storage, the fetch limits."""
from datetime import datetime

import fii_dii as FD

CASH = [{"buyValue": "12764.97", "category": "DII", "date": "09-Nov-2026", "netValue": "-7172.53", "sellValue": "19937.5"},
        {"buyValue": "20310.99", "category": "FII/FPI", "date": "09-Nov-2026", "netValue": "7212.22", "sellValue": "13098.77"}]

OI_CSV = '''"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Nov 09, 2026"
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short\t,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts\t,Total Short Contracts
Client,250000,120000,2500000,300000,4000000,3500000,3800000,3900000,700000,300000,500000,250000,11000000,9000000
DII,80000,30000,200000,3000000,100,0,0,0,0,0,2000,0,300000,3000000
FII,90000,210000,1800000,1500000,900000,1100000,800000,600000,200000,90000,150000,60000,4000000,3500000
Pro,60000,120000,400000,100000,2500000,2300000,2600000,2400000,300000,200000,250000,180000,6000000,5500000
TOTAL,480000,480000,4900000,4900000,7400100,6900000,7200000,6900000,1200000,590000,902000,490000,21300000,21000000
'''


def test_parse_cash():
    d, c = FD.parse_cash(CASH)
    assert d == "2026-11-09"
    assert c["fii"]["net_cr"] == 7212.22 and c["dii"]["sell_cr"] == 19937.5
    assert FD.parse_cash([]) == (None, {})
    assert FD.parse_cash([{"category": "FII/FPI", "date": "bad"}]) == (None, {})


def test_parse_participant_oi_by_column_name():
    oi = FD.parse_participant_oi(OI_CSV)
    assert set(oi) == {"client", "dii", "fii", "pro"}
    assert oi["fii"]["fut_long"] == 90000 and oi["fii"]["fut_short"] == 210000
    assert oi["fii"]["put_short"] == 600000
    p = FD.positioning(oi["fii"])
    assert p["fut_net"] == -120000 and p["fut_long_pct"] == 30.0
    assert p["calls_net"] == 100000 and p["puts_net"] == 500000
    assert FD.parse_participant_oi("garbage") == {}


def test_summary_and_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii.json"))
    assert FD.summary({})["available"] is False
    oi = FD.parse_participant_oi(OI_CSV)
    oi_prev = {k: dict(v) for k, v in oi.items()}
    oi_prev["fii"]["fut_long"] = 100000
    days = {"2026-11-06": {"cash": {"fii": {"net_cr": -1000.0}, "dii": {"net_cr": 900.0}}, "oi": oi_prev},
            "2026-11-09": {"cash": FD.parse_cash(CASH)[1], "oi": oi}}
    FD.save(days)
    s = FD.summary(FD.load())
    assert s["date"] == "2026-11-09" and s["cash"]["fii"]["net_cr"] == 7212.22
    assert s["fii_5d_cr"] == round(7212.22 - 1000) and len(s["history"]) == 2
    assert s["fii_fo"]["fut_net_chg"] == -10000


def test_fetch_respects_time_and_does_not_touch_network(tmp_path, monkeypatch):
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii.json"))
    def boom():
        raise AssertionError("network called")
    monkeypatch.setattr(FD, "_session", boom)
    assert FD.fetch(now=datetime(2026, 11, 11, 15, 0, tzinfo=FD.IST)) == {}   # too early
    assert FD.fetch(now=datetime(2026, 11, 14, 19, 0, tzinfo=FD.IST)) == {}   # Saturday


def test_fetch_merges_and_keeps_history_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(FD, "OUT", str(tmp_path / "fii.json"))
    FD._last_try["at"] = 0

    class R:
        def __init__(self, j=None, t="", code=200):
            self._j, self.text, self.status_code = j, t, code
        def json(self):
            return self._j

    class S:
        def get(self, url, timeout=0):
            return R(j=CASH) if "fiidii" in url else R(t=OI_CSV)
    monkeypatch.setattr(FD, "_session", lambda: S())
    d = FD.fetch(now=datetime(2026, 11, 9, 19, 0, tzinfo=FD.IST))
    assert d["2026-11-09"]["cash"]["fii"]["net_cr"] == 7212.22 and d["2026-11-09"]["oi"]["fii"]
    def down():
        raise OSError("blocked")
    monkeypatch.setattr(FD, "_session", down)
    assert FD.fetch(force=True)["2026-11-09"]["oi"]                        # history kept


def test_fetch_background_never_blocks_and_never_doubles(monkeypatch):
    import threading
    gate = threading.Event()
    calls = []
    def slow(force=False, now=None):
        calls.append(1)
        gate.wait(5)                                 # simulates NSE holding the connection
    monkeypatch.setattr(FD, "fetch", slow)
    FD._bg["thread"] = None
    assert FD.fetch_background() is True             # returns at once
    assert FD.fetch_background() is False            # still running: no second thread
    gate.set()
    FD._bg["thread"].join(2)
    assert calls == [1]

"""/api/options_view with the heavy api.py calls replaced by fakes."""
import json
from datetime import date, datetime, timedelta

import pandas as pd

import api
import options_view_api as OV

DAY = date(2026, 11, 12)


def _index_bars(sym, start, step, n=60):
    t0 = datetime(2026, 11, 12, 9, 15)
    rows = []
    for i in range(n):
        c = start + step * i
        rows.append({"ts": t0 + timedelta(minutes=i), "tradingsymbol": sym,
                     "open": c, "high": c + 2, "low": c - 2, "close": c})
    return pd.DataFrame(rows)


def test_options_view_payload(monkeypatch):
    today = pd.concat([_index_bars("NIFTY 50", 25000, 3), _index_bars("NIFTY BANK", 57000, 0),
                       _index_bars("INDIA VIX", 13, 0)])
    yday = pd.concat([_index_bars("NIFTY 50", 24900, 0), _index_bars("NIFTY BANK", 57000, 0)])
    monkeypatch.setattr(api, "latest_session", lambda: DAY)
    monkeypatch.setattr(api, "sessions", lambda: [date(2026, 11, 11), DAY])
    monkeypatch.setattr(api, "read_parts", lambda d, u: today if d == DAY else yday)
    cat = [{"key": "bull_call_spread", "label": "Bull Call Spread", "group": "bullish", "available": True,
            "loss_unbounded": False, "max_loss_rs": -4000, "margin_est": 4000, "pop": 0.45, "net_ev_rs": 90}]
    monkeypatch.setattr(api, "api_structures", lambda underlying: {
        "spot": 25177.0, "ratio": 1.0, "catalogue": cat, "expiry": "2026-11-17", "dte": 5, "lot": 65})
    monkeypatch.setattr(api, "api_analytics", lambda underlying: {
        "levels": {"support": 24800, "resistance": 25100}, "max_pain": 25000,
        "pcr": {"pcr_oi": 1.1, "pcr_oi_change": 1.4}})
    monkeypatch.setattr(OV, "market_breadth_flow", lambda day: (0.3, 0.1))
    import pulse_api as PA
    monkeypatch.setattr(PA, "vix_history", lambda: [12, 13, 14, 15, 16] * 60)
    monkeypatch.setattr(PA, "closes_on", lambda day: {"NIFTY 50": 24900.0, "NIFTY BANK": 57000.0})
    out = OV.api_options_view()
    n = out["indices"]["NIFTY"]
    assert n["available"] and n["call"] == "up"
    assert n["chg_pct"] > 0 and n["opening_range"][1] > n["opening_range"][0]
    assert n["strategies"][0]["sizing"]["lots"] == 5
    assert n["inside_walls"] is False                  # above the call wall
    assert out["indices"]["BANKNIFTY"]["available"]
    assert out["vix"]["last"] == 13.0
    json.dumps(out)


def test_one_index_failing_keeps_the_other(monkeypatch):
    monkeypatch.setattr(api, "latest_session", lambda: DAY)
    monkeypatch.setattr(api, "sessions", lambda: [DAY])
    monkeypatch.setattr(api, "read_parts", lambda d, u: None)
    def boom(underlying):
        if underlying == "BANKNIFTY":
            raise RuntimeError("chain too thin")
        return {"spot": 25000.0, "ratio": 1.0, "catalogue": [], "expiry": "2026-11-17"}
    monkeypatch.setattr(api, "api_structures", boom)
    monkeypatch.setattr(api, "api_analytics", lambda underlying: {"pcr": {"pcr_oi_change": 1.6}, "levels": {}})
    monkeypatch.setattr(OV, "market_breadth_flow", lambda day: (None, None))
    out = OV.api_options_view()
    assert out["indices"]["BANKNIFTY"]["available"] is False
    assert "chain too thin" in out["indices"]["BANKNIFTY"]["reason"]
    assert out["indices"]["NIFTY"]["available"]

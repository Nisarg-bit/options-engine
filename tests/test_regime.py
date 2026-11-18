"""quant/regime.py: every rule on hand-made inputs."""
import math

import regime as RG


def test_trend_and_opening_range_signs():
    assert RG.trend_score(0.8, 0.8) > 0.9 and RG.trend_score(-0.8, 0.8) < -0.9
    assert RG.trend_score(None, 0.8) is None and RG.trend_score(0.5, None) is None
    assert RG.opening_range_score(110, 105, 100) == 1.0
    assert RG.opening_range_score(95, 105, 100) == -1.0
    assert RG.opening_range_score(102.5, 105, 100) == 0.0
    assert RG.opening_range_score(100, 100, 100) is None


def test_oi_flow_is_symmetric_in_log():
    assert math.isclose(RG.oi_flow_score(2.0), -RG.oi_flow_score(0.5))
    assert RG.oi_flow_score(1.0) == 0.0
    assert RG.oi_flow_score(None) is None and RG.oi_flow_score(0) is None


def test_direction_reweights_missing_parts():
    d, have = RG.direction({"trend": 1.0, "breadth": None, "flow": -1.0})
    assert set(have) == {"trend", "flow"}
    assert math.isclose(d, (0.30 - 0.15) / 0.45)
    assert RG.direction({"trend": None})[0] is None


def test_odds_add_to_one_and_follow_scores():
    up = RG.odds(0.8, -0.5)
    assert abs(sum(up.values()) - 1) < 0.002 and RG.call(up) == "up"
    dn = RG.odds(-0.8, -0.5)
    assert RG.call(dn) == "down"
    side = RG.odds(0.0, 0.9)
    assert RG.call(side) == "sideways"
    assert RG.call(RG.odds(0.15, 0.1)) == "unclear"


def test_sideways_score_needs_quiet_and_walls():
    quiet = RG.sideways_score(0.05, True, 0.3)
    loud = RG.sideways_score(0.7, False, 1.4)
    assert quiet > 0.5 and loud < -0.5
    assert RG.range_used(101, 99, 100, 1.0) == 1.0


def test_vol_state():
    assert RG.vol_state(0.1)["state"] == "low"
    assert RG.vol_state(0.5, 1.1) == {"state": "normal", "premium": "rich"}
    assert RG.vol_state(0.9, 0.9) == {"state": "high", "premium": "cheap"}
    assert RG.vol_state(None)["state"] is None


def test_sizing_uses_max_loss_and_margin():
    z = RG.size_lots(12_350, 15_000)               # 1% of 20L = 20,000
    assert z["by_risk"] == 1 and z["lots"] == 1
    z = RG.size_lots(4_000, 300_000)               # margin binds: 50% of 20L / 3L = 3
    assert z["by_risk"] == 5 and z["by_margin"] == 3 and z["lots"] == 3
    z = RG.size_lots(30_000, 10_000)
    assert z["lots"] == 0 and z["reason"]
    assert RG.size_lots(None, 1)["reason"] == "loss not capped"


CAT = [
    {"key": "bull_call_spread", "label": "Bull Call Spread", "group": "bullish", "available": True,
     "loss_unbounded": False, "max_loss_rs": -3000, "margin_est": 3000, "pop": 0.45, "net_ev_rs": 120},
    {"key": "bull_put_spread", "label": "Bull Put Spread", "group": "bullish", "available": False},
    {"key": "bull_condor", "label": "Bullish Condor", "group": "bullish", "available": True,
     "loss_unbounded": False, "max_loss_rs": -1500, "margin_est": 1500, "pop": 0.3, "net_ev_rs": -40},
    {"key": "sell_put", "label": "Sell Put", "group": "bullish", "available": True, "loss_unbounded": True},
    {"key": "iron_condor", "label": "Iron Condor", "group": "neutral", "available": True,
     "loss_unbounded": False, "max_loss_rs": -9000, "margin_est": 9000, "pop": 0.6, "net_ev_rs": 50},
]


def test_pick_follows_playbook_and_skips_unavailable_or_unlimited():
    p = RG.pick("up", "normal", CAT)
    assert [x["key"] for x in p] == ["bull_call_spread", "bull_condor"]
    assert p[0]["sizing"]["lots"] == 6                 # 20,000 // 3,000
    assert RG.pick("sideways", None, CAT)[0]["key"] == "iron_condor"
    assert RG.pick("unclear", "low", CAT) == []
    for picks in RG.PLAYBOOK.values():
        assert "sell_put" not in picks and "short_strangle" not in picks


def test_view_end_to_end():
    v = RG.view({"trend": 0.9, "opening_range": 1.0, "breadth": 0.5, "flow": 0.3, "oi_flow": 0.2},
                inside_walls=False, used=1.2, vix_pct=0.5, rich_ratio=1.0, catalogue=CAT)
    assert v["available"] and v["call"] == "up"
    assert v["strategies"][0]["key"] == "bull_call_spread"
    assert len(v["inputs"]) == 5 and not v["missing"]
    assert RG.view({})["available"] is False


def test_unclear_view_still_offers_the_leading_side():
    v = RG.view({"trend": 0.21}, inside_walls=None, used=None, vix_pct=0.5, catalogue=CAT)
    assert v["call"] == "unclear" and v["strategies"] == []
    assert v["lean"] == "up" and v["lean_strategies"][0]["key"] == "bull_call_spread"

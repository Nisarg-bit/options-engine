"""Build the FIXTURE strategy_health block for firebase/public/futures-demo.json by
running the real daily evaluation (sf_daily.run_day) on a synthetic session, with
one fixture challenger in shadow. Every number in it is synthetic.

    python sf_health_demo.py [--write firebase/public/futures-demo.json]
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

import sf_api
import sf_audit
import sf_daily as DY
import sf_fixtures as F
from sf_cli import replay
from sf_core import ReplayClock, ist
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store


def build(tmp):
    for k, sub in (("EVAL", "eval"), ("SHADOW", "shadow"), ("CONFIGS", "configs")):
        setattr(DY, k, os.path.join(tmp, sub))
    DY.CHALLENGERS = os.path.join(tmp, "challengers.json")
    DY.AUDIT = os.path.join(tmp, "eval", "audit.jsonl")
    DY.alert = lambda text: None
    DY.feeds_complete = lambda day, pol, root=None: (True, [])
    sf_audit.data_quality = lambda day: ({"EQ": {"symbols": 12, "median_minutes": 375, "zero_volume_share": 0.031,
                                                 "first_bar_open_at_extreme_share": 0.0},
                                          "STKFUT": {"symbols": 12, "median_minutes": 375, "zero_volume_share": 0.012,
                                                     "first_bar_open_at_extreme_share": 0.0}},
                                         [{"check": "fixture data", "status": "OK", "detail": "synthetic session"}])
    sc = F.bullish()
    day = sc.sessions[0]
    db = os.path.join(tmp, "champ.db")
    store = Store(db)
    clock = ReplayClock(ist(day, "09:15"))
    e = Engine(store, FixtureData(sc.build()), clock, F.demo_config(), env="fixture", modes=("intraday",))
    replay(e, clock, [day])
    cfg = json.loads(json.dumps(store.config()[1]))
    cfg["profiles"]["intraday"]["exit_plan"]["hard_max_stop_pct"] = 2.0
    cfg["label"] = "FIXTURE challenger: intraday hard stop cap 2.0%"
    ch = DY.add_challenger(cfg, "FIXTURE: hard_max_stop_pct 2.5 -> 2.0", {"k": 1}, ist(day, "09:00"))
    shutil.copy(db, ch["db"])
    DY.run_day(day, champion_db=db, now=ist(day, "16:10"))
    h = sf_api.strategy_health(store, ist(day, "16:20"))
    h["fixture"] = True
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="sf-health-demo-")
    try:
        h = build(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if a.write:
        with open(a.write, encoding="utf-8") as f:
            demo = json.load(f)
        demo["strategy_health"] = json.loads(json.dumps(h, default=str))
        with open(a.write, "w", encoding="utf-8") as f:
            json.dump(demo, f, default=str)
        print("wrote strategy_health into", a.write)
    else:
        print(json.dumps(h, indent=1, default=str)[:3000])


if __name__ == "__main__":
    sys.exit(main())

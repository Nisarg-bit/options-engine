"""Candidate fixes from the loss study, DESIGNED on 2016-2022, TESTED on 2023-2026.

Each fix is one change to the live rule, run under the same risk rules as
replay.py (hold to expiry). A fix is kept only if it also helps on the test
years it was not designed on.

  F1 one_per_expiry   never stack a second trade on an expiry already held
                      (losses came in clusters on one expiry)
  F2 vix_jump         skip entry when India VIX rose > 5 points in 5 sessions
                      (March 2020: entries into a rising panic)
  F3 symmetric        drop the workbook's extra put step (_otm_pe '- step'):
                      21 of 32 losing trades were breached on the CALL side
  F4 cs_min 1.10      demand richer premium than the 0.9346 gate
  F5 min_dte 2        no entries the day before expiry
"""
import json, sys
from datetime import date
import pandas as pd
import pricing as P
import strategies as S
import replay as RP

TRAIN_END = date(2022, 12, 31)
_orig_otm_pe = S._otm_pe


def run(name, chains, spot, vix, tdays, exps, one_per_expiry=False, vix_jump=None,
        symmetric=False, cs_min=None, min_dte=1):
    S._otm_pe = (lambda sp, off, step: P.mround(sp - off, step)) if symmetric else _orig_otm_pe
    RP.CS_GATE = cs_min or 0.9346
    RP.MIN_DTE = min_dte
    vs = pd.Series(vix).sort_index()
    sig = {}
    for d in tdays:
        s, _ = RP.signal(d, chains, spot, vix, exps)
        if not s:
            continue
        if vix_jump is not None:
            past = vs[vs.index <= d].tail(6)
            if len(past) > 1 and past.iloc[-1] - past.iloc[0] > vix_jump:
                continue
        sig[d] = s
    if one_per_expiry:
        orig = RP.simulate
    trades, eq, trips = simulate_opx(sig, chains, spot, tdays, one_per_expiry)
    t = pd.DataFrame([{k: v for k, v in x.items() if k not in ("shorts", "longs")} for x in trades])
    t["exit"] = pd.to_datetime(t["exit"]).dt.date
    tr, te = t[t.exit <= TRAIN_END], t[t.exit > TRAIN_END]
    e = eq.set_index("day").equity
    dd = lambda s: float((s.cummax() - s).max()) if len(s) else 0.0
    res = {"name": name, "train_trades": len(tr), "train_pnl": round(tr.pnl.sum()),
           "test_trades": len(te), "test_pnl": round(te.pnl.sum()),
           "total": round(t.pnl.sum()), "max_dd": round(dd(e)), "breaker": trips,
           "win": round((t.pnl > 0).mean(), 3)}
    print(json.dumps(res), flush=True)
    S._otm_pe = _orig_otm_pe
    RP.CS_GATE, RP.MIN_DTE = 0.9346, 1
    return res, t


def simulate_opx(sig, chains, spot, tdays, one_per_expiry):
    if not one_per_expiry:
        return RP.simulate("hold", sig, chains, spot, tdays)
    # drop a signal whose expiry already has an entry on an earlier signal day
    seen, keep = set(), {}
    # a conservative approximation: allow only the FIRST signal per expiry
    for d in sorted(sig):
        e = sig[d]["expiry"]
        if e in seen:
            continue
        seen.add(e); keep[d] = sig[d]
    return RP.simulate("hold", keep, chains, spot, tdays)


def main():
    chains, spot, vix, tdays, exps = RP.load()
    runs = [("baseline (live rule)", {}),
            ("F1 one per expiry", {"one_per_expiry": True}),
            ("F2 skip VIX +5 in 5d", {"vix_jump": 5.0}),
            ("F3 symmetric strikes", {"symmetric": True}),
            ("F4 cs >= 1.10", {"cs_min": 1.10}),
            ("F5 min dte 2", {"min_dte": 2}),
            ("F1+F2", {"one_per_expiry": True, "vix_jump": 5.0}),
            ("F1+F2+F3", {"one_per_expiry": True, "vix_jump": 5.0, "symmetric": True})]
    out = []
    for name, kw in runs:
        r, t = run(name, chains, spot, vix, tdays, exps, **kw)
        out.append(r)
        t.to_csv(f"data/replay/tune_{name.split()[0]}.csv", index=False)
    json.dump(out, open("data/replay/tune.json", "w"), indent=1)


if __name__ == "__main__":
    main()

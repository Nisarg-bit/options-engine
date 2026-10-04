"""Does the browser's payoff engine agree with the server's?

The Firebase build is a static page, so the Builder computes payoffs in
JavaScript rather than calling /api/payoff. That is a second implementation of
numbers the Telegram signal also produces -- and a second implementation that
silently disagrees is worse than none, because the dashboard would quietly
contradict the thing you actually trade off.

So: run both over every structure in the golden fixture, plus a few awkward
shapes the fixture does not contain, and require agreement. The JS runs in a
real browser, in the real page, so this tests what ships rather than a copy.

    python test_payoff_js.py
"""

import asyncio
import json
import sys

import api
import pricing as P
import strategies as S

PAGE = "web/index.html"
FIXTURE = "fixtures/daily_signal_2026-08-28.json"

# Points, and probabilities. The JS uses an erf approximation good to ~1.5e-7,
# so probabilities are compared loosely enough to allow that and nothing more.
TOL_PTS = 0.01
TOL_PROB = 1e-4


def cases():
    fx = json.load(open(FIXTURE))
    i = fx["inputs"]
    spot, vix, dte = i["spot"], i["vix"], i["days_to_expiry"]
    vixf = vix / 100.0 if vix > 1 else vix
    lot = fx["risk_sizing"].get("lot_size", 65)
    built = S.build_all(fx["chain"], spot, vixf, dte,
                        step=i.get("strike_step", 50.0), lot_size=lot, lots=1,
                        wing_width=fx["risk_sizing"].get("wing_width", 200.0),
                        margin_per_lot=fx["risk_sizing"].get("margin_per_lot", 120000.0))
    sigma = P.Volatility(spot, vixf, dte).expiry_stdev

    out = []
    for s in built.values():
        legs = [{"strike": l.strike, "kind": l.kind, "premium": l.premium,
                 "qty": (1 if l.long else -1)} for l in s.legs]
        out.append((s.name, legs, spot, sigma, lot, dte))

    # Shapes a user can build by clicking that no builder in strategies.py makes.
    atm = P.mround(spot, 50.0)
    out.append(("single short call",
                [{"strike": atm, "kind": "CE", "premium": 101.2, "qty": -1}],
                spot, sigma, lot, dte))
    out.append(("long put spread",
                [{"strike": atm, "kind": "PE", "premium": 97.0, "qty": 1},
                 {"strike": atm - 200, "kind": "PE", "premium": 31.5, "qty": -1}],
                spot, sigma, lot, dte))
    out.append(("ratio: two short calls, one long",
                [{"strike": atm + 100, "kind": "CE", "premium": 52.0, "qty": -2},
                 {"strike": atm + 300, "kind": "CE", "premium": 12.0, "qty": 1}],
                spot, sigma, lot, dte))
    out.append(("three lots straddle",
                [{"strike": atm, "kind": "CE", "premium": 101.2, "qty": -3},
                 {"strike": atm, "kind": "PE", "premium": 97.0, "qty": -3}],
                spot, sigma, lot, dte))
    return out


async def main():
    from playwright.async_api import async_playwright

    rows, bad = [], 0
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        pg = await b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("file://" + __import__("os").path.abspath(PAGE))
        await pg.wait_for_timeout(400)

        for name, legs, spot, sigma, lot, dte in cases():
            req = {"legs": legs, "spot": spot, "sigma": sigma,
                   "lot_size": lot, "dte": dte, "points": 241, "span": 0.10}
            js = await pg.evaluate("r => payoffLocal(r)", req)
            py = api.api_payoff(api.PayoffReq(**{**req,
                    "legs": [api.Leg(**l) for l in legs]}))

            d = {
                "credit":  abs(js["net_premium_pts"] - py["net_premium_pts"]),
                "maxP":    abs(js["max_profit_pts"] - py["max_profit_pts"]),
                "maxL":    abs(js["max_loss_pts"] - py["max_loss_pts"]),
                "payoff":  abs(js["expected_payoff_pts"] - py["expected_payoff_pts"]),
                "popN":    abs(js["pop_normal"] - py["pop_normal"]),
                "popLN":   abs(js["pop_lognormal"] - py["pop_lognormal"]),
            }
            be_js, be_py = js["breakevens"], py["breakevens"]
            d["be_n"] = abs(len(be_js) - len(be_py))
            d["be"] = (max(abs(a - b) for a, b in zip(sorted(be_js), sorted(be_py)))
                       if be_js and len(be_js) == len(be_py) else 0.0)
            same_unbounded = js["loss_unbounded"] == py["loss_unbounded"]

            ok = (d["credit"] <= TOL_PTS and d["maxP"] <= TOL_PTS
                  and d["maxL"] <= TOL_PTS and d["payoff"] <= TOL_PTS
                  and d["popN"] <= TOL_PROB and d["popLN"] <= TOL_PROB
                  and d["be_n"] == 0 and d["be"] <= 0.2 and same_unbounded)
            if not ok:
                bad += 1
            rows.append((name, d, same_unbounded, ok, py, js))

        await b.close()

    hdr = (f"{'structure':<34}{'credit':>8}{'maxP':>8}{'maxL':>8}{'payoff':>8}"
           f"{'POP':>10}{'POP-ln':>10}{'BE':>7}{'unbdd':>7}   ")
    print(hdr); print("-" * (len(hdr) + 4))
    for name, d, same_u, ok, py, js in rows:
        print(f"{name:<34}{d['credit']:>8.4f}{d['maxP']:>8.4f}{d['maxL']:>8.4f}"
              f"{d['payoff']:>8.4f}{d['popN']:>10.6f}{d['popLN']:>10.6f}"
              f"{d['be']:>7.2f}{'same' if same_u else 'DIFF':>7}   "
              f"{'ok' if ok else 'FAIL'}")

    print("\nlargest disagreement, over every case:")
    print(f"  points      {max(max(r[1]['credit'], r[1]['maxP'], r[1]['maxL'], r[1]['payoff']) for r in rows):.5f}")
    print(f"  probability {max(max(r[1]['popN'], r[1]['popLN']) for r in rows):.8f}")
    print(f"  breakeven   {max(r[1]['be'] for r in rows):.3f} pts (241-point grid)")

    if bad:
        print(f"\n{bad} case(s) FAILED -- the browser and the server disagree.")
        return 1
    print(f"\nAll {len(rows)} cases agree. The Builder shows the same numbers as "
          f"the Telegram signal.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Prove series_api against real collected parquet, BEFORE restarting the API.

Same doctrine as series_probe.py, one layer up. series_api is tested against
synthetic chains in series_api_probe.py; what that cannot test is the live
path -- api.resolve picking the session and expiry, the signature cache
actually finding the parquet parts, and the payload surviving json.dumps on
real numbers, where +inf from a contract that grew from zero is a live
possibility rather than a fixture.

Nothing here restarts anything or writes anything. It imports the same code
the endpoint will run and calls it directly.

    $PY series_api_live.py
    $PY series_api_live.py --underlying BANKNIFTY
"""

import json
import sys
import time

import series_api as SA
import series_probe as SP


def main():
    args = {"--underlying": "NIFTY", "--expiry": None}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]
    u = args["--underlying"]

    print(f"BARS resolved to: {SA.SP.BARS}")
    try:
        day, exp, dte = SA._resolve(u, args["--expiry"])
    except Exception as e:
        print(f"resolve failed: {type(e).__name__}: {e}")
        return 1
    print(f"session {day}   expiry {exp}   {dte} DTE   underlying {u}\n")

    # 1. The cache must agree with the proven uncached function, exactly.
    t0 = time.time()
    direct, err = SP.by_minute(day, u, exp)
    t_direct = time.time() - t0
    if err:
        print(f"by_minute failed: {err}")
        return 1

    t0 = time.time()
    cached1, _ = SA.by_minute_cached(day, u, exp)
    t_cold = time.time() - t0
    t0 = time.time()
    cached2, _ = SA.by_minute_cached(day, u, exp)
    t_warm = time.time() - t0

    same = (direct == cached1 == cached2)
    print(f"adapter      {len(direct)} minutes, "
          f"{len({k for m in direct.values() for k in m})} strikes")
    print(f"cache        identical to uncached: {same}")
    print(f"timing       uncached {t_direct:.2f}s   cold {t_cold:.2f}s   "
          f"warm {t_warm:.4f}s")
    if not same:
        print("\nFAIL: the cached adapter does not match series_probe.by_minute")
        return 1
    if t_warm > 0.05:
        print("note: warm lookup slower than expected; the signature may be "
              "changing every call (collector mid-flush is normal)")

    # 2. Both endpoints, against that real session.
    for name, call in (
        ("/api/series  straddle @ atm_open",
         lambda: SA.api_series(underlying=u, expiry=str(exp),
                               structure="straddle", anchor="atm_open")),
        ("/api/series  straddle @ atm_rolling",
         lambda: SA.api_series(underlying=u, expiry=str(exp),
                               structure="straddle", anchor="atm_rolling")),
        ("/api/series  strangle width 2",
         lambda: SA.api_series(underlying=u, expiry=str(exp),
                               structure="strangle", width=2)),
        ("/api/buildup both views",
         lambda: SA.api_buildup(underlying=u, expiry=str(exp))),
    ):
        print(f"\n--- {name}")
        try:
            out = call()
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: "
                  f"{getattr(e, 'detail', None) or e}")
            return 1
        try:
            blob = json.dumps(out)
        except (TypeError, ValueError) as e:
            print(f"  FAILED to serialise: {type(e).__name__}: {e}")
            return 1
        print(f"  ok, {len(blob):,} bytes of JSON")

        if "rows" in out and out.get("points"):
            r = out["rows"][-1]
            print(f"  last {r['ts']}  price {r['price']}  vwap {r['vwap']}  "
                  f"ma {r['ma']}  rsi {r['rsi']}")
            print(f"  indicators_valid={out['indicators_valid']}  "
                  f"strike_rolls={out['strike_rolls']}")
        if "leg" in out:
            a = out["leg"]["agreement"]
            print(f"  leg: {out['leg']['counted']} rows, "
                  f"forward_open {out['leg']['forward_open']}")
            print(f"       near-money  calls up {a['near']['calls_up_pct']}%  "
                  f"puts down {a['near']['puts_down_pct']}%")
            print(f"       whole chain calls up {a['chain']['calls_up_pct']}%  "
                  f"puts down {a['chain']['puts_down_pct']}%")
            print(f"       held out (no opening OI): "
                  f"{len(out['leg']['no_opening_reading'])}")
        if "otm" in out:
            o = out["otm"]
            fwd = (f", forward {o['forward_open']} -> {o['forward_last']} "
                   f"({o['forward_move']:+} pts)"
                   if o["forward_move"] is not None else "")
            print(f"  otm: {o['counted']} rows{fwd}")
            print(f"       median residual {o['median_residual']}  "
                  f"skipped {o['skipped']}")
            rs = [x["resid"] for x in o["rows"]
                  if isinstance(x["resid"], (int, float))]
            if rs:
                n = len(rs)
                lo = sorted(rs)[:max(1, n // 3)]
                hi = sorted(rs)[-max(1, n // 3):]
                print(f"       residual tilt: low third "
                      f"{sum(lo) / len(lo):+.2%}   high third "
                      f"{sum(hi) / len(hi):+.2%}")

    print("\nALL LIVE CHECKS PASSED -- safe to restart options-api\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

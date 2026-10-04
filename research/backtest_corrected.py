"""Re-run the signal over every collected session, with a spot that is right.

The live job asked Kite for a quote at 09:00:00 IST -- the first second of the
pre-open auction -- and got an index computed from a mostly empty basket. Four
of six signals were built on a level that was hundreds of points wrong, which
corrupted the ATM strike and therefore sigma, POP, EV and the gate.

None of that damage touches the data on disk. The chains are sound and the
level can be recovered from them, because index options are priced off the
FORWARD and put-call parity gives it with no volatility assumption and no
external price:

    F = K + C - P     exact, at every strike
    S = F * exp(-rT)  discounting back to spot

That is the same method that settled the 8 Sep expiry at 23,635.03 against an
actual close of 23,635.10 -- seven hundredths of a point. India VIX comes from
the collector's own INDEX partition for the same session. So the corrected
signal needs nothing that was not already collected: no Kite call, no network,
no trust in the broken quote.

Each session is then replayed against real minute bars through `position`, so
what comes out is not a projection. It is what these structures actually did.

DELIBERATELY NOT IMPORTING daily_signal. That module reaches for Telegram and
the journal at import time and is built to run once, live, at 09:00. A
backtest that depends on the live job breaks whenever the live job changes.
The chain construction below is the same logic, held separately on purpose.

Usage:
    python backtest_corrected.py
    python backtest_corrected.py --min-dte 3 --capture 0.85 --hard-dte 2
    python backtest_corrected.py --gate 0.80          loosen the entry gate
    python backtest_corrected.py --verify      spot check only, no replay
"""

import glob
import json
import os
import sys
from datetime import date, datetime, time as dtime
from math import exp

import pandas as pd

import models as M
import position as X
import pricing as P
import replay as R
import strategies as S
from position import CE, PE, ExitRule, Leg, Position

BARS = "data/bars_1m"
UNDERLYING = "NIFTY"
NIFTY_TOKEN = 256265
VIX_TOKEN = 264969

STRIKE_STEP = 50.0
WING_WIDTH = 200.0
SIGMA_MULT = 1.5
MARGIN_LOT = 120000.0
DEFAULT_LOT = 75
RISK_FREE = 0.065
CS_GATE = 0.9346                 # POP >= 0.65 on the ATM straddle
MAX_DTE = 12

SPREAD_ABS, SPREAD_REL = 2.0, 0.25
COSTS = P.CURRENT_COSTS   # one definition, in pricing.py -- see the history there


# ---------------------------------------------------------------- the chain

def sessions():
    out = []
    for p in sorted(glob.glob(os.path.join(BARS, "date=*"))):
        try:
            out.append(date.fromisoformat(os.path.basename(p).split("=")[1]))
        except ValueError:
            continue
    return out


def closing_bars(day):
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", f"underlying={UNDERLYING}", "*.parquet")))
    if not files:
        return None
    cols = ["ts", "instrument_token", "expiry", "strike", "opt_type",
            "close", "bid", "ask"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except (OSError, ValueError, KeyError):
            continue
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["ts"] = ts
    return (df.sort_values("ts")
              .groupby("instrument_token", as_index=False).last())


def quote_price(row):
    try:
        bid, ask = float(row["bid"]), float(row["ask"])
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = 0.5 * (bid + ask)
    if (ask - bid) > max(SPREAD_ABS, SPREAD_REL * mid):
        return None
    return mid


STALE_MINUTES = 5


def build_chain(bars, expiry, stale_minutes=STALE_MINUTES):
    """Strikes priced at mid, from quotes that are CONTEMPORANEOUS.

    `closing_bars` returns the last bar per instrument, and those bars are not
    all from the same minute -- an illiquid strike can stop printing at 14:40
    while the money is still trading at 15:29. Put-call parity across two legs
    quoted an hour apart is not parity, it is the index's move in between, and
    that showed up as up to 68 points of scatter in the recovered spot.

    So: anchor on the session's newest bar and drop any leg older than
    `stale_minutes`. Losing a few far strikes costs nothing -- parity is
    estimated near the money, where both legs carry real time value.
    """
    sub = bars[bars["expiry"].astype(str).str.slice(0, 10) == expiry.isoformat()]
    if sub.empty:
        return [], {"stale": 0, "kept": 0, "as_of": None}
    newest = pd.to_datetime(sub["ts"]).max()
    cutoff = newest - pd.Timedelta(minutes=stale_minutes)

    legs, stale = {}, 0
    for _, row in sub.iterrows():
        if pd.to_datetime(row["ts"]) < cutoff:
            stale += 1
            continue
        px = quote_price(row)
        if px is None:
            continue
        legs.setdefault(float(row["strike"]), {})[str(row["opt_type"])] = px

    chain = [{"strike": k, "ce": v["CE"], "pe": v["PE"]}
             for k, v in sorted(legs.items()) if "CE" in v and "PE" in v]
    return chain, {"stale": stale, "kept": len(chain),
                   "as_of": newest.to_pydatetime()}


# ------------------------------------------------- the spot, without a quote

def forward_unseeded(chain, n=5):
    """Put-call parity forward, with no spot to seed it.

    `models.forward_from_parity` needs a spot to pick the near strikes, which
    is circular when the spot is the thing in doubt. The strikes where both
    legs carry real time value are exactly those where |C - P| is smallest,
    so that picks the money without knowing where the money is.
    """
    rows = [r for r in chain if float(r["ce"]) > 0 and float(r["pe"]) > 0]
    if not rows:
        return None, None
    rows.sort(key=lambda r: abs(float(r["ce"]) - float(r["pe"])))
    fwds = sorted(float(r["strike"]) + float(r["ce"]) - float(r["pe"])
                  for r in rows[:n])
    spread = fwds[-1] - fwds[0]      # how much the near strikes disagree
    return fwds[len(fwds) // 2], spread


def spot_from_chain(chain, days_to_expiry, r=RISK_FREE):
    """Spot implied by the chain.

    `days_to_expiry` is measured from the moment the CHAIN was quoted, which
    is the previous session's close -- not from the signal morning. Using the
    signal day understates the carry by one day and biased every estimate
    about +4 points high.
    """
    f, spread = forward_unseeded(chain)
    if f is None:
        return None, None, None
    return f * exp(-r * max(days_to_expiry, 0) / 365.0), f, spread


def index_series(day, token):
    """(ts, close) for one index instrument across a whole session."""
    files = sorted(glob.glob(os.path.join(
        BARS, f"date={day}", "underlying=INDEX", "*.parquet")))
    if not files:
        return None
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(
                f, columns=["ts", "instrument_token", "close"]))
        except (OSError, ValueError, KeyError):
            continue
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df[df["instrument_token"] == token]
    if df.empty:
        return None
    ts = pd.to_datetime(df["ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.assign(ts=ts).sort_values("ts")
    return df


def index_close(day, token):
    df = index_series(day, token)
    return None if df is None else float(df["close"].iloc[-1])


def index_at(day, token, when):
    """The index at a specific minute -- the honest comparison.

    Comparing a forward implied by option quotes stamped 15:29 against an
    index print stamped somewhere else is not a measurement of the estimator,
    it is a measurement of whatever the market did in between. Five minutes
    of NIFTY is comfortably 40 points, which is the size of the error being
    chased.
    """
    df = index_series(day, token)
    if df is None or when is None:
        return None, None
    at = df[df["ts"] <= pd.Timestamp(when)]
    if at.empty:
        at = df
    return float(at["close"].iloc[-1]), at["ts"].iloc[-1].to_pydatetime()


def lot_for(day):
    for d in (day, date.today()):
        path = f"data/instruments/{d}.json"
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path))
            rows = data if isinstance(data, list) else data.get("instruments", [])
            for r in rows:
                if r.get("name") == UNDERLYING:
                    v = r.get("lot_size") or r.get("lotsize")
                    if v:
                        return int(v)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return DEFAULT_LOT


# ----------------------------------------------------------- one signal day

def signal_for(day, prev, min_dte, cs_gate=CS_GATE):
    """What the job SHOULD have produced on `day`, from `prev`'s close."""
    bars = closing_bars(prev)
    if bars is None or bars.empty:
        return {"status": "no bars"}

    expiries = sorted({date.fromisoformat(str(e)[:10])
                       for e in bars["expiry"].unique()})
    usable = [e for e in expiries if min_dte <= (e - day).days <= MAX_DTE]
    if not usable:
        return {"status": f"no expiry {min_dte}-{MAX_DTE} days out"}
    expiry = usable[0]
    dte = (expiry - day).days

    chain, cov = build_chain(bars, expiry)
    if len(chain) < 15:
        return {"status": f"thin chain ({len(chain)} strikes, "
                          f"{cov['stale']} stale)"}

    # THE SPOT. Not recovered, not quoted -- read from the collector's own
    # INDEX partition for the session the chain came from. The signal needs
    # "the previous session's close", and that is a number already on disk,
    # exact. Reconstructing it from parity was solving a problem that did
    # not exist; asking Kite for it at 09:00 was solving it wrongly.
    spot = index_close(prev, NIFTY_TOKEN)
    if spot is None:
        return {"status": "no NIFTY close on disk"}

    # Parity still earns its place, as a CROSS-CHECK rather than a source.
    # The forward and the spot must agree to within carry; when they do not,
    # something is wrong with the chain and the honest answer is to refuse
    # to signal rather than to pick a strike against a level nothing agrees
    # on. This is the guard the five bad signals needed.
    fwd, spread = forward_unseeded(chain)
    basis = None if fwd is None else fwd - spot
    vix = index_close(prev, VIX_TOKEN)
    if vix is None:
        return {"status": "no VIX on disk"}

    lot = lot_for(prev)
    structures = S.build_all(chain, spot, vix / 100.0, dte, step=STRIKE_STEP,
                             lot_size=lot, lots=1, wing_width=WING_WIDTH,
                             sigma_mult=SIGMA_MULT, margin_per_lot=MARGIN_LOT,
                             costs=COSTS)
    vol = P.Volatility(spot, vix / 100.0, dte)
    straddle = structures["short_straddle"].credit_pts
    cs = straddle / vol.expiry_stdev if vol.expiry_stdev else 0.0

    best = S.best(structures)
    if cs < cs_gate:
        return {"status": "gate shut", "spot": spot, "fwd": fwd, "vix": vix,
                "cs": cs, "expiry": expiry, "dte": dte, "basis": basis}
    if best is None:
        return {"status": "no positive EV", "spot": spot, "fwd": fwd,
                "vix": vix, "cs": cs, "expiry": expiry, "dte": dte,
                "basis": basis}
    return {"status": "ok", "spot": spot, "fwd": fwd, "vix": vix, "cs": cs,
            "expiry": expiry, "dte": dte, "best": best, "lot": lot,
            "basis": basis, "spread": spread, "structures": structures}


def legs_of(st):
    legs = [Leg(float(st.ce_strike), CE, -1, float(st.ce_premium)),
            Leg(float(st.pe_strike), PE, -1, float(st.pe_premium))]
    if st.long_ce_strike is not None:
        legs.append(Leg(float(st.long_ce_strike), CE, 1,
                        float(st.long_ce_premium)))
    if st.long_pe_strike is not None:
        legs.append(Leg(float(st.long_pe_strike), PE, 1,
                        float(st.long_pe_premium)))
    return tuple(legs)


def replay_signal(day, sig, rule):
    st = sig["best"]
    legs = legs_of(st)
    wanted = {l.key() for l in legs}
    pos = Position(legs=legs, lot_size=sig["lot"], lots=1,
                   entry_ts=datetime.combine(day, R.ENTRY_MARK),
                   expiry=sig["expiry"], rule=rule, label=st.name)

    slip = None
    last_ts = last_value = None
    for ts, prices, dte in R.price_stream(day, sig["expiry"], wanted):
        if slip is None:
            slip = pos.value_pts(prices) - pos.credit_pts
        if pos.is_open:
            pos.step(ts, prices, dte)
        last_ts, last_value = ts, pos.value_pts(prices)

    if last_ts is None:
        return None
    settled = last_ts.date() >= sig["expiry"]
    hold = pos.credit_pts - last_value
    return {"pos": pos, "exit": pos.exit, "slip": slip, "hold": hold,
            "settled": settled}


# -------------------------------------------------------------------- main

def main():
    min_dte, capture, hard_dte, cs_gate = 3, 0.85, 2, CS_GATE
    verify_only = "--verify" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--min-dte":
            min_dte = int(sys.argv[i + 1])
        if a == "--capture":
            capture = float(sys.argv[i + 1])
        if a == "--hard-dte":
            hard_dte = int(sys.argv[i + 1])
        if a == "--gate":
            cs_gate = float(sys.argv[i + 1])
    rule = ExitRule(capture=capture, hard_exit_dte=hard_dte)

    days = sessions()
    if len(days) < 2:
        print("need at least two collected sessions")
        return

    # ------------------------------------------------ does the spot hold up
    print("STEP 1 -- WHERE THE SPOT COMES FROM\n")
    print("The signal uses NIFTY 50's own collected close (token 256265) for")
    print("the session the chain came from. Exact, on disk, no Kite call.")
    print()
    print("Put-call parity is shown beside it as a CROSS-CHECK. The two")
    print("should agree to within carry; where they do not, the chain and")
    print("the index disagree about where the market is and the honest")
    print("response is to refuse to signal.\n")
    h = (f"{'session':<12}{'chain':>7}{'parity fwd':>11}{'implied':>13}"
         f"{'idx close':>13}{'err':>8}{'idx@chain':>13}{'err':>9}"
         f"{'spread':>8}")
    print(h)
    print("-" * len(h))
    errs, aligned_errs = [], []
    for prev, day in zip(days, days[1:]):
        bars = closing_bars(prev)
        if bars is None:
            continue
        exps = sorted({date.fromisoformat(str(e)[:10])
                       for e in bars["expiry"].unique()})
        use = [e for e in exps if min_dte <= (e - day).days <= MAX_DTE]
        if not use:
            continue
        ch, cov = build_chain(bars, use[0])
        if len(ch) < 15:
            continue
        sp, fw, spread = spot_from_chain(ch, (use[0] - prev).days)
        at_close = index_close(prev, NIFTY_TOKEN)
        aligned, idx_ts = index_at(prev, NIFTY_TOKEN, cov["as_of"])
        if sp is None or at_close is None or aligned is None:
            continue
        errs.append(sp - at_close)
        aligned_errs.append(sp - aligned)
        chain_t = cov["as_of"].strftime("%H:%M") if cov["as_of"] else "  -  "
        print(f"{str(prev):<12}{chain_t:>7}{fw:>11,.2f}{sp:>13,.2f}"
              f"{at_close:>13,.2f}{sp - at_close:>8.1f}"
              f"{aligned:>13,.2f}{sp - aligned:>9.1f}{spread:>8.1f}")
    if errs:
        print("-" * len(h))
        print(f"against the index CLOSE      mean {sum(errs)/len(errs):+7.1f}"
              f"   worst {max(abs(e) for e in errs):6.1f}")
        if aligned_errs:
            print(f"against the index AT THAT MINUTE  "
                  f"mean {sum(aligned_errs)/len(aligned_errs):+7.1f}"
                  f"   worst {max(abs(e) for e in aligned_errs):6.1f}")
            print()
            print("If the second line is far tighter, the estimator was")
            print("always fine and the first comparison was sloppy -- the")
            print("chain and the index print were simply from different")
            print("moments. If both are equally loose, the basis is not the")
            print("6.5% carry assumed here and has to be calibrated, not")
            print("guessed.")
        print()
        print("`spread` is how far the five near-the-money strikes disagree")
        print("with each other about the forward -- the estimator's own noise,")
        print("independent of whether it is right. `stale` counts legs dropped")
        print("for being quoted more than 5 minutes before the session's last")
        print("bar; those were the main source of the earlier scatter.")
        print()
        print("Compare with the live quote, which was wrong by up to 540.")
    if verify_only:
        return

    # ------------------------------------------------------ the corrected run
    print(f"\n\nSTEP 2 -- THE SIGNAL, REBUILT AND REPLAYED\n")
    print(f"min DTE {min_dte}, gate {cs_gate:.4f}, exit at "
          f"{capture:.0%} of credit, hard exit DTE {hard_dte}\n")
    h2 = (f"{'session':<12}{'expiry':<12}{'dte':>4}{'spot':>10}{'c/sig':>7}"
          f"  {'structure':<18}{'credit':>8}{'slip':>7}{'exit':>8}"
          f"{'P&L pts':>9}{'hold':>8}{'MAE':>7}")
    print(h2)
    print("-" * len(h2))

    rule_rs = hold_rs = 0.0
    taken = settled = 0
    for prev, day in zip(days, days[1:]):
        sig = signal_for(day, prev, min_dte, cs_gate)
        if sig["status"] != "ok":
            extra = ""
            if "cs" in sig:
                extra = f"  (c/sigma {sig['cs']:.3f})"
            print(f"{str(day):<12}{'':<12}{'':>4}{'':>10}{'':>7}  "
                  f"{sig['status']}{extra}")
            continue
        taken += 1
        r = replay_signal(day, sig, rule)
        if r is None:
            print(f"{str(day):<12}{str(sig['expiry']):<12}"
                  f"{sig['dte']:>4}{sig['spot']:>10,.0f}{sig['cs']:>7.3f}"
                  f"  {sig['best'].name:<18}  no bars to replay")
            continue
        pos, e = r["pos"], r["exit"]
        st = sig["best"]
        if not r["settled"]:
            print(f"{str(day):<12}{str(sig['expiry']):<12}{sig['dte']:>4}"
                  f"{sig['spot']:>10,.0f}{sig['cs']:>7.3f}  {st.name:<18}"
                  f"{pos.credit_pts:>8.1f}{r['slip']:>7.1f}{'OPEN':>8}"
                  f"{'':>9}{'':>8}{pos.mae_pts:>7.0f}")
            continue
        settled += 1
        rp = e.pnl_pts if e else r["hold"]
        rule_rs += rp * pos.lot_size
        hold_rs += r["hold"] * pos.lot_size
        print(f"{str(day):<12}{str(sig['expiry']):<12}{sig['dte']:>4}"
              f"{sig['spot']:>10,.0f}{sig['cs']:>7.3f}  {st.name:<18}"
              f"{pos.credit_pts:>8.1f}{r['slip']:>7.1f}"
              f"{(e.reason if e else 'expiry'):>8}{rp:>9.1f}"
              f"{r['hold']:>8.1f}{pos.mae_pts:>7.0f}")

    print("-" * len(h2))
    print(f"{taken} signals passed the gate, {settled} have settled\n")
    if settled:
        print(f"  exit rule        Rs {rule_rs:>12,.0f}")
        print(f"  held to expiry   Rs {hold_rs:>12,.0f}")
        print(f"  difference       Rs {rule_rs - hold_rs:>12,.0f}")
    print()
    print("Gross of costs. Independent expiries, not rows, are the sample")
    print("size that counts -- signals entered on different days into the")
    print("same Friday are one test, not several.")


if __name__ == "__main__":
    main()

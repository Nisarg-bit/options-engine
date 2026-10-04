"""The signal journal -- what was signalled, and what actually happened.

Two halves:

  log()    called by signal.py each morning. Appends every structure, not
           just the recommended one, so the record can answer "would the
           condor have done better" without re-running anything.

  score()  called after the close. Fills in the outcome of any logged signal
           whose expiry has passed.

  report() the calibration table: modelled POP against realised hit rate,
           modelled net EV against realised P&L.

That last one is the point of the whole exercise. Your workbook's B54 has
read "insufficient sample" since the day it was built, because a spreadsheet
that gets overwritten every morning cannot accumulate a record. This can.

Settlement level comes from your own expiry-day bars via put-call parity
(C - P = S - K holds exactly at expiry, whatever the time value), falling
back to Kite's daily close. Which source was used is recorded per row --
never silently substituted.

Usage:
    python journal.py score      fill in outcomes for expired signals
    python journal.py report     print the calibration table
"""

import glob
import os
import sys
from datetime import date, datetime, timezone

import pandas as pd

PATH = "data/journal/signals.csv"
NIFTY_INDEX_TOKEN = 256265

FIELDS = [
    # --- identity
    "signal_date", "session", "expiry", "dte", "structure",
    # --- market state at signal time
    "spot", "forward", "vix", "sigma", "straddle", "fair", "ratio", "c_over_sigma",
    # --- the structure
    "ce_strike", "pe_strike", "long_ce_strike", "long_pe_strike",
    "ce_premium", "pe_premium", "long_ce_premium", "long_pe_premium",
    "credit_pts", "credit_rs", "lot", "lots", "defined_risk",
    "lower_be", "upper_be", "pop", "exp_payoff_pts",
    "gross_ev_rs", "fees_rs", "slippage_rs", "net_ev_rs", "margin",
    # --- decision
    "gate_passed", "recommended",
    # --- outcome, filled by score()
    "final_level", "level_source", "settle_pts",
    "pnl_rs", "pnl_per_unit", "inside_zone", "scored_at",
]


# Outcome columns that hold something other than a number. Until the first
# signal is scored they are empty in the CSV, and pandas reads an all-empty
# column as float64 -- after which writing "parity/own-bars" into it raises
# `Invalid value for dtype float64` and takes the whole scoring run down. The
# settlement had already been computed correctly at that point, which is the
# worst kind of failure: the hard part worked and the result was thrown away.
TEXTY = ("level_source", "scored_at", "inside_zone")


def _load():
    if not os.path.exists(PATH):
        return pd.DataFrame(columns=FIELDS)
    df = pd.read_csv(PATH)
    for c in FIELDS:
        if c not in df.columns:
            df[c] = None
    for c in ("signal_date", "session", "expiry"):
        df[c] = pd.to_datetime(df[c]).dt.date
    # Force them to object up front rather than at the point of assignment, so
    # every caller gets a frame that can be written to, not just score().
    for c in TEXTY:
        df[c] = df[c].astype("object")
    return df[FIELDS]


def _save(df):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    df.to_csv(PATH, index=False)


# --------------------------------------------------------------------- log

def log(ctx, structures, best):
    """Append one row per structure for today's signal. Idempotent."""
    df = _load()
    sig_date = ctx["today"]
    if len(df) and (df["signal_date"] == sig_date).any():
        print(f"journal: {sig_date} already logged, skipping")
        return

    rows = []
    for s in structures.values():
        rows.append({
            "signal_date": sig_date, "session": ctx["session"],
            "expiry": ctx["expiry"], "dte": ctx["dte"], "structure": s.name,
            "spot": round(ctx["spot"], 2), "forward": round(ctx["forward"], 2),
            "vix": ctx["vix"], "sigma": round(ctx["sigma"], 2),
            "straddle": round(ctx["straddle"], 2), "fair": round(ctx["fair"], 2),
            "ratio": round(ctx["ratio"], 4),
            "c_over_sigma": round(ctx["c_over_sigma"], 4),
            "ce_strike": s.ce_strike, "pe_strike": s.pe_strike,
            "long_ce_strike": s.long_ce_strike, "long_pe_strike": s.long_pe_strike,
            "ce_premium": round(s.ce_premium, 2), "pe_premium": round(s.pe_premium, 2),
            "long_ce_premium": round(s.long_ce_premium, 2),
            "long_pe_premium": round(s.long_pe_premium, 2),
            "credit_pts": round(s.credit_pts, 2), "credit_rs": round(s.credit_rs, 2),
            "lot": s.lot_size, "lots": s.lots, "defined_risk": s.defined_risk,
            "lower_be": round(s.lower_be, 2), "upper_be": round(s.upper_be, 2),
            "pop": round(s.pop, 6), "exp_payoff_pts": round(s.exp_payoff_pts, 4),
            "gross_ev_rs": round(s.gross_ev_rs, 2), "fees_rs": round(s.fees_rs, 2),
            "slippage_rs": round(s.slippage_rs, 2),
            "net_ev_rs": round(s.net_ev_rs, 2), "margin": round(s.margin, 2),
            "gate_passed": best is not None,
            "recommended": best is not None and s.name == best.name,
            "final_level": None, "level_source": None, "settle_pts": None,
            "pnl_rs": None, "pnl_per_unit": None, "inside_zone": None,
            "scored_at": None,
        })
    _save(pd.concat([df, pd.DataFrame(rows)], ignore_index=True)[FIELDS])
    print(f"journal: logged {len(rows)} structures for {sig_date}")


# ------------------------------------------------------------------- score

def implied_level(chain):
    """Spot implied by put-call parity -- needs no external price.

    F = K + C - P holds at EVERY strike, with no volatility assumption. The
    estimate is most stable where both legs carry real value, which at expiry
    is where |C - P| is smallest. Median of the five nearest such strikes, so
    one bad quote cannot move it.
    """
    usable = [r for r in chain if r["ce"] > 0 and r["pe"] > 0]
    if not usable:
        return None
    near = sorted(usable, key=lambda r: abs(r["ce"] - r["pe"]))[:5]
    vals = sorted(r["strike"] + r["ce"] - r["pe"] for r in near)
    return vals[len(vals) // 2]


def _expiry_chain(expiry):
    """Closing chain on the expiry date, from our own bars."""
    files = glob.glob(f"data/bars_1m/date={expiry}/underlying=NIFTY/*.parquet")
    if not files:
        return []
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df[df["expiry"] == expiry]
    if df.empty:
        return []
    df = df.sort_values("ts").groupby("instrument_token", as_index=False).last()

    legs = {}
    for _, r in df.iterrows():
        bid, ask = float(r["bid"]), float(r["ask"])
        px = 0.5 * (bid + ask) if bid > 0 and ask > 0 and ask >= bid else float(r["close"])
        if px > 0:
            legs.setdefault(float(r["strike"]), {})[r["opt_type"]] = px
    return [{"strike": k, "ce": v["CE"], "pe": v["PE"]}
            for k, v in sorted(legs.items()) if "CE" in v and "PE" in v]


def settlement(expiry, kite=None):
    """(level, source) for one expiry. Own data first, Kite as fallback."""
    lvl = implied_level(_expiry_chain(expiry))
    if lvl:
        return lvl, "parity/own-bars"
    if kite is not None:
        try:
            c = kite.historical_data(NIFTY_INDEX_TOKEN, expiry, expiry, "day")
            if c:
                # NSE settles on the last half-hour's average, not the close.
                # A few points adrift; fine for calibration, recorded as such.
                return float(c[-1]["close"]), "kite-daily-close"
        except Exception as e:
            print("kite historical failed:", e)
    return None, None


def _intrinsic(level, strike, kind):
    return max(0.0, level - strike) if kind == "CE" else max(0.0, strike - level)


def score():
    df = _load()
    if df.empty:
        print("journal: nothing logged yet")
        return

    today = datetime.now(timezone.utc).date()
    todo = df[df["pnl_rs"].isna() & (df["expiry"] < today)]
    if todo.empty:
        print("journal: nothing to score")
        return

    kite = None
    levels = {}
    for expiry in sorted(todo["expiry"].unique()):
        lvl, src = settlement(expiry, kite)
        if lvl is None and kite is None:
            try:
                import session
                kite = session.get_kite()
                lvl, src = settlement(expiry, kite)
            except Exception as e:
                print(f"could not reach Kite for {expiry}: {e}")
        if lvl is None:
            print(f"  {expiry}  no settlement level available, leaving unscored")
            continue
        levels[expiry] = (lvl, src)
        print(f"  {expiry}  settled {lvl:,.2f}  ({src})")

    scored = 0
    for i, r in todo.iterrows():
        if r["expiry"] not in levels:
            continue
        lvl, src = levels[r["expiry"]]

        settle = (_intrinsic(lvl, r["ce_strike"], "CE")
                  + _intrinsic(lvl, r["pe_strike"], "PE"))
        if bool(r["defined_risk"]):
            settle -= _intrinsic(lvl, r["long_ce_strike"], "CE")
            settle -= _intrinsic(lvl, r["long_pe_strike"], "PE")

        gross = (r["credit_pts"] - settle) * r["lot"] * r["lots"]
        pnl = gross - r["fees_rs"] - r["slippage_rs"]

        df.loc[i, "final_level"] = round(lvl, 2)
        df.loc[i, "level_source"] = src
        df.loc[i, "settle_pts"] = round(settle, 2)
        df.loc[i, "pnl_rs"] = round(pnl, 2)
        df.loc[i, "pnl_per_unit"] = round(pnl / (r["lot"] * r["lots"]), 4)
        df.loc[i, "inside_zone"] = bool(r["lower_be"] < lvl < r["upper_be"])
        df.loc[i, "scored_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        scored += 1

    _save(df)
    print(f"journal: scored {scored} rows")


# ------------------------------------------------------------------ report

def report():
    df = _load()
    done = df[df["pnl_rs"].notna()].copy()
    if done.empty:
        n = df["signal_date"].nunique() if not df.empty else 0
        print(f"journal: {n} signals logged, none scored yet")
        return

    print(f"\n{done['signal_date'].nunique()} scored signals   "
          f"{done['signal_date'].min()} -> {done['signal_date'].max()}\n")

    hdr = (f"{'structure':<20}{'n':>4}{'POP':>8}{'hit%':>8}{'gap':>8}"
           f"{'EV/unit':>10}{'real/unit':>11}{'gap':>9}")
    print(hdr)
    print("-" * len(hdr))
    for name, g in done.groupby("structure"):
        pop = g["pop"].mean() * 100
        hit = g["inside_zone"].astype(bool).mean() * 100
        ev = (g["net_ev_rs"] / (g["lot"] * g["lots"])).mean()
        real = g["pnl_per_unit"].mean()
        print(f"{name:<20}{len(g):>4}{pop:>8.1f}{hit:>8.1f}{hit-pop:>+8.1f}"
              f"{ev:>10.2f}{real:>11.2f}{real-ev:>+9.2f}")

    rec = done[done["recommended"].astype(bool)]
    if len(rec):
        pop, hit = rec["pop"].mean() * 100, rec["inside_zone"].astype(bool).mean() * 100
        print(f"\nRECOMMENDED TRADES ONLY  n={len(rec)}")
        print(f"  modelled POP {pop:.1f}%   realised {hit:.1f}%   "
              f"gap {hit-pop:+.1f} points")
        print(f"  modelled net EV {rec['net_ev_rs'].mean():,.0f}   "
              f"realised {rec['pnl_rs'].mean():,.0f} Rs/trade")
        if len(rec) < 20:
            print(f"  -- {20-len(rec)} more needed before this means anything "
                  f"(the workbook's own Setup!B66 threshold)")

    print("""
HOW TO READ THIS
  gap (POP)   realised hit rate minus modelled. Positive means the model is
              pessimistic, negative means overconfident. Daily_Signal!B57
              warns about the second; this is where you would see it.
  gap (EV)    realised P&L per unit minus modelled EV per unit. The number
              that says whether the whole edifice predicts anything.

Both need a real sample. Twenty scored signals is the floor, a quarter
including a volatility spike is the honest bar.""")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "score":
        score()
    elif cmd == "report":
        report()
    else:
        sys.exit("usage: python journal.py [score|report]")

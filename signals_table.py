"""Every signal the system has issued: what it predicted, what happened.

One row per signal day, showing the recommended structure only -- the other six
are in the journal for later comparison, but this is the trade the system would
actually have had you take.

Two things it does that the calibration table does not:

  * It separates OPEN positions from settled ones. A signal whose expiry has
    not arrived is not a miss, and averaging it in as one is how a record
    starts lying to you.

  * It counts INDEPENDENT EXPIRIES, not rows. Four signals entered on four
    different days that all settle on the same Friday share one market
    outcome. They are four trades but one test, and the twenty-signal floor
    means twenty tests. This is the same episode-counting problem study.py
    hit in August; counting rows overstated the sample there too.

Usage:  python signals_table.py
"""

import os
import pandas as pd

PATH = "data/journal/signals.csv"


def main():
    if not os.path.exists(PATH):
        print("no journal yet")
        return
    j = pd.read_csv(PATH)
    rec = j[j["recommended"].astype(str).str.lower() == "true"].copy()
    if rec.empty:
        print("no recommended signals logged")
        return
    rec = rec.sort_values("signal_date")

    hdr = (f"{'signal':<11}{'expiry':<11}{'dte':>4}  {'structure':<18}"
           f"{'sell CE':>8}{'sell PE':>8}{'credit':>8}{'POP':>7}"
           f"{'EV Rs':>9}{'settled':>10}{'inside':>8}{'P&L Rs':>10}")
    print(hdr)
    print("-" * len(hdr))

    for _, r in rec.iterrows():
        open_ = pd.isna(r["pnl_rs"])
        settled = "  open" if open_ else f"{r['final_level']:>10,.0f}"
        inside = ("     –" if open_ else
                  ("   yes" if str(r["inside_zone"]).lower() == "true" else "    NO"))
        pnl = "         –" if open_ else f"{r['pnl_rs']:>10,.0f}"
        print(f"{str(r['signal_date']):<11}{str(r['expiry']):<11}{int(r['dte']):>4}  "
              f"{r['structure']:<18}{r['ce_strike']:>8,.0f}{r['pe_strike']:>8,.0f}"
              f"{r['credit_pts']:>8.1f}{r['pop']*100:>6.1f}%{r['net_ev_rs']:>9,.0f}"
              f"{settled}{inside:>8}{pnl}")

    done = rec[rec["pnl_rs"].notna()]
    openr = rec[rec["pnl_rs"].isna()]
    print()
    print(f"{len(rec)} signals   {len(done)} settled   {len(openr)} still open")

    if len(done):
        hits = (done["inside_zone"].astype(str).str.lower() == "true").sum()
        print(f"\nSETTLED ONLY")
        print(f"  predicted POP (average)   {done['pop'].mean()*100:>7.1f}%")
        print(f"  actually finished inside  {hits/len(done)*100:>7.1f}%   "
              f"({hits} of {len(done)})")
        print(f"  modelled EV               {done['net_ev_rs'].mean():>7,.0f} Rs/trade")
        print(f"  realised                  {done['pnl_rs'].mean():>7,.0f} Rs/trade")
        print(f"  total realised            {done['pnl_rs'].sum():>7,.0f} Rs")

        # The number that decides whether any of the above means anything.
        ind_done = done["expiry"].nunique()
        ind_all = rec["expiry"].nunique()
        print(f"\nSAMPLE")
        print(f"  rows settled              {len(done)}")
        print(f"  INDEPENDENT expiries      {ind_done}   <- this is the one that counts")
        print(f"  independent expiries once everything open settles: {ind_all}")
        print(f"  floor is 20 independent expiries; "
              f"{max(0, 20-ind_done)} to go")
        if len(done) > ind_done:
            print(f"\n  {len(done)} settled rows share only {ind_done} settlement "
                  f"level(s). Signals entered on\n  different days into the same "
                  f"expiry are separate trades but ONE test of\n  the model -- they "
                  f"all live or die on the same closing price.")
    else:
        print("\nNothing settled yet. First outcome lands when the earliest "
              "expiry above passes.")


if __name__ == "__main__":
    main()

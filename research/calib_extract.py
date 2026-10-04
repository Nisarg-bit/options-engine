"""Step 1 of the POP calibration: pull NIFTY index options out of every cached
bhavcopy into one small file, data/calib/nifty_opts.parquet.

    python calib_extract.py
"""
import glob, os
import pandas as pd

OUT = "data/calib/nifty_opts.parquet"
COLS = ["TradDt", "TckrSymb", "FinInstrmTp", "XpryDt", "StrkPric", "OptnTp",
        "ClsPric", "SttlmPric", "UndrlygPric", "TtlTradgVol", "OpnIntrst"]


PARTS = "data/calib/parts"


def main(budget_s=150):
    """Resumable: one small parquet per bhavcopy day, skipped once written,
    then everything concatenated. Re-run until it prints 'wrote'."""
    import time
    t0 = time.time()
    os.makedirs(PARTS, exist_ok=True)
    files = sorted(glob.glob("data/bhavcopy/*.csv"))
    todo = [f for f in files if not os.path.exists(
        os.path.join(PARTS, os.path.basename(f).replace(".csv", ".parquet")))]
    for f in todo:
        if time.time() - t0 > budget_s:
            print(f"{len(todo)} left at start; stopped on budget -- run again", flush=True)
            return
        d = pd.read_csv(f, usecols=COLS)
        d = d[(d.TckrSymb == "NIFTY") & (d.FinInstrmTp.isin(["IDO", "IDF"]))]
        d.drop(columns=["TckrSymb"]).to_parquet(
            os.path.join(PARTS, os.path.basename(f).replace(".csv", ".parquet")))
    out = pd.concat([pd.read_parquet(p) for p in sorted(glob.glob(PARTS + "/*.parquet"))],
                    ignore_index=True)
    out.to_parquet(OUT)
    print("wrote", OUT, out.shape)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Phase A helper, run on the server. Usage: bash stocks_check.sh build | load
cd /home/ubuntu
PY=/home/ubuntu/.venv/bin/python; [ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python
D=$(TZ=Asia/Kolkata date +%F)
if [ "$1" = "build" ]; then
  "$PY" instruments.py 2>&1 | tail -9
  "$PY" - <<'PYEOF'
import json
d = json.load(open("data/sectors.json"))
from collections import Counter
c = Counter(v["sector"] for v in d["map"].values())
print("sector map:", d["count"], "symbols; failed lists:", d["failed"] or "none")
print("biggest sectors:", ", ".join(f"{k} {v}" for k, v in c.most_common(8)))
PYEOF
fi
if [ "$1" = "load" ]; then
  echo "== memory"; free -m
  echo "== collector process"; ps -o pid,rss,pcpu,etime,cmd -C python3 -C python | grep -i collector
  echo "== stock files today ($D)"
  for p in EQ STKFUT; do n=$(ls data/stocks_1m/date=$D/underlying=$p 2>/dev/null | wc -l); echo "  $p: $n parts"; done
  echo "== option files today"; ls data/bars_1m/date=$D/*/ 2>/dev/null | grep -c parquet
  "$PY" - <<PYEOF
import glob, pyarrow.parquet as pq
for p in ("EQ", "STKFUT"):
    fs = sorted(glob.glob(f"data/stocks_1m/date=$D/underlying={p}/*.parquet"))
    if not fs: print(f"  {p}: no data"); continue
    t = pq.read_table(fs[-1]).to_pandas()
    print(f"  {p}: last part {t.ts.max()} rows {len(t)} symbols {t.underlying.nunique()} interpolated {int(t.is_interpolated.sum())}")
PYEOF
  echo "== warnings/errors today"; journalctl -u options-collector --since today --no-pager | grep -ciE "warning|error|traceback"
  journalctl -u options-collector --since today --no-pager | grep -iE "warning|error|traceback" | tail -5
  echo "== last minute lines"; journalctl -u options-collector -n 3 --no-pager
fi
if [ "$1" = "pulse" ]; then
  timeout 180 "$PY" - <<'PYEOF'
import pulse_api
o = pulse_api.api_pulse()
print("pulse session:", o["session"], "| breadth:", o["breadth"].get("available"), "| mood:", o["mood"].get("score"), o["mood"].get("label"))
print("indices:", {k: v.get("last") for k, v in o.get("indices", {}).items()})
try:
    import fii_dii, threading
    th = threading.Thread(target=lambda: fii_dii.fetch(force=True), daemon=True)
    th.start(); th.join(40)
    if th.is_alive():
        print("fii/dii: NSE did not answer within 40 s (the site keeps retrying in the background)")
    x = fii_dii.summary(fii_dii.load())
    print("fii/dii:", x.get("date"), (x.get("cash") or {}).get("fii"), "| F&O OI:", bool(x.get("fii_fo")), x.get("reason", ""))
except Exception as e:
    print("fii/dii FAILED:", type(e).__name__, e)
try:
    import options_view_api
    v = options_view_api.api_options_view()
    for u, x in v["indices"].items():
        print("options view", u, ":", x.get("call") or x.get("reason"), x.get("odds"), [s["key"] for s in x.get("strategies", [])])
except Exception as e:
    print("options view FAILED:", type(e).__name__, e)
try:
    import paper_book_api, paper_book
    s = paper_book.summary(paper_book.load())
    print("paper book:", {b: (x["equity"], x["trades"], len(x["open"])) for b, x in s["books"].items()})
except Exception as e:
    print("paper book FAILED:", type(e).__name__, e)
PYEOF
fi

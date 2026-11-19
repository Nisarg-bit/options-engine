#!/usr/bin/env bash
# Read-only check of the stock-futures system on the server. Changes nothing.
#   bash ~/futures_check.sh          during or after a session
# Shows: services, memory, worker footprint, today's NIFTY-futures (VWAP source) bars,
# the instrument master's futures fields, feed health, latest scan per mode, positions,
# today's summary and recent worker errors.
cd /home/ubuntu
PY=/home/ubuntu/.venv/bin/python; [ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python
D=$(TZ=Asia/Kolkata date +%F)
echo "== $(TZ=Asia/Kolkata date '+%a %d %b %H:%M IST')"
echo "== services"
for s in options-collector options-api options-publisher options-futures; do printf '  %-20s %s\n' "$s" "$(systemctl is-active $s)"; done
echo "== memory"; free -m
echo "== worker process"
ps -o pid,rss,pcpu,etime,cmd -C python -C python3 | grep -e sf_cli -e PID | cut -c1-110
systemctl show options-futures -p MemoryCurrent -p NRestarts 2>/dev/null | sed 's/^/  /'
echo "== NIFTY futures bars today (VWAP source)"
n=$(ls data/stocks_1m/date=$D/underlying=IDXFUT 2>/dev/null | wc -l); echo "  IDXFUT parts: $n"
"$PY" - <<'PYEOF'
import glob, json, os
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
day = datetime.now(IST).date().isoformat()
fs = sorted(glob.glob(f"data/stocks_1m/date={day}/underlying=IDXFUT/*.parquet"))
if fs:
    import pyarrow.parquet as pq
    t = pq.read_table(fs[-1]).to_pandas()
    print(f"  last IDXFUT bar {t.ts.max()} | contract {sorted(set(t.tradingsymbol))} | volume {int(t.volume.sum())}")
m = sorted(glob.glob("data/instruments/*.json"))
if m:
    d = json.load(open(m[-1]))
    f = [r for r in d["instruments"] if r.get("kind") == "stock_fut"]
    x = [r["tradingsymbol"] for r in d["instruments"] if r.get("kind") == "index_fut"]
    print(f"== master {d['date']}: {len(f)} stock futures, {sum(1 for r in f if r.get('tick_size'))} with tick_size, NIFTY future: {x or 'MISSING'}")
p = "data/futures/state.json"
if not os.path.exists(p):
    print("== no futures snapshot yet (worker not started or not written)")
    raise SystemExit
s = json.load(open(p))
age = (datetime.now(IST) - datetime.fromisoformat(s["generated"])).total_seconds()
print(f"== snapshot {s['generated']} ({int(age)}s old) | config v{s['config_version']} | rules_validated {s.get('rules_validated')}")
print("  entry gate:", s.get("live_entry_gate") or "OPEN (paper entries allowed)")
print("  feeds:", ", ".join(f"{k}={v}" for k, v in ((s.get("feeds") or {}).get("feeds") or {}).items()))
for mode, pr in s["profiles"].items():
    sc = pr.get("last_scan") or {}
    reg = sc.get("regime") or {}
    ev = reg.get("evidence") or {}
    print(f"== {mode}: paused={pr['paused']} last scan {sc.get('scan_id')} {sc.get('status')} regime {reg.get('regime')} ({ev.get('readiness', '')})")
    if ev.get("reason"):
        print("   ", ev["reason"])
    if sc.get("no_trade_reason"):
        print("    no trade:", sc["no_trade_reason"][:220])
    cands = ((sc.get("candidates") or {}).get("candidates") or [])[:5]
    if cands:
        print("    top:", ", ".join(f"{c['symbol']} {c['direction']} {c['score']:+.2f}" for c in cands))
pos = s.get("positions") or []
print(f"== positions: {len(pos)}")
for q in pos:
    print(f"   {q['mode']:<8} {q['direction']:<5} {q.get('tradingsymbol')} {q['status']} entry {q.get('entry_avg')} mark {q.get('mark')} "
          f"stop {q.get('stop_price')} target {q.get('target_price')} mtm {q.get('mtm')} {q.get('data_flag') or ''} {(q.get('unresolved') or {}).get('reason', '')}")
d = s.get("daily_summary") or {}
if d:
    print(f"== summary {d.get('session')}: {d.get('text', '').splitlines()[1:4]}")
a = s["summary"]["all"]
print(f"== journal: settled {a['settled']} net {a['net_pnl']} | excluded {a['excluded']} | unresolved {a['unresolved']} | open {a['open']}")
if s.get("dead_letters"):
    print("== DEAD LETTERS:", len(s["dead_letters"]))
PYEOF
echo "== worker log (errors, last 15 min)"
journalctl -u options-futures --since "-15min" --no-pager 2>/dev/null | grep -iE "error|traceback|failed|killed" | tail -8
echo "== done"

#!/usr/bin/env bash
# Read-only morning check (first live day of the weekend's work). Changes nothing.
cd ~
PY=/home/ubuntu/.venv/bin/python; [ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python
D=$(TZ=Asia/Kolkata date +%F)
echo "== now: $(TZ=Asia/Kolkata date '+%a %F %H:%M IST')"
echo; echo "== 1. collector: restarted at 08:45 IST, fixed writer, bars flowing"
systemctl show options-collector -p ActiveEnterTimestamp --value
echo "writer.py md5 $(md5sum writer.py | cut -c1-7) (expect 7696ada)"
sudo journalctl -u options-collector --since "$D 03:00" --no-pager -o cat | grep -vE 'Z  bars=' | tail -n 8
sudo journalctl -u options-collector --since '-3min' --no-pager -o cat | tail -n 2
ls data/bars_1m/date=$D/underlying=NIFTY 2>/dev/null | wc -l | sed 's/^/NIFTY parts today: /'
echo "restarts since 08:45 IST (expect 1):"; sudo journalctl -u options-collector --since "$D 03:10" --no-pager -o cat | grep -c 'Started options-collector'
echo; echo "== 2. daily signal (09:00 IST)"
sudo journalctl -u options-signal --since "$D 03:00" --no-pager -o cat | grep -vE '^\s*$' | tail -n 25
echo; echo "== 3. feature row"
tail -n 1 data/features/decisions.csv | cut -c1-300
$PY -c "import features as F; r=F.load().tail(1).iloc[0]; print('decision', r.decision_id, '| version', r.feature_version, '| missing:', r.missing)"
echo; echo "== 4. shadow decisions"
tail -n 3 data/shadow/decisions.csv 2>/dev/null || echo "no decisions file"
echo; echo "== 5. score job (09:15 IST): journal, outcomes, shadow"
sudo journalctl -u options-score --since "$D 03:40" --no-pager -o cat | grep -vE '^\s*$' | tail -n 20
echo; echo "== 6. failed units"; systemctl --failed --no-pager --no-legend || true

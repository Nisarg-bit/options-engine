#!/usr/bin/env bash
# Install the live intraday prediction loop. Safe to re-run.
set -euo pipefail

PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python

echo "=== one live cycle, recording nothing"
if ( cd /home/ubuntu && "$PY" live_signal.py --dry ); then
    echo "  OK"
else
    echo
    echo "  It refused or failed. Outside 09:20-15:15 IST a refusal is the"
    echo "  correct answer and the timer is fine to install anyway. Any other"
    echo "  message -- fix it before installing."
fi

echo
echo "=== installing"
chmod +x /home/ubuntu/run_live_signal.sh
sudo cp /home/ubuntu/deploy/options-live-signal.service \
        /home/ubuntu/deploy/options-live-signal.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-live-signal.timer

echo
echo "=== next run"
systemctl list-timers options-live-signal.timer --no-pager

echo
echo "DONE. It predicts every 5 minutes from 09:20 to 15:15 IST."
echo
echo "SHADOW MODE: every row is tradeable=false and nothing is sent anywhere."
echo "The sigma underneath is known to be wrong -- it spreads variance evenly"
echo "across a clock where ~73% of it arrives overnight. These are being"
echo "recorded, not offered. Remove --live from the service only after the"
echo "two-term sigma is in."
echo
echo "Watch it:   journalctl -u options-live-signal -f"
echo "Today:      column -s, -t data/journal/live_signals.csv"

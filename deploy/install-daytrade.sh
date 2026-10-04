#!/usr/bin/env bash
# Install the intraday paper-trade journaller. Safe to re-run.
set -euo pipefail

PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python

echo "=== dry run (idempotent -- rewrites today's row, changes nothing else)"
if ( cd /home/ubuntu && "$PY" daytrade.py ); then
    echo "  OK"
else
    echo "  daytrade.py failed -- fix before installing the timer."
    exit 1
fi

echo
echo "=== installing"
sudo cp /home/ubuntu/deploy/options-daytrade.service \
        /home/ubuntu/deploy/options-daytrade.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-daytrade.timer

echo
echo "=== confirming the sandbox actually permits the one write it needs"
# ProtectSystem=strict makes /home/ubuntu read-only except data/journal.
# If that path is wrong the job fails at 15:20 on a day nobody is watching,
# so prove it HERE, under the real unit, rather than trusting the unit file.
sudo systemctl start options-daytrade.service
if systemctl is-failed --quiet options-daytrade.service; then
    echo "  FAILED under the unit's sandbox. Logs:"
    journalctl -u options-daytrade -n 30 --no-pager
    exit 1
fi
echo "  OK -- wrote the journal from inside ProtectSystem=strict"

echo
echo "=== next runs"
systemctl list-timers options-daytrade.timer --no-pager

echo
echo "DONE. Fires 15:20 and 15:40 IST, weekdays."
echo "Run by hand:  sudo systemctl start options-daytrade"
echo "Logs:         journalctl -u options-daytrade -n 40 --no-pager"

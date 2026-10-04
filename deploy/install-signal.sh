#!/usr/bin/env bash
# Install the daily signal job. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== checking signal.py runs (dry, sends nothing)"
if /home/ubuntu/.venv/bin/python /home/ubuntu/signal.py --dry >/dev/null 2>&1; then
    echo "  OK  signal.py builds a signal"
else
    echo "  FAILED - fix signal.py before installing the timer."
    echo "  Run it directly to see why:"
    echo "    /home/ubuntu/.venv/bin/python /home/ubuntu/signal.py --dry"
    exit 1
fi

echo
echo "=== installing"
sudo cp options-signal.service options-signal.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-signal.timer

echo
echo "=== next run"
systemctl list-timers options-signal.timer --no-pager

echo
echo "DONE. Send one to Telegram now with:"
echo "  sudo systemctl start options-signal && journalctl -u options-signal -n 30 --no-pager"

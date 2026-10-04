#!/usr/bin/env bash
# Install the nightly journal scorer. Safe to re-run.
set -euo pipefail

PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python

echo "=== dry run (scores nothing that is not due anyway)"
if ( cd /home/ubuntu && "$PY" journal.py score ); then
    echo "  OK"
else
    echo "  journal.py score failed -- fix before installing the timer."
    exit 1
fi

echo
echo "=== installing"
chmod +x /home/ubuntu/run_score.sh
sudo cp /home/ubuntu/deploy/options-score.service \
        /home/ubuntu/deploy/options-score.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-score.timer

echo
echo "=== next run"
systemctl list-timers options-score.timer --no-pager
echo
echo "DONE. Tomorrow at 09:15 IST it scores the 8 Sep expiry and, because"
echo "something will have changed, sends you the calibration table on Telegram."
echo
echo "Run it by hand any time with:  sudo systemctl start options-score"
echo "Logs:  journalctl -u options-score -n 40 --no-pager"

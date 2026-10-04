#!/usr/bin/env bash
# Install the options-engine systemd units. Safe to re-run.
# Installs and ENABLES but deliberately does NOT start anything -- today's
# collector is still running under tmux and two collectors writing the same
# Parquet files would corrupt the day.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== server time and timezone"
timedatectl | sed -n '1,4p'

echo
echo "=== resolving interpreter"
if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
echo "will use: $PY"

echo
echo "=== preflight: can that interpreter import everything?"
missing=0
for mod in dotenv kiteconnect pandas pyarrow requests pyotp exchange_calendars; do
    if "$PY" -c "import $mod" 2>/dev/null; then
        echo "  ok      $mod"
    else
        echo "  MISSING $mod"
        missing=1
    fi
done
if [ "$missing" -ne 0 ]; then
    echo
    echo "Install the missing ones before starting the services, e.g.:"
    echo "  $PY -m pip install python-dotenv --break-system-packages"
    echo "Continuing with the unit install anyway."
fi

echo
echo "=== installing launch scripts"
install -m 755 run_collector.sh /home/ubuntu/run_collector.sh
install -m 755 run_bot.sh       /home/ubuntu/run_bot.sh
echo "  /home/ubuntu/run_collector.sh"
echo "  /home/ubuntu/run_bot.sh"

echo
echo "=== installing systemd units"
sudo cp options-collector.service options-bot.service \
        options-restart.service options-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable options-collector.service options-bot.service options-restart.timer

echo
echo "=== next scheduled restart"
sudo systemctl start options-restart.timer
systemctl list-timers options-restart.timer --no-pager

echo
echo "DONE. Units are installed and enabled but NOT started."
echo "Tonight after 15:30 IST, stop the tmux sessions and run:"
echo "  sudo systemctl start options-collector options-bot"

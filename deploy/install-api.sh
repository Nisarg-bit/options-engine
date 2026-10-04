#!/usr/bin/env bash
# Install the dashboard API. Safe to re-run.
#
# Runs from anywhere: every path below is absolute, which is what
# install-signal.sh got wrong.
set -euo pipefail

PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python

echo "=== interpreter"
echo "  $PY"
"$PY" -V

echo
echo "=== dependencies"
"$PY" -m pip install --quiet --upgrade fastapi "uvicorn[standard]" || {
    echo "  pip install failed"; exit 1; }
"$PY" -c "import fastapi, uvicorn; print('  fastapi', fastapi.__version__)"

echo
echo "=== files present"
for f in /home/ubuntu/api.py /home/ubuntu/web/index.html /home/ubuntu/run_api.sh; do
    [ -e "$f" ] || { echo "  MISSING $f"; exit 1; }
    echo "  OK  $f"
done
chmod +x /home/ubuntu/run_api.sh

echo
echo "=== stdlib shadowing check"
# A file named signal.py or json.py on the path breaks uvicorn's import of
# anyio, which does `from signal import Signals`. This bit us once already.
for bad in signal.py json.py types.py select.py socket.py; do
    if [ -e "/home/ubuntu/$bad" ]; then
        echo "  FATAL /home/ubuntu/$bad shadows a stdlib module -- rename it"
        exit 1
    fi
done
echo "  clean"

echo
echo "=== import check (does not bind a port)"
if ( cd /home/ubuntu && "$PY" -c "import api; print('  routes:', len(api.app.routes))" ); then
    echo "  OK"
else
    echo "  FAILED - fix api.py before installing the unit."
    exit 1
fi

echo
echo "=== installing"
sudo cp /home/ubuntu/deploy/options-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-api.service
sleep 3

echo
echo "=== status"
systemctl --no-pager --lines=0 status options-api.service || true

echo
echo "=== local probe"
curl -s -m 5 http://127.0.0.1:8000/api/health || echo "  no answer on 127.0.0.1:8000"
echo

echo
echo "DONE."
echo "Port 8000 must stay CLOSED in the EC2 security group."
echo "Reach it over Tailscale:  http://<tailscale-name>:8000/"
echo "Logs:  journalctl -u options-api -f"

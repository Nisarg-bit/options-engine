#!/usr/bin/env bash
# Install the Firebase publisher. Safe to re-run.
#
# Every path is absolute, so it runs from anywhere -- that is what
# install-signal.sh got wrong.
set -euo pipefail

PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python
CRED=/home/ubuntu/.firebase-service-account.json

echo "=== interpreter"
echo "  $PY"

echo
echo "=== dependency"
"$PY" -m pip install --quiet --upgrade firebase-admin || {
    echo "  pip install failed"; exit 1; }
"$PY" -c "import firebase_admin; print('  firebase-admin', firebase_admin.__version__)"

echo
echo "=== credentials"
if [ ! -f "$CRED" ]; then
    echo "  MISSING $CRED"
    echo
    echo "  In the Firebase console:  Project settings -> Service accounts"
    echo "  -> Generate new private key. That downloads a .json on Windows."
    echo "  Copy it up with:"
    echo
    echo "    scp -i <path-to-your-key>.pem \\"
    echo "        <that-file>.json <user>@<server-ip>:/home/ubuntu/.firebase-service-account.json"
    echo
    echo "  Then run this again."
    exit 1
fi
chmod 600 "$CRED"
# Never print the file. The project id is the only harmless field and it is
# what you actually want to eyeball, so print exactly that and nothing else.
PROJ=$("$PY" -c "import json,sys; print(json.load(open('$CRED')).get('project_id','?'))")
echo "  key present, chmod 600, project: $PROJ"

echo
echo "=== database URL"
if ! grep -q '^FIREBASE_DB_URL=' /home/ubuntu/.env 2>/dev/null; then
    echo "  FIREBASE_DB_URL is not in /home/ubuntu/.env"
    echo
    echo "  Find it in the console: Realtime Database -> the https://... shown"
    echo "  at the top of the Data tab. Then, WITHOUT pasting it into a chat"
    echo "  or a screenshot of the whole file:"
    echo
    echo "    echo 'FIREBASE_DB_URL=https://${PROJ}-default-rtdb.firebaseio.com' >> /home/ubuntu/.env"
    echo
    echo "  (Asia region databases look like"
    echo "   https://${PROJ}-default-rtdb.asia-southeast1.firebasedatabase.app"
    echo "   -- copy whatever the console shows, exactly.)"
    exit 1
fi
chmod 600 /home/ubuntu/.env
echo "  set"

echo
echo "=== one publish, to prove it works before installing a unit"
set -a; . /home/ubuntu/.env; set +a
export FIREBASE_CREDENTIALS="$CRED"
if ( cd /home/ubuntu && FIREBASE_DB_URL="$FIREBASE_DB_URL" "$PY" publisher.py --once ); then
    echo "  OK"
else
    echo "  FAILED -- fix the above before installing the unit."
    exit 1
fi

echo
echo "=== installing"
chmod +x /home/ubuntu/run_publisher.sh
sudo cp /home/ubuntu/deploy/options-publisher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-publisher.service
sleep 4

echo
systemctl --no-pager --lines=0 status options-publisher.service || true
echo
echo "DONE."
echo "Live log:  journalctl -u options-publisher -f"
echo "Each line shows which nodes were written and which were unchanged."

#!/usr/bin/env bash
# Push live state to Firebase Realtime Database.
#
# Reads, never writes, the collector's data -- same as api.py. The only thing
# it writes is to Firebase.
set -euo pipefail
cd /home/ubuntu

# Same interpreter-pinning as the other units. A bare `python3` resolves
# differently depending on which shell started it; that is what broke bot.py.
if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
echo "interpreter: $PY"

# FIREBASE_DB_URL and, optionally, FIREBASE_CREDENTIALS live in .env.
if [ -f /home/ubuntu/.env ]; then
    set +u; set -a; . /home/ubuntu/.env; set +a; set -u
fi

: "${FIREBASE_DB_URL:?FIREBASE_DB_URL is not set in /home/ubuntu/.env}"
export FIREBASE_DB_URL
export FIREBASE_CREDENTIALS="${FIREBASE_CREDENTIALS:-/home/ubuntu/.firebase-service-account.json}"

exec "$PY" publisher.py

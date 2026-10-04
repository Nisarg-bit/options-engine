#!/usr/bin/env bash
# Serve the dashboard API. Read-only: it never writes into data/ and never
# opens a Kite websocket, so it cannot disturb the collector.
set -euo pipefail
cd /home/ubuntu

# Same interpreter-pinning as run_collector.sh. A bare `python3` resolves
# differently depending on which shell started it -- that is what broke bot.py.
if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
echo "interpreter: $PY"

# DASH_TOKEN, if present in .env, makes every /api/ call require ?k=<token>.
# The real boundary is the network -- this is the second lock, not the first.
if [ -f /home/ubuntu/.env ]; then
    set +u
    # shellcheck disable=SC1091
    set -a; . /home/ubuntu/.env; set +a
    set -u
fi

# Bound to 0.0.0.0 so the Tailscale interface can serve it. This is only safe
# while port 8000 stays CLOSED in the EC2 security group -- do not open it.
exec "$PY" -m uvicorn api:app \
    --host 0.0.0.0 --port 8000 \
    --workers 1 \
    --log-level warning \
    --timeout-keep-alive 30

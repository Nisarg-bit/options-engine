#!/usr/bin/env bash
# Refresh today's instrument master, then hand over to the collector.
#
# systemd restarts this whole script, not just collector.py, so every restart
# re-runs instruments.py first. That means the collector can never come back
# up holding yesterday's expiry list.
set -euo pipefail
cd /home/ubuntu

# Pin the interpreter explicitly. A bare `python3` resolves differently
# depending on which shell started it -- that is exactly what broke bot.py.
if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
echo "interpreter: $PY"

# Kite can be briefly unavailable at 08:45. Retry rather than dying and
# letting systemd hammer the API in a restart loop.
for attempt in 1 2 3; do
    if "$PY" instruments.py; then
        break
    fi
    echo "instruments.py failed (attempt ${attempt}/3)"
    if [ "$attempt" -eq 3 ]; then
        echo "giving up on instrument refresh"
        exit 1
    fi
    sleep 60
done

# -u: unbuffered stdout. Under systemd stdout is a pipe, not a terminal, so
# Python block-buffers it in ~8 KB chunks -- the collector's startup lines
# ("instruments : N from <file>", "subscribing to N") sat in memory and never
# reached the journal, which made it impossible to tell from the logs which
# instrument master the running collector had actually loaded. Found on the
# morning SENSEX was added, when that was the only question that mattered.
# publisher.py already passes flush=True everywhere for the same reason.
exec "$PY" -u collector.py

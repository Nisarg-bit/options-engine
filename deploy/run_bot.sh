#!/usr/bin/env bash
# Telegram command bot. Same interpreter resolution as the collector.
set -euo pipefail
cd /home/ubuntu

if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
echo "interpreter: $PY"

exec "$PY" bot.py

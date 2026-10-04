#!/usr/bin/env bash
set -euo pipefail
PY=/home/ubuntu/.venv/bin/python
[ -x /home/ubuntu/venv/bin/python ] && PY=/home/ubuntu/venv/bin/python
cd /home/ubuntu
exec "$PY" live_signal.py "$@"

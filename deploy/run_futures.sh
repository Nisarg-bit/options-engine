#!/usr/bin/env bash
# Stock-futures paper worker. Template, not installed (see options-futures.service).
# Current-session entries stay disabled until the stored config has
# live.entries_enabled=true and rules_validated=true and every required feed is ok.
set -euo pipefail
cd /home/ubuntu
if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi
mkdir -p /home/ubuntu/data/futures
exec "$PY" sf_cli.py worker --source repo --mode both

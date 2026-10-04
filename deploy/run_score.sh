#!/usr/bin/env bash
# Score any expired signals, and tell you only when there is something to tell.
#
# Timing matters here and is the reason this runs in the morning rather than
# after the close. journal.py scores an expiry the day AFTER it expires, and
# that is deliberate: BarWriter flushes on five distinct minutes, so when ticks
# stop at 15:30 the last few minutes sit in memory until the collector restarts
# at 08:45. Score at 16:00 on expiry day and the parity settlement reads a
# 15:26 bar as if it were the close -- wrong in a way nobody would notice.
#
# So: after the 08:45 restart has flushed, and after the 09:00 signal has
# logged. 09:15 IST.
set -uo pipefail
cd /home/ubuntu

if   [ -x /home/ubuntu/venv/bin/python ];  then PY=/home/ubuntu/venv/bin/python
elif [ -x /home/ubuntu/.venv/bin/python ]; then PY=/home/ubuntu/.venv/bin/python
else PY="$(command -v python3)"; fi

OUT="$("$PY" journal.py score 2>&1)"
echo "$OUT"

# Only speak when a row actually moved. A message every morning saying
# "nothing to score" is a message you stop reading, and then you stop reading
# the one that matters too.
if echo "$OUT" | grep -Eq 'scored [1-9]'; then
    REPORT="$("$PY" journal.py report 2>&1)"
    echo "$REPORT"
    "$PY" - "$OUT" "$REPORT" <<'PYEOF'
import html, sys
import notify
scored, report = sys.argv[1], sys.argv[2]
notify.send("<b>JOURNAL SCORED</b>\n<pre>"
            + html.escape(scored) + "\n\n" + html.escape(report[:3200])
            + "</pre>")
PYEOF
else
    echo "nothing new to report"
fi

# Outcome store (outcomes.py, roadmap step 6): what happened on every settled
# signal -- path, adverse excursion, each exit rule. Runs AFTER scoring and
# the report on its own line: a failure here can never cost a scored row or
# the Telegram message.
"$PY" outcomes.py || echo "outcomes.py failed -- journal scoring above is unaffected"

# Shadow books (shadow.py): settle expired paper trades, refresh the summary.
"$PY" shadow.py settle || echo "shadow.py failed -- journal scoring above is unaffected"

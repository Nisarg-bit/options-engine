#!/usr/bin/env bash
# Install the backup job. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

BACKUP_BUCKET="${BACKUP_BUCKET:-$(grep -E '^BACKUP_BUCKET=' /home/ubuntu/.env 2>/dev/null | cut -d= -f2-)}"
BUCKET="${BACKUP_BUCKET:?BACKUP_BUCKET is not set - add it to /home/ubuntu/.env}"

echo "=== checking the instance role can reach the bucket"
if aws s3 ls "$BUCKET/" >/dev/null 2>&1; then
    echo "  OK  $BUCKET is reachable"
else
    echo "  FAILED - the role cannot list the bucket."
    echo "  Check the inline policy is attached and the bucket name matches."
    exit 1
fi

echo
echo "=== installing"
install -m 755 backup.sh /home/ubuntu/backup.sh
sudo cp options-backup.service options-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now options-backup.timer

echo
echo "=== next run"
systemctl list-timers options-backup.timer --no-pager

echo
echo "DONE. Run the first backup now with:"
echo "  sudo systemctl start options-backup && journalctl -u options-backup -f"

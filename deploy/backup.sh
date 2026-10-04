#!/usr/bin/env bash
# Daily backup of collected data to S3.
#
# Design notes worth keeping in mind if you ever edit this:
#
#   * NO --delete. The backup only ever grows. A bug that wipes data/ on this
#     box must not propagate to the copy that exists to survive that bug.
#     Together with bucket versioning and the fact that this instance's IAM
#     role has no s3:DeleteObject, nothing running here can destroy a backup.
#
#   * The code is backed up too, on a strict whitelist. .env and .kite_token
#     hold live broker credentials and are excluded by construction -- the
#     include list names what goes, so a new secret file can never be swept
#     up by accident.
#
#   * Minute bars for a past session cannot be re-collected by anyone at any
#     price. That asymmetry is the whole reason this job exists.
set -euo pipefail

# Bucket name lives in /home/ubuntu/.env (BACKUP_BUCKET=s3://...), not in the repo.
BACKUP_BUCKET="${BACKUP_BUCKET:-$(grep -E '^BACKUP_BUCKET=' /home/ubuntu/.env 2>/dev/null | cut -d= -f2-)}"
BUCKET="${BACKUP_BUCKET:?BACKUP_BUCKET is not set - add it to /home/ubuntu/.env}"
cd /home/ubuntu

echo "backup starting $(date -u +%FT%TZ)"

# --- collected data -------------------------------------------------------
aws s3 sync data/ "$BUCKET/data/" --only-show-errors

# --- source, whitelist only ----------------------------------------------
aws s3 sync . "$BUCKET/code/" \
    --exclude "*" \
    --include "*.py" \
    --include "*.md" \
    --include "deploy/*" \
    --exclude ".env" \
    --exclude ".kite_token" \
    --exclude "*/__pycache__/*" \
    --only-show-errors

echo "backup complete $(date -u +%FT%TZ)"
aws s3 ls --summarize --human-readable --recursive "$BUCKET/data/" | tail -3

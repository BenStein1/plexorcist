#!/usr/bin/env bash
set -euo pipefail

# Copy this to deploy.local.sh and set your own paths.
SRC="/path/to/local/plexorcist/"
DST="/path/to/remote-mounted/plexorcist/"

rsync -av --delete \
  --filter='dir-merge /.rsync-filter' \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SRC" "$DST"

echo "Deploy sync complete: $SRC -> $DST"

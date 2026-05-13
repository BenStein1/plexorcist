#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
exec ./.venv/bin/gunicorn backend.main:app -c gunicorn.conf.py

#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python scripts/backup_local.py "$@"
fi
exec python3 scripts/backup_local.py "$@"

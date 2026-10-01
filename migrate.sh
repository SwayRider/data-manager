#!/usr/bin/env bash
# Apply database migrations (defaults to `upgrade head`; pass alembic args to override,
# e.g. ./migrate.sh downgrade -1, ./migrate.sh current).
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] && source .venv/bin/activate

if [ $# -eq 0 ]; then
  set -- upgrade head
fi

exec alembic "$@"

#!/usr/bin/env bash
# Run the Flask dev server in debug mode, reachable from the network, together with an RQ worker
# (stopped again when the server exits). Set NO_WORKER=1 to start only the server, e.g. when a
# worker already runs elsewhere; an already running worker of this checkout is reused.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] && source .venv/bin/activate
export FLASK_APP="${FLASK_APP:-wsgi.py}"

if [ -z "${NO_WORKER:-}" ]; then
  if pgrep -f "$PWD/run_worker.py" >/dev/null || pgrep -fx "python run_worker.py" >/dev/null; then
    echo "debug.sh: worker already running, not starting another" >&2
  else
    python "$PWD/run_worker.py" &
    WORKER_PID=$!
    trap 'kill "$WORKER_PID" 2>/dev/null || true; wait "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
    echo "debug.sh: worker started (pid $WORKER_PID)" >&2
  fi
fi

flask run --debug --host 0.0.0.0 --port 5050 "$@"

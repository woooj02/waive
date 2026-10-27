#!/usr/bin/env bash
# ./run.sh            serve on http://127.0.0.1:${PORT:-8100} with auto-reload
# ./run.sh test       run the test suite (offline, mock engine)
# ./run.sh setup      create the venv and install everything
set -euo pipefail
cd "$(dirname "$0")"

# ROS puts itself on PYTHONPATH system-wide on this machine and breaks imports.
unset PYTHONPATH

# Load .env if present, so a key set there reaches the app.
if [ -f .env ]; then set -a; . ./.env; set +a; fi

PY=./venv/bin/python
case "${1:-serve}" in
  setup)
    python3 -m venv venv
    ./venv/bin/pip install -q -r requirements-dev.txt
    echo "ready: ./run.sh"
    ;;
  serve)
    [ -x "$PY" ] || { echo "run ./run.sh setup first"; exit 1; }
    exec "$PY" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8100}"
    ;;
  test)
    shift
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 exec "$PY" -m pytest -q "$@"
    ;;
  *) echo "usage: ./run.sh [serve|test|setup]"; exit 1 ;;
esac

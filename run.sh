#!/usr/bin/env bash
# Convenience wrapper. Runs watcher.py with passthrough args.
# Pure stdlib — no venv/activation needed, just any Python 3.9+.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$HERE/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi
exec "$PYTHON" "$HERE/watcher.py" "$@"

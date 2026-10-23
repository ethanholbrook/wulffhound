#!/usr/bin/env bash
# Serve the dashboard (prototype) and the dataset it reads: http://127.0.0.1:PORT/dashboard/
# The page fetches ../dataset/*.jsonl, so the repository root is served (python -m http.server, no build step).
#   tools/serve_dashboard.sh [PORT]        (default 8765; rebuild the data first with ./wh py tools/export_dataset.py)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${1:-8765}"
PY="${WULFFHOUND_PY:-$(command -v python3 || command -v python)}"
[ -f dataset/index.json ] || { echo "dataset/ is missing: run ./wh py tools/export_dataset.py first" >&2; exit 1; }
echo "dashboard: http://127.0.0.1:${PORT}/dashboard/   (Ctrl-C to stop)"
exec "$PY" -m http.server "$PORT" --bind 127.0.0.1

#!/usr/bin/env bash
# Start the dashboard for the HR example. Optional first argument is a port: bash start.sh 8010
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo " [X] Not set up yet. Run: bash setup.sh"
    exit 1
fi

PORT="${1:-8000}"
URL="http://localhost:${PORT}"

echo
echo " Starting the dashboard (HR example simulator) on ${URL}"
echo " Keep this window open while you use it. Press Ctrl+C to stop."
echo

# Open the browser once the server has had a moment to bind.
(
    sleep 3
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    fi
) >/dev/null 2>&1 &

exec .venv/bin/python -m askback --config examples/hr/config.yaml serve --port "$PORT"

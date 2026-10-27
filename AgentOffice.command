#!/bin/bash
# AgentOffice desktop app on macOS — double-click in Finder, or run from Terminal.
# Starts the API if needed, picks a free port if :8000 is taken, opens a visible Chrome app window.
set -e
cd "$(dirname "$0")"
mkdir -p logs

if [[ ! -x .venv/bin/python ]]; then
  osascript -e 'display alert "AgentOffice" message "No .venv yet. In Terminal run:\n\npython3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp -n .env.example .env" as critical' || true
  echo "No .venv yet. Run once:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp -n .env.example .env"
  exit 1
fi

if [[ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] \
   && [[ ! -x "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" ]]; then
  osascript -e 'display alert "AgentOffice" message "Install Google Chrome (recommended) or Microsoft Edge, then try again." as critical' || true
  exit 1
fi

osascript -e 'display notification "Starting AgentOffice…" with title "AgentOffice"' >/dev/null 2>&1 || true
exec .venv/bin/python scripts/island.py --serve --browser chrome "$@"

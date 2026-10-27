#!/bin/bash
# AgentOffice desktop app on macOS: double-click this file (or run it from Terminal).
# Starts the API if needed and opens the floating island (Chrome/Edge app mode).
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "No .venv yet. Run once:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp -n .env.example .env"
  exit 1
fi
exec .venv/bin/python scripts/island.py --serve "$@"

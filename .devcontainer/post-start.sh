#!/usr/bin/env bash
set -euo pipefail
trap 'echo "Dev Container startup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

if [[ ! -d ".venv" ]]; then
  echo "Creating Python virtual environment at .venv..."
  python3 -m venv .venv
else
  echo "Python virtual environment already exists at .venv."
fi

echo "Installing package in editable mode..."
.venv/bin/pip install --quiet -e ".[dev]" 2>/dev/null || .venv/bin/pip install --quiet -e .

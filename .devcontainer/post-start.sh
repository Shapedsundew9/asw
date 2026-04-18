#!/usr/bin/env bash
set -euo pipefail
trap 'echo "Dev Container startup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

if [[ ! -d ".venv" ]]; then
  echo "Creating Python virtual environment at .venv..."
  python3 -m venv .venv
else
  echo "Python virtual environment already exists at .venv."
fi

echo "Activating virtual environment and installing dependencies..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "Installing package and dev quality tools in editable mode..."
.venv/bin/pip install -e .[dev]

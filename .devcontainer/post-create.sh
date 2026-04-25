#!/usr/bin/env bash
set -euo pipefail
trap 'echo "Dev Container setup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

echo "Installing apt dependencies..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends ripgrep
sudo rm -rf /var/lib/apt/lists/*

echo "Installing Python virtual environment and dependencies..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .[dev]

echo "Installing npm CLIs..."
npm install -g @devcontainers/cli @google/gemini-cli markdownlint-cli2

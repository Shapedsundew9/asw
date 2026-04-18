#!/usr/bin/env bash
set -euo pipefail
trap 'echo "Dev Container setup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

echo "Installing apt dependencies..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends ripgrep
sudo rm -rf /var/lib/apt/lists/*

echo "Installing npm CLIs..."
npm install -g @google/gemini-cli @github/copilot

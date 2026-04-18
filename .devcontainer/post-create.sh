#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ripgrep
npm install -g @google/gemini-cli @github/copilot

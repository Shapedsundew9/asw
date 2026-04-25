#!/usr/bin/env bash
# Run the company CLI test pipeline inside a disposable dev container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$BASE_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
	PYTHON_BIN="${PYTHON:-python3}"
fi

echo "Running company tests in a disposable dev container..."
exec "$PYTHON_BIN" -m asw.test_company_runner "$@"
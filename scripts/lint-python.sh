#!/bin/bash
# Lint Python files using pylint and mypy
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
	PYTHON_BIN="${PYTHON:-python3}"
fi

echo "Linting Python files with pylint..."
"$PYTHON_BIN" -m pylint src/asw tests/

echo "Type checking with mypy..."
"$PYTHON_BIN" -m mypy src/asw

echo "✓ Python linting passed"

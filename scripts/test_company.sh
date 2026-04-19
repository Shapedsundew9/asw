#!/bin/bash
# Format Python files using isort and black
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPANY_DIR="$BASE_DIR/tests/test_company"

echo "Running company tests..."
asw start --vision "$BASE_DIR/tests/test_vision.md" --workdir "$COMPANY_DIR" --debug "$COMPANY_DIR/debug.log" --no-commit --restart
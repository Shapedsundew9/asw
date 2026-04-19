#!/bin/bash
# Format Python files using isort and black
set -e

echo "Running company tests..."
asw start --vision /workspaces/asw/tests/test_vision.md --workdir /workspaces/asw/tests/test_company --debug --no-commit
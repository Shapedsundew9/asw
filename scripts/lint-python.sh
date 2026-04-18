#!/bin/bash
# Lint Python files using pylint and mypy
set -e

echo "Linting Python files with pylint..."
pylint src/asw tests/

echo "Type checking with mypy..."
mypy src/asw

echo "✓ Python linting passed"

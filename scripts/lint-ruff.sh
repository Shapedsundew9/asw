#!/bin/bash
# Lint Python files using ruff
set -e

echo "Auto-fixing Python files with ruff..."
ruff check --fix src/asw tests/ || true

echo "Linting Python files with ruff..."
ruff check src/asw tests/

echo "✓ Ruff linting passed"

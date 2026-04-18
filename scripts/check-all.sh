#!/bin/bash
# Comprehensive check suite: markdown lint, python format, python lint
set -e

echo "=========================================="
echo "Starting comprehensive check suite"
echo "=========================================="
echo ""

# Run markdown linting
echo "[1/4] Markdown Linting"
echo "---"
./scripts/lint-markdown.sh
echo ""

# Run python formatting
echo "[2/4] Python Formatting"
echo "---"
./scripts/format-python.sh
echo ""

# Run python linting
echo "[3/4] Python Linting"
echo "---"
./scripts/lint-python.sh
echo ""

# Run ruff linting
echo "[4/4] Ruff Linting"
echo "---"
./scripts/lint-ruff.sh
echo ""

echo "=========================================="
echo "✓ All checks passed!"
echo "=========================================="

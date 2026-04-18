#!/bin/bash
# Comprehensive check suite: markdown lint, python format, python lint
exit_code=0

echo "=========================================="
echo "Starting comprehensive check suite"
echo "=========================================="
echo ""

# Run markdown linting
echo "[1/4] Markdown Linting"
echo "---"
./scripts/lint-markdown.sh || exit_code=$?
echo ""

# Run python formatting
echo "[2/4] Python Formatting"
echo "---"
./scripts/format-python.sh || exit_code=$?
echo ""

# Run python linting
echo "[3/4] Python Linting"
echo "---"
./scripts/lint-python.sh || exit_code=$?
echo ""

# Run ruff linting
echo "[4/4] Ruff Linting"
echo "---"
./scripts/lint-ruff.sh || exit_code=$?
echo ""

echo "=========================================="
if [ "$exit_code" -eq 0 ]; then
    echo "✓ All checks passed!"
else
    echo "✗ Some checks failed (exit code: $exit_code)"
fi
echo "=========================================="
exit "$exit_code"

#!/bin/bash
# Lint Markdown files using markdownlint-cli2
set -e

echo "Linting Markdown files..."
markdownlint-cli2 --config pyproject.toml --configPointer /tool/markdownlint-cli2/config "**/*.md" "#."
echo "✓ Markdown linting passed"

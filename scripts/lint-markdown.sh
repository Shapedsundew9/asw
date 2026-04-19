#!/bin/bash
# Lint Markdown files using markdownlint-cli2
set -e

markdown_globs=(
	"AGENT.md"
	"README.md"
	"docs/**/*.md"
	"src/**/*.md"
	"tests/**/*.md"
	".github/**/*.md"
)

echo "Auto-fixing Markdown files..."
markdownlint-cli2 --fix --config pyproject.toml --configPointer /tool/markdownlint-cli2/config "${markdown_globs[@]}" || true

echo "Linting Markdown files..."
markdownlint-cli2 --config pyproject.toml --configPointer /tool/markdownlint-cli2/config "${markdown_globs[@]}"
echo "✓ Markdown linting passed"

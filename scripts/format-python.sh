#!/bin/bash
# Format Python files using isort and black
set -e

echo "Formatting imports with isort..."
isort src/asw tests/

echo "Formatting code with black..."
black src/asw tests/

echo "✓ Python formatting complete"

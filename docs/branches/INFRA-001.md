# INFRA-001 – Add Python Package Assets

## Overview

Added standard Python packaging assets to bootstrap the `asw` package.

## Changes

| File | Change |
|---|---|
| `pyproject.toml` | New — declares the `asw` package using PEP 517/518 (`setuptools` build backend), project metadata, pytest config, black/isort settings |
| `src/asw/__init__.py` | New — package entry point with `__version__` |
| `.devcontainer/post-start.sh` | Updated — installs the package in editable mode (`pip install -e .`) after venv creation |

## Package Layout

```
src/
└── asw/
    └── __init__.py
```

The `src`-layout is used to keep the importable package isolated from the project root, preventing accidental imports of uninstalled code.

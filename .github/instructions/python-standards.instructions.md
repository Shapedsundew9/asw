---
description: "Use when creating or modifying Python modules, functions, classes, or pytest tests in ASW. Covers future annotations, type hints, docstrings, formatting, logging, and exception handling."
name: "ASW Python Standards"
applyTo: "src/**/*.py, tests/**/*.py"
---

# ASW Python Standards

Follow these repository rules when creating or modifying Python in `src/` and `tests/`:

- Start every module with `from __future__ import annotations` after any module docstring.
- Add type annotations to all function and method parameters and return values.
- Use Google-style docstrings for public modules, classes, and functions.
- Give each pytest test a docstring that explains the behavior under test.
- Keep code consistent with Black and isort, and stay within the repository's 120-character line length.
- Use `logging`, not `print()`, for operational output. In `src/asw/`, prefer a module-level logger named from the module import path, such as `logging.getLogger("asw.company")`.
- Raise specific exception types. Under no circumstances use bare `except:`.

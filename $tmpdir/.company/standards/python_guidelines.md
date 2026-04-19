# Python Development Standards

## Style & Formatting

- Follow PEP 8 for all Python code.
- Use Google-style docstrings for all public modules, classes, and functions.
- Maximum line length: 120 characters.
- Use `black` formatting conventions.
- Sort imports with `isort` (profile: black).

## Type Safety

- Add type annotations to all function signatures and return types.
- Use `from __future__ import annotations` at the top of every module.

## Testing

- Write comprehensive unit tests for all public functions and classes.
- Use `pytest` as the test framework.
- Aim for branch coverage, not just line coverage.
- Test edge cases and error conditions, not just the happy path.

## Dependencies

- Prefer Python standard library modules over third-party packages.
- Justify any new dependency with a comment explaining why the stdlib alternative is insufficient.

## Error Handling

- Use specific exception types; never use bare `except:`.
- Raise descriptive exceptions at system boundaries.
- Log errors with the `logging` module, not `print()`.

## Security

- Never hardcode secrets, API keys, or credentials.
- Validate and sanitise all external input.
- Follow the OWASP Top 10 guidelines where applicable.

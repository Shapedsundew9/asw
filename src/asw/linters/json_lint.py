"""JSON mechanical validator for architecture artifacts."""

from __future__ import annotations

import json

_REQUIRED_KEYS = (
    "project_name",
    "tech_stack",
    "components",
    "data_models",
    "api_contracts",
    "deployment",
)


def validate_architecture(content: str) -> list[str]:
    """Validate that *content* is well-formed architecture JSON.

    Returns a list of error messages (empty == pass).
    """
    errors: list[str] = []

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid JSON: {exc}")
        return errors

    if not isinstance(data, dict):
        errors.append(f"Expected a JSON object at top level, got {type(data).__name__}.")
        return errors

    for key in _REQUIRED_KEYS:
        if key not in data:
            errors.append(f"Missing required key: '{key}'")

    return errors

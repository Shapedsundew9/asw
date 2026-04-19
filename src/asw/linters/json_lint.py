"""JSON mechanical validators for structured pipeline artifacts."""

from __future__ import annotations

import json
import re

_REQUIRED_KEYS = (
    "project_name",
    "tech_stack",
    "components",
    "data_models",
    "api_contracts",
    "deployment",
)

_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.md$")


def _expect_non_empty_string(data: dict, key: str, prefix: str, errors: list[str]) -> None:
    """Require *key* in *data* to be a non-empty string."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}: must be a non-empty string.")


def _expect_string_list(data: dict, key: str, prefix: str, errors: list[str], *, allow_empty: bool = False) -> None:
    """Require *key* in *data* to be a list of non-empty strings."""
    value = data.get(key)
    if not isinstance(value, list):
        errors.append(f"{prefix}.{key}: must be an array.")
        return
    if not allow_empty and not value:
        errors.append(f"{prefix}.{key}: must be a non-empty array.")
        return
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{prefix}.{key}[{idx}]: must be a non-empty string.")


def _validate_founder_questions(data: dict, errors: list[str]) -> None:
    """Validate optional founder questions on a structured JSON artifact."""
    questions = data.get("founder_questions")
    if questions is None:
        return
    if not isinstance(questions, list):
        errors.append("founder_questions: must be an array when present.")
        return

    for idx, item in enumerate(questions):
        prefix = f"founder_questions[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        _expect_non_empty_string(item, "question", prefix, errors)

        answer = item.get("answer")
        choices = item.get("choices")
        if answer is not None and (not isinstance(answer, str) or not answer.strip()):
            errors.append(f"{prefix}.answer: must be a non-empty string when present.")
        if answer is None:
            if not isinstance(choices, list) or not choices:
                errors.append(f"{prefix}.choices: must be a non-empty array when no answer is present.")
                continue
            for choice_idx, choice in enumerate(choices):
                if not isinstance(choice, str) or not choice.strip():
                    errors.append(f"{prefix}.choices[{choice_idx}]: must be a non-empty string.")


def _validate_selected_team(data: dict, errors: list[str]) -> set[str]:
    """Validate the selected team and return the discovered role titles."""
    selected_titles: set[str] = set()
    selected_team = data.get("selected_team")
    if not isinstance(selected_team, list) or not selected_team:
        errors.append("selected_team: must be a non-empty array.")
        return selected_titles

    for idx, entry in enumerate(selected_team):
        prefix = f"selected_team[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        _expect_non_empty_string(entry, "title", prefix, errors)
        _expect_non_empty_string(entry, "filename", prefix, errors)
        _expect_non_empty_string(entry, "responsibility", prefix, errors)
        _expect_non_empty_string(entry, "rationale", prefix, errors)

        filename = entry.get("filename")
        if isinstance(filename, str) and not _FILENAME_RE.match(filename):
            errors.append(f"{prefix}.filename: must match lowercase_underscore.md format.")

        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            selected_titles.add(title)
    return selected_titles


def _validate_phases(data: dict, selected_titles: set[str], errors: list[str]) -> None:
    """Validate the phase array and role references."""
    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        errors.append("phases: must be a non-empty array.")
        return

    for idx, phase in enumerate(phases):
        prefix = f"phases[{idx}]"
        if not isinstance(phase, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        _expect_non_empty_string(phase, "id", prefix, errors)
        _expect_non_empty_string(phase, "name", prefix, errors)
        _expect_non_empty_string(phase, "objective", prefix, errors)
        _expect_non_empty_string(phase, "scope", prefix, errors)
        _expect_string_list(phase, "deliverables", prefix, errors)
        _expect_string_list(phase, "exit_criteria", prefix, errors)
        selected_team_roles = phase.get("selected_team_roles")
        _expect_string_list(phase, "selected_team_roles", prefix, errors)

        if not isinstance(selected_team_roles, list):
            continue

        for role in selected_team_roles:
            if isinstance(role, str) and selected_titles and role not in selected_titles:
                errors.append(f"{prefix}.selected_team_roles: '{role}' is not present in selected_team.")


def _validate_generic_role_catalog(data: dict, errors: list[str]) -> None:
    """Validate the generic role catalog entries."""
    catalog = data.get("generic_role_catalog")
    if not isinstance(catalog, list) or not catalog:
        errors.append("generic_role_catalog: must be a non-empty array.")
        return

    for idx, entry in enumerate(catalog):
        prefix = f"generic_role_catalog[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        _expect_non_empty_string(entry, "title", prefix, errors)
        _expect_non_empty_string(entry, "summary", prefix, errors)
        _expect_non_empty_string(entry, "when_needed", prefix, errors)


def _validate_deferred_items(data: dict, errors: list[str]) -> None:
    """Validate deferred roles or capabilities."""
    deferred = data.get("deferred_roles_or_capabilities")
    if not isinstance(deferred, list):
        errors.append("deferred_roles_or_capabilities: must be an array.")
        return

    for idx, entry in enumerate(deferred):
        prefix = f"deferred_roles_or_capabilities[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object.")
            continue
        _expect_non_empty_string(entry, "name", prefix, errors)
        _expect_non_empty_string(entry, "rationale", prefix, errors)


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


def validate_execution_plan(content: str) -> list[str]:
    """Validate that *content* is well-formed execution-plan JSON."""
    errors: list[str] = []

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid JSON: {exc}")
        return errors

    if not isinstance(data, dict):
        errors.append(f"Expected a JSON object at top level, got {type(data).__name__}.")
        return errors

    required_top_level = (
        "phases",
        "selected_team",
        "generic_role_catalog",
        "deferred_roles_or_capabilities",
    )
    for key in required_top_level:
        if key not in data:
            errors.append(f"Missing required key: '{key}'")

    selected_titles = _validate_selected_team(data, errors)
    _validate_phases(data, selected_titles, errors)
    _validate_generic_role_catalog(data, errors)
    _validate_deferred_items(data, errors)

    _validate_founder_questions(data, errors)
    return errors

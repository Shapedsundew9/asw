"""Tests for execution-plan validation and rendering."""

from __future__ import annotations

import json

from asw.execution_plan import _render_execution_plan_markdown
from asw.linters.json_lint import validate_execution_plan

_VALID_EXECUTION_PLAN = {
    "phases": [
        {
            "id": "phase_1",
            "name": "Local Validation",
            "objective": "Validate the core workflow with minimal infrastructure.",
            "scope": "Use local-only dependencies and defer production hardening.",
            "deliverables": [
                "Core workflow implemented",
                "Foundational user path validated",
            ],
            "exit_criteria": [
                "Founder can run the core flow locally",
                "Core acceptance checks pass",
            ],
            "selected_team_roles": ["Python Backend Developer"],
        }
    ],
    "selected_team": [
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement the orchestrator and persistence path.",
            "rationale": "This role is required immediately to build the first milestone.",
        }
    ],
    "generic_role_catalog": [
        {
            "title": "DevOps Engineer",
            "summary": "Owns deployment automation and runtime operations.",
            "when_needed": "Needed when the product moves beyond a local-only workflow.",
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Production DevOps",
            "rationale": "Deferred until the product needs persistent hosted infrastructure.",
        }
    ],
    "founder_questions": [
        {
            "question": "Should the first milestone stay local-only?",
            "choices": ["Yes", "No"],
        }
    ],
}


def test_validate_execution_plan_valid() -> None:
    """A valid execution plan should produce no validation errors."""
    errors = validate_execution_plan(json.dumps(_VALID_EXECUTION_PLAN))
    assert not errors


def test_validate_execution_plan_missing_selected_team() -> None:
    """selected_team is required and must be non-empty."""
    payload = dict(_VALID_EXECUTION_PLAN)
    payload["selected_team"] = []

    errors = validate_execution_plan(json.dumps(payload))
    assert any("selected_team" in error for error in errors)


def test_validate_execution_plan_rejects_unknown_phase_role() -> None:
    """Phase role references must point at a selected team entry."""
    payload = json.loads(json.dumps(_VALID_EXECUTION_PLAN))
    payload["phases"][0]["selected_team_roles"] = ["Unknown Role"]

    errors = validate_execution_plan(json.dumps(payload))
    assert any("Unknown Role" in error for error in errors)


def test_validate_execution_plan_non_list_selected_team_roles_only_reports_type_error() -> None:
    """Non-list selected_team_roles should not trigger role-membership errors."""
    payload = json.loads(json.dumps(_VALID_EXECUTION_PLAN))
    payload["phases"][0]["selected_team_roles"] = "Python Backend Developer"

    errors = validate_execution_plan(json.dumps(payload))
    assert any("selected_team_roles: must be an array." in error for error in errors)
    assert not any("is not present in selected_team" in error for error in errors)


def test_render_execution_plan_markdown() -> None:
    """Rendered execution plan should contain phases, team details, and founder input."""
    md = _render_execution_plan_markdown(json.dumps(_VALID_EXECUTION_PLAN))

    assert "# Execution Plan" in md
    assert "Python Backend Developer" in md
    assert "Local Validation" in md
    assert "Production DevOps" in md
    assert "Should the first milestone stay local-only?" in md

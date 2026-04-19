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
            "selected_team_roles": [
                "Development Lead",
                "DevOps Engineer",
                "Python Backend Developer",
            ],
        }
    ],
    "selected_team": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate the approved phase plan and review implementation against it.",
            "rationale": "This role is required immediately to turn the approved plan into executable team work.",
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling for the phase.",
            "rationale": "This role is required immediately to keep tooling and environment setup repeatable.",
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement the orchestrator and persistence path.",
            "rationale": "This role is required immediately to build the first milestone.",
        },
    ],
    "generic_role_catalog": [
        {
            "title": "Documentation Standards Lead",
            "summary": "Owns tutorials, reference updates, and documentation quality control.",
            "when_needed": (
                "Needed when the product surface or user workflow changes faster than " "the docs stay current."
            ),
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Hosted Operations Platform",
            "rationale": "Deferred until the product needs persistent hosted infrastructure and production monitoring.",
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
    payload["phases"][0]["selected_team_roles"] = [
        "Development Lead",
        "DevOps Engineer",
        "Unknown Role",
    ]

    errors = validate_execution_plan(json.dumps(payload))
    assert any("Unknown Role" in error for error in errors)


def test_validate_execution_plan_missing_mandatory_core_role() -> None:
    """Mandatory core roles must appear in selected_team."""
    payload = json.loads(json.dumps(_VALID_EXECUTION_PLAN))
    payload["selected_team"] = [entry for entry in payload["selected_team"] if entry["title"] != "Development Lead"]

    errors = validate_execution_plan(json.dumps(payload))
    assert any("missing mandatory role 'Development Lead'" in error for error in errors)


def test_validate_execution_plan_phase_must_include_mandatory_core_roles() -> None:
    """Every phase must explicitly include the mandatory core roles."""
    payload = json.loads(json.dumps(_VALID_EXECUTION_PLAN))
    payload["phases"][0]["selected_team_roles"] = ["Python Backend Developer"]

    errors = validate_execution_plan(json.dumps(payload))
    assert any("must include 'Development Lead'" in error for error in errors)
    assert any("must include 'DevOps Engineer'" in error for error in errors)


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
    assert "Development Lead" in md
    assert "Python Backend Developer" in md
    assert "Local Validation" in md
    assert "Hosted Operations Platform" in md
    assert "Should the first milestone stay local-only?" in md

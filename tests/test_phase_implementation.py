"""Tests for dependency-aware phase implementation helpers."""

from __future__ import annotations

import json

import pytest

from asw.phase_implementation import (
    lint_development_lead_review_json,
    next_phase_implementation_turn,
    phase_implementation_turns,
    ready_phase_tasks,
)

_TASK_MAPPING = {
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare environment",
            "owner": "DevOps Engineer",
            "objective": "Provision the local toolchain.",
            "depends_on": [],
            "deliverables": ["Setup proposal"],
            "acceptance_criteria": ["Tooling is available locally"],
        },
        {
            "id": "implement_cli",
            "title": "Implement CLI",
            "owner": "Python Backend Developer",
            "objective": "Add the CLI command flow.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["CLI implementation"],
            "acceptance_criteria": ["CLI command works locally"],
        },
        {
            "id": "write_docs",
            "title": "Write docs",
            "owner": "Documentation Lead",
            "objective": "Document the local setup.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Updated quickstart"],
            "acceptance_criteria": ["Quickstart matches the local flow"],
        },
        {
            "id": "refine_cli",
            "title": "Refine CLI",
            "owner": "Python Backend Developer",
            "objective": "Close the CLI gaps discovered during documentation.",
            "depends_on": ["write_docs"],
            "deliverables": ["CLI refinements"],
            "acceptance_criteria": ["Documented flow matches the CLI"],
        },
    ]
}

_ROSTER_JSON = json.dumps(
    {
        "hired_agents": [
            {
                "title": "DevOps Engineer",
                "filename": "devops_engineer.md",
            },
            {
                "title": "Python Backend Developer",
                "filename": "python_backend_developer.md",
            },
            {
                "title": "Documentation Lead",
                "filename": "documentation_lead.md",
            },
        ]
    }
)

_VALID_DEVELOPMENT_LEAD_REVIEW = """\
# Development Lead Review: Local Validation

```json
{
    "decision": "approve",
    "summary": "The turn stayed in scope and the validations remain adequate.",
    "scope_findings": [],
    "standards_findings": [],
    "validation_findings": [],
    "required_follow_up": []
}
```
"""


def test_ready_phase_tasks_only_returns_dependency_satisfied_tasks() -> None:
    """Ready-task selection should exclude incomplete dependency chains."""
    initial_ready = ready_phase_tasks(_TASK_MAPPING)
    second_ready = ready_phase_tasks(_TASK_MAPPING, completed_task_ids={"prepare_environment"})

    assert [task["id"] for task in initial_ready] == ["prepare_environment"]
    assert [task["id"] for task in second_ready] == ["implement_cli", "write_docs"]


def test_next_phase_implementation_turn_batches_only_current_owner_tasks() -> None:
    """A turn should include all currently ready tasks for the first ready owner only."""
    turn = next_phase_implementation_turn(
        _TASK_MAPPING,
        _ROSTER_JSON,
        completed_task_ids={"prepare_environment"},
        turn_index=2,
    )

    assert turn is not None
    assert turn.turn_index == 2
    assert turn.owner_title == "Python Backend Developer"
    assert turn.task_ids == ["implement_cli"]


def test_phase_implementation_turns_reschedule_owner_after_dependency_unlock() -> None:
    """Interleaved dependencies should allow the same owner to appear in a later turn."""
    turns = phase_implementation_turns(_TASK_MAPPING, _ROSTER_JSON)

    assert [(turn.turn_index, turn.owner_title, turn.task_ids) for turn in turns] == [
        (1, "DevOps Engineer", ["prepare_environment"]),
        (2, "Python Backend Developer", ["implement_cli"]),
        (3, "Documentation Lead", ["write_docs"]),
        (4, "Python Backend Developer", ["refine_cli"]),
    ]


def test_next_phase_implementation_turn_rejects_missing_owner_in_roster() -> None:
    """Scheduling should fail fast when the approved owner is missing from the roster."""
    roster_without_docs = json.dumps(
        {
            "hired_agents": [
                {"title": "DevOps Engineer", "filename": "devops_engineer.md"},
                {"title": "Python Backend Developer", "filename": "python_backend_developer.md"},
            ]
        }
    )

    with pytest.raises(RuntimeError, match="Documentation Lead"):
        next_phase_implementation_turn(
            _TASK_MAPPING,
            roster_without_docs,
            completed_task_ids={"prepare_environment", "implement_cli"},
            turn_index=3,
        )


def test_lint_development_lead_review_json_accepts_valid_review() -> None:
    """A valid Development Lead review payload should parse cleanly."""
    errors, review = lint_development_lead_review_json(_VALID_DEVELOPMENT_LEAD_REVIEW)

    assert errors == []
    assert review is not None
    assert review["decision"] == "approve"
    assert review["required_follow_up"] == []


def test_lint_development_lead_review_json_rejects_missing_json_block() -> None:
    """Review output must contain a fenced JSON block."""
    errors, review = lint_development_lead_review_json("# Development Lead Review\n\nNo JSON here.")

    assert review is None
    assert any("fenced ```json``` review block" in error for error in errors)


def test_lint_development_lead_review_json_rejects_invalid_field_shapes() -> None:
    """Review array fields must be present as non-empty-string lists."""
    invalid_review = _VALID_DEVELOPMENT_LEAD_REVIEW.replace('"scope_findings": [],', '"scope_findings": [1],')

    errors, review = lint_development_lead_review_json(invalid_review)

    assert review is None
    assert any("scope_findings" in error for error in errors)


def test_lint_development_lead_review_json_normalizes_findings_to_revise() -> None:
    """Approve decisions with findings should be normalized to revise."""
    flagged_review = _VALID_DEVELOPMENT_LEAD_REVIEW.replace(
        '"scope_findings": [],',
        '"scope_findings": ["Touched files outside the scheduled task boundary."],',
    )

    errors, review = lint_development_lead_review_json(flagged_review)

    assert errors == []
    assert review is not None
    assert review["decision"] == "revise"

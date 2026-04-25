"""Tests for phase-task mapping helpers."""

# pylint: disable=duplicate-code

from __future__ import annotations

import json
from pathlib import Path

from asw.phase_preparation import build_phase_artifact_paths
from asw.phase_tasks import (
    lint_phase_task_mapping_json,
    load_phase_task_mapping,
    ordered_phase_tasks,
    render_phase_task_mapping_markdown,
    tasks_owned_by,
    write_phase_task_mapping,
)

_VALID_TASK_MAPPING = {
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare environment",
            "owner": "DevOps Engineer",
            "objective": "Provision the local toolchain.",
            "deliverables": ["Setup proposal"],
            "acceptance_criteria": ["Tooling is available locally"],
        },
        {
            "id": "implement_feature",
            "title": "Implement feature",
            "owner": "Python Backend Developer",
            "objective": "Add the local validation flow.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Updated CLI behavior"],
            "acceptance_criteria": ["Local validation succeeds"],
        },
    ]
}


def test_lint_phase_task_mapping_json_returns_canonical_mapping() -> None:
    """Valid task mappings should parse into canonical JSON data."""
    errors, task_mapping = lint_phase_task_mapping_json(
        json.dumps(_VALID_TASK_MAPPING),
        allowed_roles={"DevOps Engineer", "Python Backend Developer"},
    )

    assert not errors
    assert task_mapping is not None
    assert task_mapping["tasks"][0]["depends_on"] == []


def test_lint_phase_task_mapping_json_rejects_dependency_cycles() -> None:
    """Dependency cycles should fail before task mappings are persisted."""
    cyclic_mapping = {
        "tasks": [
            {
                "id": "first_task",
                "title": "First task",
                "owner": "Development Lead",
                "objective": "Start the cycle.",
                "depends_on": ["second_task"],
                "deliverables": ["Cycle entry"],
                "acceptance_criteria": ["Cycle is detected"],
            },
            {
                "id": "second_task",
                "title": "Second task",
                "owner": "Development Lead",
                "objective": "Close the cycle.",
                "depends_on": ["first_task"],
                "deliverables": ["Cycle exit"],
                "acceptance_criteria": ["Cycle is detected"],
            },
        ]
    }

    errors, task_mapping = lint_phase_task_mapping_json(json.dumps(cyclic_mapping), allowed_roles={"Development Lead"})

    assert task_mapping is None
    assert any("dependency cycle" in error for error in errors)


def test_ordered_phase_tasks_returns_stable_topological_order() -> None:
    """Tasks should be ordered by dependencies while preserving sibling order."""
    ordered_tasks = ordered_phase_tasks(_VALID_TASK_MAPPING)

    assert [task["id"] for task in ordered_tasks] == ["prepare_environment", "implement_feature"]


def test_tasks_owned_by_filters_ordered_tasks() -> None:
    """Owner filtering should preserve dependency-respecting task order."""
    owned_tasks = tasks_owned_by(_VALID_TASK_MAPPING, "Python Backend Developer")

    assert [task["id"] for task in owned_tasks] == ["implement_feature"]


def test_render_phase_task_mapping_markdown_lists_ordered_tasks() -> None:
    """Rendered Markdown should summarize ordered tasks and dependencies."""
    rendered = render_phase_task_mapping_markdown(_VALID_TASK_MAPPING, phase_label="phase_1 - Local Validation")

    assert "# Phase Task Mapping: phase_1 - Local Validation" in rendered
    assert "### 1. Prepare environment (`prepare_environment`)" in rendered
    assert "- **Depends On:** prepare_environment" in rendered


def test_write_and_load_phase_task_mapping_round_trip(tmp_path: Path) -> None:
    """Writing phase-task artifacts should persist canonical JSON and Markdown."""
    company = tmp_path / ".company"
    paths = build_phase_artifact_paths(company, 0)

    write_phase_task_mapping(_VALID_TASK_MAPPING, paths, phase_label="phase_1 - Local Validation")
    task_mapping = load_phase_task_mapping(paths)

    assert task_mapping == {
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
                "id": "implement_feature",
                "title": "Implement feature",
                "owner": "Python Backend Developer",
                "objective": "Add the local validation flow.",
                "depends_on": ["prepare_environment"],
                "deliverables": ["Updated CLI behavior"],
                "acceptance_criteria": ["Local validation succeeds"],
            },
        ]
    }
    assert paths.task_mapping_md_path.is_file()

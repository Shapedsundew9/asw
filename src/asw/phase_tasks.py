"""Helpers for canonical per-phase task-mapping artifacts."""

# pylint: disable=duplicate-code

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from asw.linters.json_lint import validate_phase_task_mapping

if TYPE_CHECKING:
    from asw.phase_preparation import PhaseArtifactPaths

logger = logging.getLogger("asw.phase_tasks")

_TASK_FIELDS = (
    "id",
    "title",
    "owner",
    "objective",
    "depends_on",
    "deliverables",
    "acceptance_criteria",
)


def lint_phase_task_mapping_json(
    content: str,
    *,
    allowed_roles: set[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return validation errors and canonical JSON for phase-task mapping content."""
    errors = validate_phase_task_mapping(content, allowed_roles=allowed_roles)
    if errors:
        return errors, None

    data = json.loads(content)
    if not isinstance(data, dict):
        return [f"Expected a JSON object at top level, got {type(data).__name__}."], None

    task_mapping = _canonicalize_task_mapping(data)
    try:
        ordered_phase_tasks(task_mapping)
    except ValueError as exc:
        return [str(exc)], None
    return [], task_mapping


def render_phase_task_mapping_markdown(task_mapping: dict[str, Any], *, phase_label: str) -> str:
    """Render a human-readable Markdown companion for a phase-task mapping."""
    ordered_tasks = ordered_phase_tasks(task_mapping)
    lines = [
        f"# Phase Task Mapping: {phase_label}",
        "",
        "> **Source of Truth:** The canonical task mapping is stored in the JSON artifact.",
        "",
        "## Ordered Tasks",
    ]

    for index, task in enumerate(ordered_tasks, start=1):
        depends_on = task.get("depends_on", [])
        dependency_text = ", ".join(depends_on) if depends_on else "None"
        lines.extend(
            [
                f"### {index}. {task['title']} (`{task['id']}`)",
                f"- **Owner:** {task['owner']}",
                f"- **Objective:** {task['objective']}",
                f"- **Depends On:** {dependency_text}",
                "- **Deliverables:**",
            ]
        )
        lines.extend(f"  - {item}" for item in task["deliverables"])
        lines.append("- **Acceptance Criteria:**")
        lines.extend(f"  - {item}" for item in task["acceptance_criteria"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_phase_task_mapping(task_mapping: dict[str, Any], paths: PhaseArtifactPaths, *, phase_label: str) -> None:
    """Write canonical JSON and Markdown artifacts for a phase-task mapping."""
    canonical_mapping = _canonicalize_task_mapping(task_mapping)
    json_content = json.dumps(canonical_mapping, indent=2) + "\n"
    errors, validated_mapping = lint_phase_task_mapping_json(json_content)
    if errors or validated_mapping is None:
        raise ValueError(f"Invalid phase task mapping: {'; '.join(errors)}")

    paths.task_mapping_json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.task_mapping_json_path.write_text(json.dumps(validated_mapping, indent=2) + "\n", encoding="utf-8")
    paths.task_mapping_md_path.write_text(
        render_phase_task_mapping_markdown(validated_mapping, phase_label=phase_label),
        encoding="utf-8",
    )


def load_phase_task_mapping(paths: PhaseArtifactPaths) -> dict[str, Any] | None:
    """Load and validate the canonical phase-task mapping when it exists."""
    json_path = paths.task_mapping_json_path
    if not json_path.is_file():
        return None

    try:
        content = json_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read phase task mapping: %s", json_path)
        return None

    errors, task_mapping = lint_phase_task_mapping_json(content)
    if errors:
        logger.warning("Phase task mapping is invalid at %s: %s", json_path, "; ".join(errors))
        return None
    return task_mapping


def ordered_phase_tasks(task_mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Return phase tasks in stable topological order."""
    tasks = _require_task_list(task_mapping)
    task_ids = [task["id"] for task in tasks]
    task_by_id = dict(zip(task_ids, tasks, strict=True))
    original_index = {task_id: index for index, task_id in enumerate(task_ids)}

    dependents: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    indegree: dict[str, int] = {task_id: 0 for task_id in task_ids}

    for task in tasks:
        task_id = task["id"]
        for dependency in task["depends_on"]:
            if dependency not in task_by_id:
                raise ValueError(f"Task '{task_id}' depends on unknown task '{dependency}'.")
            dependents[dependency].append(task_id)
            indegree[task_id] += 1

    ready = sorted((task_id for task_id, degree in indegree.items() if degree == 0), key=original_index.__getitem__)
    ordered_ids: list[str] = []

    while ready:
        task_id = ready.pop(0)
        ordered_ids.append(task_id)
        newly_ready: list[str] = []
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        ready.extend(sorted(newly_ready, key=original_index.__getitem__))

    if len(ordered_ids) != len(task_ids):
        cycle_nodes = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
        raise ValueError(f"Task mapping contains a dependency cycle involving: {', '.join(cycle_nodes)}.")

    return [task_by_id[task_id] for task_id in ordered_ids]


def tasks_owned_by(task_mapping: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    """Return ordered phase tasks owned by *owner*."""
    return [task for task in ordered_phase_tasks(task_mapping) if task["owner"] == owner]


def _canonicalize_task_mapping(task_mapping: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical copy of *task_mapping* with explicit dependency arrays."""
    tasks = _require_task_list(task_mapping)
    return {"tasks": [_canonicalize_task(task) for task in tasks]}


def _canonicalize_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical copy of a task object."""
    canonical_task = {field: task.get(field) for field in _TASK_FIELDS if field != "depends_on"}
    canonical_task["depends_on"] = list(task.get("depends_on", []))
    canonical_task["deliverables"] = list(task.get("deliverables", []))
    canonical_task["acceptance_criteria"] = list(task.get("acceptance_criteria", []))

    extras = {key: value for key, value in task.items() if key not in canonical_task}
    return {**canonical_task, **extras}


def _require_task_list(task_mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the task list from *task_mapping* or raise a descriptive error."""
    tasks = task_mapping.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Task mapping must contain a non-empty 'tasks' array.")

    normalized_tasks: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"tasks[{index}] must be an object.")
        normalized_tasks.append(_canonicalize_task(task))
    return normalized_tasks

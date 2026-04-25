"""Helpers for dependency-aware phase implementation turns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from asw.phase_preparation import extract_fenced_code_block
from asw.phase_tasks import ordered_phase_tasks

_DEVELOPMENT_LEAD_REVIEW_DECISIONS = {"approve", "revise"}
_DEVELOPMENT_LEAD_REVIEW_ARRAY_FIELDS = (
    "scope_findings",
    "standards_findings",
    "validation_findings",
    "required_follow_up",
)


@dataclass(frozen=True)
class PhaseImplementationTurn:
    """A single owner turn within a phase implementation loop."""

    turn_index: int
    owner_title: str
    roster_entry: dict[str, Any]
    tasks: list[dict[str, Any]]

    @property
    def task_ids(self) -> list[str]:
        """Return the task ids scheduled in this turn."""
        return [str(task["id"]) for task in self.tasks]


def phase_roster_entries_by_title(roster_json: str | dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return phase roster entries keyed by title."""
    roster_data = _roster_data(roster_json)
    hired_agents = roster_data.get("hired_agents", [])
    if not isinstance(hired_agents, list):
        raise ValueError("Roster must contain a 'hired_agents' array.")

    by_title: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(hired_agents):
        if not isinstance(entry, dict):
            raise ValueError(f"hired_agents[{index}] must be an object.")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"hired_agents[{index}].title must be a non-empty string.")
        by_title[title] = entry
    return by_title


def ready_phase_tasks(
    task_mapping: dict[str, Any],
    *,
    completed_task_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return ordered tasks whose dependencies are already completed."""
    completed = completed_task_ids or set()
    return [
        task
        for task in ordered_phase_tasks(task_mapping)
        if task["id"] not in completed and all(dependency in completed for dependency in task.get("depends_on", []))
    ]


def next_phase_implementation_turn(
    task_mapping: dict[str, Any],
    roster_json: str | dict[str, Any],
    *,
    completed_task_ids: set[str] | None = None,
    turn_index: int,
) -> PhaseImplementationTurn | None:
    """Return the next owner turn for the remaining ready tasks."""
    ready_tasks = ready_phase_tasks(task_mapping, completed_task_ids=completed_task_ids)
    if not ready_tasks:
        return None

    owner_title = str(ready_tasks[0]["owner"])
    roster_by_title = phase_roster_entries_by_title(roster_json)
    roster_entry = roster_by_title.get(owner_title)
    if roster_entry is None:
        raise RuntimeError(f"Task owner '{owner_title}' is not present in roster.json.")

    owner_tasks = [task for task in ready_tasks if task["owner"] == owner_title]
    return PhaseImplementationTurn(
        turn_index=turn_index,
        owner_title=owner_title,
        roster_entry=roster_entry,
        tasks=owner_tasks,
    )


def phase_implementation_turns(
    task_mapping: dict[str, Any],
    roster_json: str | dict[str, Any],
) -> list[PhaseImplementationTurn]:
    """Return all implementation turns for a phase using ready-task batching."""
    turns: list[PhaseImplementationTurn] = []
    completed_task_ids: set[str] = set()
    turn_index = 1
    total_tasks = len(ordered_phase_tasks(task_mapping))

    while len(completed_task_ids) < total_tasks:
        turn = next_phase_implementation_turn(
            task_mapping,
            roster_json,
            completed_task_ids=completed_task_ids,
            turn_index=turn_index,
        )
        if turn is None:
            raise ValueError("Could not determine the next implementation turn from the remaining phase tasks.")

        turns.append(turn)
        completed_task_ids.update(turn.task_ids)
        turn_index += 1

    return turns


def lint_development_lead_review_json(content: str) -> tuple[list[str], dict[str, Any] | None]:
    """Validate and normalize a Development Lead review JSON artifact."""
    errors: list[str] = []
    json_block = extract_fenced_code_block(content, "json")
    if json_block is None:
        errors.append("No fenced ```json``` review block found in Development Lead review output.")
        return errors, None

    try:
        review = json.loads(json_block)
    except json.JSONDecodeError as exc:
        errors.append(f"Development Lead review JSON is invalid: {exc.msg}.")
        return errors, None

    if not isinstance(review, dict):
        errors.append("Development Lead review JSON must be an object.")
        return errors, None

    decision = review.get("decision")
    if not isinstance(decision, str) or decision not in _DEVELOPMENT_LEAD_REVIEW_DECISIONS:
        errors.append("Development Lead review decision must be 'approve' or 'revise'.")

    summary = review.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("Development Lead review summary must be a non-empty string.")

    normalized: dict[str, Any] = {
        "decision": decision,
        "summary": summary.strip() if isinstance(summary, str) else "",
    }
    for field in _DEVELOPMENT_LEAD_REVIEW_ARRAY_FIELDS:
        value = review.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"Development Lead review field '{field}' must be a list of non-empty strings.")
            continue
        normalized[field] = [item.strip() for item in value]

    if errors:
        return errors, None

    if normalized["decision"] == "approve" and _review_requires_revision(normalized):
        normalized["decision"] = "revise"

    return [], normalized


def build_implementation_plan_request(phase_label: str, turn: PhaseImplementationTurn) -> str:
    """Return the planning instructions for one implementation turn."""
    return (
        f"Produce the implementation plan for {phase_label} turn {turn.turn_index}. "
        "Return Markdown only using this exact structure:\n\n"
        f"# Implementation Plan: {phase_label} Turn {turn.turn_index}\n\n"
        "## Task Summary\n"
        "- Summarize the scheduled tasks for this turn only.\n\n"
        "## Planned Changes\n"
        "- List the concrete code, docs, config, or test changes you will make in this turn.\n\n"
        "## Validation Approach\n"
        "- State which validations you expect to run or update for this turn.\n\n"
        "## Risks\n"
        "- List scope, dependency, or validation risks that could affect this turn.\n\n"
        "Stay within the scheduled tasks for this turn. Do not introduce work that belongs to later turns."
    )


def build_implementation_execute_request(phase_label: str, turn: PhaseImplementationTurn) -> str:
    """Return the execution instructions for one implementation turn."""
    return (
        f"Execute only the scheduled tasks for {phase_label} turn {turn.turn_index}. "
        "Return Markdown only using this exact structure:\n\n"
        f"# Implementation Execution: {phase_label} Turn {turn.turn_index}\n\n"
        "## Completed Work\n"
        "- Describe the concrete work completed for the scheduled tasks.\n\n"
        "## Files Changed\n"
        "- List the files you intentionally changed while executing this turn. Use '- None.' if no files changed.\n\n"
        "## Validation Notes\n"
        "- Describe any validations you ran, updated, or intentionally deferred.\n\n"
        "## Follow-Up\n"
        "- Note any remaining issues or explicit follow-up required before the next turn. Use '- None.' when done.\n\n"
        "Stay inside the approved turn plan and do not expand scope beyond the scheduled tasks."
    )


def build_development_lead_review_request(phase_label: str, turn: PhaseImplementationTurn) -> str:
    """Return the strict review instructions for one implementation turn."""
    return (
        f"Review the delivered work for {phase_label} turn {turn.turn_index}. "
        "Compare the actual changed files, implementation artifacts, and validation report "
        "against the approved turn scope. "
        "Return JSON only using this exact schema:\n\n"
        "```json\n"
        "{\n"
        '  "decision": "approve",\n'
        '  "summary": "Short explanation of the review result.",\n'
        '  "scope_findings": [],\n'
        '  "standards_findings": [],\n'
        '  "validation_findings": [],\n'
        '  "required_follow_up": []\n'
        "}\n"
        "```\n\n"
        "Rules:\n"
        "- Use 'approve' only when the turn stayed in scope, the changed files are appropriate,"
        " and the validation contract remains adequate.\n"
        "- Use 'revise' when scope drift, standards issues, missing validations, or validation-contract gaps remain.\n"
        "- Keep findings concrete and actionable.\n"
        "- Put only the concrete next actions needed before rerunning the same turn into required_follow_up.\n"
        "- Do not introduce new product scope or future-turn work."
    )


def render_phase_implementation_turn_summary(turn: PhaseImplementationTurn) -> str:
    """Render a Markdown summary of the tasks scheduled in one turn."""
    lines = [
        f"# Turn Summary: {turn.owner_title} Turn {turn.turn_index}",
        "",
        f"- **Owner:** {turn.owner_title}",
        f"- **Task IDs:** {', '.join(turn.task_ids)}",
        "",
        "## Scheduled Tasks",
    ]

    for task in turn.tasks:
        depends_on = task.get("depends_on", [])
        dependencies = ", ".join(depends_on) if depends_on else "None"
        deliverables = task.get("deliverables", [])
        acceptance_criteria = task.get("acceptance_criteria", [])
        lines.extend(
            [
                f"### {task['id']}: {task['title']}",
                f"- **Objective:** {task['objective']}",
                f"- **Depends On:** {dependencies}",
                "- **Deliverables:**",
                *[f"  - {item}" for item in deliverables],
                "- **Acceptance Criteria:**",
                *[f"  - {item}" for item in acceptance_criteria],
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _review_requires_revision(review: dict[str, Any]) -> bool:
    """Return whether review findings or follow-up require another turn attempt."""
    finding_fields = ("scope_findings", "standards_findings", "validation_findings")
    return any(review[field] for field in finding_fields) or bool(review["required_follow_up"])


def _roster_data(roster_json: str | dict[str, Any]) -> dict[str, Any]:
    """Return parsed roster data."""
    if isinstance(roster_json, dict):
        return roster_json

    data = json.loads(roster_json)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at top level, got {type(data).__name__}.")
    return data

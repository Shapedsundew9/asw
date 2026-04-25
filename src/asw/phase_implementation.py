"""Helpers for dependency-aware phase implementation turns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from asw.phase_tasks import ordered_phase_tasks


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


def _roster_data(roster_json: str | dict[str, Any]) -> dict[str, Any]:
    """Return parsed roster data."""
    if isinstance(roster_json, dict):
        return roster_json

    data = json.loads(roster_json)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at top level, got {type(data).__name__}.")
    return data

"""Hiring artifact helpers for role-brief generation."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from asw.core_roles import MANDATORY_CORE_ROLES
from asw.founder_questions import _render_founder_question_section

logger = logging.getLogger("asw.hiring")

_ROSTER_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.md$")
_ROSTER_REQUIRED_KEYS = {
    "title",
    "filename",
    "responsibility",
    "mission",
    "scope",
    "key_deliverables",
    "collaborators",
    "assigned_standards",
}


def _safe_join(items: list[str] | str) -> str:
    """Safely join a list of strings, or return the string if not a list."""
    if isinstance(items, str):
        return items
    return ", ".join(items)


def _require_non_empty_string(entry: dict, key: str, prefix: str, errors: list[str]) -> None:
    """Require *key* to be a non-empty string."""
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        errors.append(f"{prefix}.{key}: must be a non-empty string.")


def _require_non_empty_string_list(entry: dict, key: str, prefix: str, errors: list[str]) -> None:
    """Require *key* to be a non-empty list of non-empty strings."""
    values = entry.get(key)
    if not isinstance(values, list) or len(values) == 0:
        errors.append(f"{prefix}.{key}: must be a non-empty array.")
        return
    for idx, value in enumerate(values):
        if not isinstance(value, str) or not value:
            errors.append(f"{prefix}.{key}[{idx}]: must be a non-empty string.")


def _lint_roster_entry(entry: dict, prefix: str, available: set[str] | None, errors: list[str]) -> None:
    """Validate a single roster entry and append any errors found."""
    _require_non_empty_string(entry, "title", prefix, errors)
    _require_non_empty_string(entry, "responsibility", prefix, errors)
    _require_non_empty_string(entry, "mission", prefix, errors)
    _require_non_empty_string(entry, "scope", prefix, errors)
    _require_non_empty_string_list(entry, "key_deliverables", prefix, errors)
    _require_non_empty_string_list(entry, "collaborators", prefix, errors)

    filename = entry.get("filename")
    if not isinstance(filename, str) or not _ROSTER_FILENAME_RE.match(filename):
        errors.append(f"{prefix}.filename: must match lowercase_underscore.md (got '{entry.get('filename', '')}')")

    standards = entry.get("assigned_standards")
    if not isinstance(standards, list):
        errors.append(f"{prefix}.assigned_standards: must be an array.")
        return

    if available is None:
        return
    for idx, standard in enumerate(standards):
        if not isinstance(standard, str) or not standard:
            errors.append(f"{prefix}.assigned_standards[{idx}]: must be a non-empty string.")
            continue
        if standard not in available:
            errors.append(f"{prefix}.assigned_standards: '{standard}' not found in standards directory.")


def _extract_json_block(content: str) -> str | None:
    """Extract the first fenced JSON code block from *content*."""
    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _resolve_available_standards(standards_dir: Path | None) -> set[str] | None:
    """Return available standards when the standards directory exists."""
    if standards_dir is None or not standards_dir.is_dir():
        return None
    return {item.name for item in standards_dir.iterdir() if item.is_file()}


def _record_roster_entry(
    entry: dict,
    seen_titles: set[str],
    seen_filenames_by_title: dict[str, str],
) -> None:
    """Record title and filename data for later mandatory-role checks."""
    title = entry.get("title")
    filename = entry.get("filename")
    if not isinstance(title, str) or not title:
        return

    seen_titles.add(title)
    if isinstance(filename, str) and filename:
        seen_filenames_by_title[title] = filename


def _validate_mandatory_core_roles(
    seen_titles: set[str],
    seen_filenames_by_title: dict[str, str],
    errors: list[str],
) -> None:
    """Append validation errors for any missing or renamed core roles."""
    for role in MANDATORY_CORE_ROLES:
        if role.title not in seen_titles:
            errors.append(f"hired_agents: missing mandatory role '{role.title}'.")
            continue
        if seen_filenames_by_title.get(role.title) != role.filename:
            errors.append(f"hired_agents: mandatory role '{role.title}' must use filename '{role.filename}'.")


def _lint_roster(content: str, *, standards_dir: Path | None = None) -> list[str]:
    """Validate Hiring Manager roster output."""
    errors: list[str] = []

    json_block = _extract_json_block(content)
    if json_block is None:
        errors.append("No fenced ```json``` code block found in Hiring Manager output.")
        return errors

    try:
        data = json.loads(json_block)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON parse error: {exc}")
        return errors

    if not isinstance(data, dict) or "hired_agents" not in data:
        errors.append("JSON must be an object with a 'hired_agents' key.")
        return errors

    agents = data["hired_agents"]
    if not isinstance(agents, list) or len(agents) == 0:
        errors.append("'hired_agents' must be a non-empty array.")
        return errors

    available = _resolve_available_standards(standards_dir)
    seen_titles: set[str] = set()
    seen_filenames_by_title: dict[str, str] = {}

    for idx, entry in enumerate(agents):
        prefix = f"hired_agents[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object.")
            continue

        missing = _ROSTER_REQUIRED_KEYS - set(entry.keys())
        if missing:
            errors.append(f"{prefix}: missing keys: {', '.join(sorted(missing))}")
            continue

        _record_roster_entry(entry, seen_titles, seen_filenames_by_title)
        _lint_roster_entry(entry, prefix, available, errors)

    _validate_mandatory_core_roles(seen_titles, seen_filenames_by_title, errors)

    logger.debug("Roster lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  Roster lint error: %s", err)
    return errors


def _render_roster_markdown(json_str: str) -> str:
    """Render a human-readable Markdown view from role-brief JSON."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return "# Proposed Roster\n\n> **Warning:** Roster JSON could not be parsed.\n"

    agents = data.get("hired_agents", [])
    lines = [
        "# Proposed Roster",
        "",
        "> **Source of Truth:** The approved team role briefs are stored in `roster.json`.",
        "",
        "| # | Title | Filename | Mission | Standards |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, agent in enumerate(agents, 1):
        stds = _safe_join(agent.get("assigned_standards", [])) or "None"
        lines.append(
            f"| {idx} | {agent.get('title', 'N/A')} | {agent.get('filename', 'N/A')} "
            f"| {agent.get('mission', 'N/A')} | {stds} |"
        )
    lines.extend(["", f"**Total: {len(agents)} role(s) elaborated**"])

    for idx, agent in enumerate(agents, 1):
        lines.extend(
            [
                "",
                f"## Role {idx}: {agent.get('title', 'N/A')}",
                f"- **Responsibility:** {agent.get('responsibility', 'N/A')}",
                f"- **Mission:** {agent.get('mission', 'N/A')}",
                f"- **Scope:** {agent.get('scope', 'N/A')}",
                f"- **Collaborators:** {_safe_join(agent.get('collaborators', [])) or 'None'}",
                "- **Key Deliverables:**",
            ]
        )
        for deliverable in agent.get("key_deliverables", []):
            lines.append(f"  - {deliverable}")

    founder_questions = data.get("founder_questions", [])
    if isinstance(founder_questions, list) and founder_questions:
        lines.append("")
        lines.extend(_render_founder_question_section(founder_questions, heading="## Founder Input"))
    return "\n".join(lines)


def _write_roster(roster_json_str: str, company: Path) -> None:
    """Write roster artifacts to .company/artifacts/."""
    roster_json_path = company / "artifacts" / "roster.json"
    roster_json_path.write_text(roster_json_str, encoding="utf-8")

    roster_md_path = company / "artifacts" / "roster.md"
    roster_md_path.write_text(_render_roster_markdown(roster_json_str), encoding="utf-8")

    print(f"\n✓ Roster JSON written: {roster_json_path}")
    print(f"✓ Roster summary written: {roster_md_path}")


def _expected_role_paths(company: Path, roster_json: str) -> list[Path]:
    """Return the generated role file paths expected from *roster_json*."""
    try:
        data = json.loads(roster_json)
    except json.JSONDecodeError:
        logger.warning("Could not parse roster JSON while building expected role paths")
        return []

    agents = data.get("hired_agents")
    if not isinstance(agents, list):
        logger.warning("Roster JSON missing hired_agents while building expected role paths")
        return []

    paths: list[Path] = []
    for entry in agents:
        filename = entry.get("filename") if isinstance(entry, dict) else None
        if isinstance(filename, str) and filename:
            paths.append(company / "roles" / filename)
    return paths

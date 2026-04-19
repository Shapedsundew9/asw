"""Execution-plan artifact helpers."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from asw.founder_questions import _render_founder_question_section
from asw.linters.json_lint import validate_execution_plan

logger = logging.getLogger("asw.execution_plan")


def _safe_join(items: list[str] | str) -> str:
    """Safely join a list of strings, or return the string if not a list."""
    if isinstance(items, str):
        return items
    return ", ".join(items)


def _extract_json_block(content: str) -> str | None:
    """Extract the first fenced JSON code block from *content*."""
    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _render_execution_plan_markdown(json_str: str) -> str:
    """Render a human-readable Markdown view of the execution plan JSON."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return "# Execution Plan\n\n> **Warning:** Execution-plan JSON could not be parsed.\n"

    lines = [
        "# Execution Plan",
        "",
        "> **Source of Truth:** The execution plan is stored in `execution_plan.json`.",
        "",
        "## Selected Team",
        "| # | Title | Filename | Responsibility | Why Now |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, entry in enumerate(data.get("selected_team", []), 1):
        lines.append(
            f"| {idx} | {entry.get('title', 'N/A')} | {entry.get('filename', 'N/A')} "
            f"| {entry.get('responsibility', 'N/A')} | {entry.get('rationale', 'N/A')} |"
        )

    lines.extend(["", "## Delivery Phases", ""])
    for phase in data.get("phases", []):
        lines.extend(
            [
                f"### {phase.get('id', 'N/A')} - {phase.get('name', 'N/A')}",
                f"- **Objective:** {phase.get('objective', 'N/A')}",
                f"- **Scope:** {phase.get('scope', 'N/A')}",
                f"- **Selected Team Roles:** {_safe_join(phase.get('selected_team_roles', [])) or 'None'}",
                "- **Deliverables:**",
            ]
        )
        for deliverable in phase.get("deliverables", []):
            lines.append(f"  - {deliverable}")
        lines.append("- **Exit Criteria:**")
        for criterion in phase.get("exit_criteria", []):
            lines.append(f"  - {criterion}")
        lines.append("")

    lines.extend(
        [
            "## Generic Role Catalog",
            "| Title | Summary | When Needed |",
            "| --- | --- | --- |",
        ]
    )
    for entry in data.get("generic_role_catalog", []):
        lines.append(
            f"| {entry.get('title', 'N/A')} | {entry.get('summary', 'N/A')} | {entry.get('when_needed', 'N/A')} |"
        )

    lines.extend(["", "## Deferred Roles Or Capabilities", ""])
    deferred = data.get("deferred_roles_or_capabilities", [])
    if deferred:
        for entry in deferred:
            lines.append(f"- **{entry.get('name', 'N/A')}:** {entry.get('rationale', 'N/A')}")
    else:
        lines.append("- None.")

    founder_questions = data.get("founder_questions", [])
    if isinstance(founder_questions, list) and founder_questions:
        lines.extend(["", *_render_founder_question_section(founder_questions, heading="## Founder Input")])

    return "\n".join(lines)


def _lint_execution_plan(content: str) -> tuple[list[str], str | None]:
    """Lint VP Engineering execution-plan output."""
    errors: list[str] = []

    json_block = _extract_json_block(content)
    if json_block is None:
        errors.append("No fenced ```json``` code block found in VP Engineering output.")
    else:
        errors.extend(validate_execution_plan(json_block))

    logger.debug("Execution-plan lint result: %d error(s)", len(errors))
    for err in errors:
        logger.debug("  Execution-plan lint error: %s", err)
    return errors, json_block


def _write_execution_plan(plan_json_str: str, company: Path) -> None:
    """Write execution-plan artifacts to .company/artifacts/."""
    plan_json_path = company / "artifacts" / "execution_plan.json"
    plan_json_path.write_text(plan_json_str, encoding="utf-8")

    plan_md_path = company / "artifacts" / "execution_plan.md"
    plan_md_path.write_text(_render_execution_plan_markdown(plan_json_str), encoding="utf-8")

    print(f"\n✓ Execution plan JSON written: {plan_json_path}")
    print(f"✓ Execution plan summary written: {plan_md_path}")

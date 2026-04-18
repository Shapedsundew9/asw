"""Markdown mechanical validators."""

from __future__ import annotations

import re

# Fenced mermaid block pattern.
_MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

# Valid Mermaid diagram keywords that should appear near the top of a block.
_MERMAID_KEYWORDS = (
    "graph",
    "flowchart",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "C4Context",
    "C4Container",
    "C4Component",
)

_CHECKED_RE = re.compile(r"^- \[x\] .+", re.MULTILINE)
_UNCHECKED_RE = re.compile(r"^- \[ \] .+", re.MULTILINE)


def validate_checklist(content: str) -> list[str]:
    """Verify completed checklist items exist and none are unchecked.

    Returns a list of error messages (empty == pass).
    """
    errors: list[str] = []

    if not _CHECKED_RE.search(content):
        errors.append("No completed checklist items (- [x]) found.")

    unchecked = _UNCHECKED_RE.findall(content)
    if unchecked:
        errors.append(f"Found {len(unchecked)} unchecked item(s): {unchecked[:3]}")

    return errors


def validate_mermaid(content: str) -> list[str]:
    """Verify at least one valid fenced Mermaid block exists.

    Returns a list of error messages (empty == pass).
    """
    errors: list[str] = []
    blocks = _MERMAID_RE.findall(content)

    if not blocks:
        errors.append("No fenced ```mermaid``` code block found.")
        return errors

    for i, block in enumerate(blocks, 1):
        stripped = block.strip()
        if not any(stripped.startswith(kw) or f"\n{kw}" in stripped for kw in _MERMAID_KEYWORDS):
            errors.append(
                f"Mermaid block {i} does not start with a recognised diagram keyword "
                f"(expected one of {', '.join(_MERMAID_KEYWORDS[:5])}, …)."
            )

    return errors


def validate_sections(content: str, required_sections: list[str]) -> list[str]:
    """Verify that all *required_sections* appear as Markdown headings.

    Returns a list of error messages (empty == pass).
    """
    errors: list[str] = []
    lower_content = content.lower()

    for section in required_sections:
        # Match ## heading at any level.
        if section.lower() not in lower_content:
            errors.append(f"Required section missing: '{section}'")

    return errors

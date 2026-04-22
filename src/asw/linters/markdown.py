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

_CHECKED_RE = re.compile(r"^\s*[\-*+]\s+\[[xX]\]\s+.+", re.MULTILINE)
_UNCHECKED_RE = re.compile(r"^\s*[\-*+]\s+\[ \]\s+.+", re.MULTILINE)


def extract_markdown_section_body(content: str, heading: str) -> str | None:
    """Return the body for *heading*, or ``None`` when it is absent."""
    lines = content.splitlines()
    heading_level: int | None = None
    collecting = False
    body: list[str] = []

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            if collecting and heading_level is not None and level <= heading_level:
                break
            if title.casefold() == heading.casefold():
                collecting = True
                heading_level = level
                body = []
                continue

        if collecting:
            body.append(line)

    if not collecting:
        return None
    return "\n".join(body).strip()


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

    for section in required_sections:
        pattern = rf"^#{{1,6}}\s+{re.escape(section)}\s*$"
        if not re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
            errors.append(f"Required section missing: '{section}'")

    return errors


def validate_markdown_list_section(content: str, heading: str) -> list[str]:
    """Verify *heading* exists and contains at least one Markdown list item."""
    errors: list[str] = []
    body = extract_markdown_section_body(content, heading)
    if body is None:
        errors.append(f"Required section missing: '{heading}'")
        return errors

    if not re.search(r"^\s*[\-*+]\s+\S", body, re.MULTILINE):
        errors.append(f"Section '{heading}' must contain at least one Markdown list item.")

    return errors

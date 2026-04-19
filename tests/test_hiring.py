"""Tests for hiring pipeline functions: roster lint, role lint, roster rendering."""

from __future__ import annotations

import json
from pathlib import Path

from asw.hiring import _lint_roster, _render_roster_markdown
from asw.orchestrator import _lint_role

# ── _lint_roster tests ──────────────────────────────────────────────────


def _wrap_json(obj: dict | list | str) -> str:
    """Wrap a Python object in a fenced JSON code block."""
    return f"```json\n{json.dumps(obj, indent=2)}\n```"


_VALID_ROSTER = {
    "hired_agents": [
        {
            "title": "Backend Developer",
            "filename": "backend_developer.md",
            "responsibility": "Implement API endpoints.",
            "mission": "Deliver the first backend milestone.",
            "scope": "Own the service layer and persistence path for Phase 1.",
            "key_deliverables": ["Implement API endpoints", "Write backend tests"],
            "collaborators": ["Founder", "Frontend Developer"],
            "assigned_standards": ["python_guidelines.md"],
        },
        {
            "title": "Frontend Developer",
            "filename": "frontend_developer.md",
            "responsibility": "Build UI components.",
            "mission": "Deliver the core user workflow UI.",
            "scope": "Own the initial user interface for the approved Phase 1 scope.",
            "key_deliverables": ["Build the first workflow UI", "Write UI acceptance checks"],
            "collaborators": ["Backend Developer", "Founder"],
            "assigned_standards": ["ui_guidelines.md"],
        },
    ]
}


def test_lint_roster_valid(tmp_path: Path) -> None:
    """A valid roster should produce no errors."""
    standards = tmp_path / "standards"
    standards.mkdir()
    (standards / "python_guidelines.md").write_text("# Python")
    (standards / "ui_guidelines.md").write_text("# UI")

    errors = _lint_roster(_wrap_json(_VALID_ROSTER), standards_dir=standards)
    assert not errors


def test_lint_roster_no_json_block() -> None:
    """Missing JSON block should be an error."""
    errors = _lint_roster("No JSON here.")
    assert len(errors) == 1
    assert "No fenced" in errors[0]


def test_lint_roster_invalid_json() -> None:
    """Malformed JSON should be an error."""
    errors = _lint_roster("```json\n{invalid\n```")
    assert len(errors) == 1
    assert "JSON parse error" in errors[0]


def test_lint_roster_missing_hired_agents() -> None:
    """JSON without hired_agents key should be an error."""
    errors = _lint_roster(_wrap_json({"roles": []}))
    assert any("hired_agents" in e for e in errors)


def test_lint_roster_empty_array() -> None:
    """Empty hired_agents array should be an error."""
    errors = _lint_roster(_wrap_json({"hired_agents": []}))
    assert any("non-empty" in e for e in errors)


def test_lint_roster_missing_keys() -> None:
    """Entry missing required keys should be an error."""
    roster = {"hired_agents": [{"title": "Dev"}]}
    errors = _lint_roster(_wrap_json(roster))
    assert any("missing keys" in e for e in errors)


def test_lint_roster_bad_filename() -> None:
    """Invalid filename format should be an error."""
    roster = {
        "hired_agents": [
            {
                "title": "Dev",
                "filename": "Bad-Name.md",
                "responsibility": "Code.",
                "mission": "Ship code.",
                "scope": "Own one slice of the product.",
                "key_deliverables": ["Deliver feature work"],
                "collaborators": ["Founder"],
                "assigned_standards": [],
            }
        ]
    }
    errors = _lint_roster(_wrap_json(roster))
    assert any("lowercase_underscore" in e for e in errors)


def test_lint_roster_unknown_standard(tmp_path: Path) -> None:
    """Standards referencing non-existent files should be an error."""
    standards = tmp_path / "standards"
    standards.mkdir()
    (standards / "python_guidelines.md").write_text("# Python")

    roster = {
        "hired_agents": [
            {
                "title": "Dev",
                "filename": "dev.md",
                "responsibility": "Code.",
                "mission": "Ship code.",
                "scope": "Own one slice of the product.",
                "key_deliverables": ["Deliver feature work"],
                "collaborators": ["Founder"],
                "assigned_standards": ["nonexistent.md"],
            }
        ]
    }
    errors = _lint_roster(_wrap_json(roster), standards_dir=standards)
    assert any("nonexistent.md" in e for e in errors)


def test_lint_roster_no_standards_dir() -> None:
    """When standards_dir is None, standards refs should not be checked."""
    roster = {
        "hired_agents": [
            {
                "title": "Dev",
                "filename": "dev.md",
                "responsibility": "Code.",
                "mission": "Ship code.",
                "scope": "Own one slice of the product.",
                "key_deliverables": ["Deliver feature work"],
                "collaborators": ["Founder"],
                "assigned_standards": ["anything.md"],
            }
        ]
    }
    errors = _lint_roster(_wrap_json(roster), standards_dir=None)
    assert not errors


# ── _lint_role tests ────────────────────────────────────────────────────

_VALID_ROLE = """\
# Role: Backend Developer

You are the **Backend Developer** of a software company.

## Context

You receive architecture specs and implement backend services.

## Output Format

Produce Python source files with full test coverage.

## Strict Rules

- Follow PEP 8.
- Use type annotations everywhere.
- Do NOT include any text outside the code blocks.
"""


def test_lint_role_valid() -> None:
    """A valid role file should produce no errors."""
    errors = _lint_role(_VALID_ROLE)
    assert not errors


def test_lint_role_too_short() -> None:
    """Role file shorter than 200 chars should be an error."""
    errors = _lint_role("# Role: X\nShort.")
    assert any("too short" in e for e in errors)


def test_lint_role_missing_heading() -> None:
    """Role file without # Role: heading should be an error."""
    content = "x" * 250 + "\n## Output Format\n\nStuff.\n\n## Strict Rules\n\nMore stuff.\n"
    errors = _lint_role(content)
    assert any("# Role:" in e for e in errors)


def test_lint_role_missing_output_format() -> None:
    """Role file without ## Output Format should be an error."""
    content = "# Role: Test\n\n" + "x" * 250 + "\n\n## Strict Rules\n\nRules here.\n"
    errors = _lint_role(content)
    assert any("Output Format" in e for e in errors)


def test_lint_role_missing_strict_rules() -> None:
    """Role file without ## Strict Rules should be an error."""
    content = "# Role: Test\n\n" + "x" * 250 + "\n\n## Output Format\n\nFormat here.\n"
    errors = _lint_role(content)
    assert any("Strict Rules" in e for e in errors)


# ── _render_roster_markdown tests ───────────────────────────────────────


def test_render_roster_markdown() -> None:
    """Rendered roster should contain a table with role entries."""
    md = _render_roster_markdown(json.dumps(_VALID_ROSTER))

    assert "# Proposed Roster" in md
    assert "Backend Developer" in md
    assert "Frontend Developer" in md
    assert "backend_developer.md" in md
    assert "Deliver the first backend milestone." in md
    assert "**Total: 2 role(s) elaborated**" in md


def test_render_roster_markdown_invalid_json() -> None:
    """Invalid JSON should produce a warning message."""
    md = _render_roster_markdown("{invalid")
    assert "Warning" in md

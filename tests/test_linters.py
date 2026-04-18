"""Tests for mechanical linters."""

from __future__ import annotations

from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections

# ── validate_checklist ───────────────────────────────────────────────────


def test_checklist_valid() -> None:
    content = "- [x] First item\n- [x] Second item\n"
    assert validate_checklist(content) == []


def test_checklist_missing() -> None:
    content = "No checklist here.\n"
    errors = validate_checklist(content)
    assert any("No completed checklist" in e for e in errors)


def test_checklist_unchecked() -> None:
    content = "- [x] Done\n- [ ] Not done\n"
    errors = validate_checklist(content)
    assert any("unchecked" in e for e in errors)


# ── validate_mermaid ─────────────────────────────────────────────────────


def test_mermaid_valid() -> None:
    content = "```mermaid\ngraph TD\n  A --> B\n```\n"
    assert validate_mermaid(content) == []


def test_mermaid_missing() -> None:
    content = "No diagram here.\n"
    errors = validate_mermaid(content)
    assert any("No fenced" in e for e in errors)


def test_mermaid_bad_keyword() -> None:
    content = "```mermaid\nnotADiagram\n  A --> B\n```\n"
    errors = validate_mermaid(content)
    assert any("recognised diagram keyword" in e for e in errors)


def test_mermaid_sequence_diagram() -> None:
    content = "```mermaid\nsequenceDiagram\n  Alice->>Bob: Hello\n```\n"
    assert validate_mermaid(content) == []


# ── validate_sections ────────────────────────────────────────────────────


def test_sections_all_present() -> None:
    content = "## Foo\n\ntext\n\n## Bar\n\ntext\n"
    assert validate_sections(content, ["Foo", "Bar"]) == []


def test_sections_missing() -> None:
    content = "## Foo\n\ntext\n"
    errors = validate_sections(content, ["Foo", "Bar"])
    assert any("Bar" in e for e in errors)


# ── validate_architecture ────────────────────────────────────────────────


def test_architecture_valid() -> None:
    import json

    data = {
        "project_name": "test",
        "tech_stack": {},
        "components": [],
        "data_models": [],
        "api_contracts": [],
        "deployment": {},
    }
    assert validate_architecture(json.dumps(data)) == []


def test_architecture_invalid_json() -> None:
    errors = validate_architecture("not json")
    assert any("Invalid JSON" in e for e in errors)


def test_architecture_missing_keys() -> None:
    errors = validate_architecture('{"project_name": "x"}')
    assert len(errors) >= 4  # at least tech_stack, components, data_models, api_contracts missing

"""Tests for mechanical linters."""

from __future__ import annotations

from asw.linters.json_lint import validate_architecture
from asw.linters.markdown import validate_checklist, validate_mermaid, validate_sections

# ── validate_checklist ───────────────────────────────────────────────────


def test_checklist_valid() -> None:
    """Test valid checklist passes with no errors."""
    content = "- [x] First item\n- [x] Second item\n"
    assert not validate_checklist(content)


def test_checklist_missing() -> None:
    """Test missing checklist returns an error."""
    content = "No checklist here.\n"
    errors = validate_checklist(content)
    assert any("No completed checklist" in e for e in errors)


def test_checklist_unchecked() -> None:
    """Test unchecked items return an error."""
    content = "- [x] Done\n- [ ] Not done\n"
    errors = validate_checklist(content)
    assert any("unchecked" in e for e in errors)


# ── validate_mermaid ─────────────────────────────────────────────────────


def test_mermaid_valid() -> None:
    """Test valid mermaid block passes with no errors."""
    content = "```mermaid\ngraph TD\n  A --> B\n```\n"
    assert not validate_mermaid(content)


def test_mermaid_missing() -> None:
    """Test missing mermaid block returns an error."""
    content = "No diagram here.\n"
    errors = validate_mermaid(content)
    assert any("No fenced" in e for e in errors)


def test_mermaid_bad_keyword() -> None:
    """Test unrecognised diagram keyword returns an error."""
    content = "```mermaid\nnotADiagram\n  A --> B\n```\n"
    errors = validate_mermaid(content)
    assert any("recognised diagram keyword" in e for e in errors)


def test_mermaid_sequence_diagram() -> None:
    """Test sequence diagram passes with no errors."""
    content = "```mermaid\nsequenceDiagram\n  Alice->>Bob: Hello\n```\n"
    assert not validate_mermaid(content)


# ── validate_sections ────────────────────────────────────────────────────


def test_sections_all_present() -> None:
    """Test all required sections present passes with no errors."""
    content = "## Foo\n\ntext\n\n## Bar\n\ntext\n"
    assert not validate_sections(content, ["Foo", "Bar"])


def test_sections_missing() -> None:
    """Test missing section returns an error."""
    content = "## Foo\n\ntext\n"
    errors = validate_sections(content, ["Foo", "Bar"])
    assert any("Bar" in e for e in errors)


# ── validate_architecture ────────────────────────────────────────────────


def test_architecture_valid() -> None:
    """Test valid architecture JSON passes with no errors."""
    import json

    data = {
        "project_name": "test",
        "tech_stack": {},
        "components": [],
        "data_models": [],
        "api_contracts": [],
        "deployment": {},
    }
    assert not validate_architecture(json.dumps(data))


def test_architecture_invalid_json() -> None:
    """Test invalid JSON returns an error."""
    errors = validate_architecture("not json")
    assert any("Invalid JSON" in e for e in errors)


def test_architecture_missing_keys() -> None:
    """Test JSON missing required keys returns errors."""
    errors = validate_architecture('{"project_name": "x"}')
    assert len(errors) >= 4  # at least tech_stack, components, data_models, api_contracts missing

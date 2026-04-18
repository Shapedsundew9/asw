"""Integration test for the orchestrator pipeline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from asw.company import hash_file, init_company, write_pipeline_state
from asw.orchestrator import _is_phase_done, _render_architecture_markdown, run_pipeline

# ── Unit tests ──────────────────────────────────────────────────────────


def test_render_architecture_markdown_string_lists() -> None:
    """Ensure we safely handle single strings instead of lists in architecture."""
    data = {
        "project_name": "Test",
        "tech_stack": {
            "frameworks": "React",
            "tools": "Webpack",
        },
        "components": [
            {"name": "Frontend", "interfaces": "HTTP"},
        ],
        "deployment": {
            "requirements": "Modern browser",
        },
    }
    json_str = json.dumps(data)
    md = _render_architecture_markdown(json_str, "graph TD\nA-->B")

    assert "- **Frameworks:** React" in md
    assert "- **Tools:** Webpack" in md
    assert "| Frontend | N/A | HTTP |" in md
    assert "- **Requirements:** Modern browser" in md
    assert "M, o, d, e, r, n" not in md


# ── Canned LLM responses ────────────────────────────────────────────────

_CANNED_PRD = """\
## Executive Summary

AgenticOrg CLI orchestrates LLM-based agents.

## Goals & Success Metrics

- Automate SDLC phases.

## Target Users

Solo founders.

## Functional Requirements

- CLI start command.

## Non-Functional Requirements

- Must run inside DevContainer.

## User Stories

- As a founder, I want to run a single command, so that agents produce a PRD.

## Acceptance Criteria Checklist

- [x] CLI accepts --vision flag
- [x] PRD is generated
- [x] Architecture JSON is generated
- [x] Founder review gate pauses pipeline

## System Overview Diagram

```mermaid
graph TD
    A[Founder] --> B[CLI]
    B --> C[CPO Agent]
    B --> D[CTO Agent]
```

## Risks & Mitigations

- LLM hallucination → mechanical linting.

## Open Questions

- None.
"""

_CANNED_ARCH = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]}
}
```

```mermaid
graph TD
    CLI --> Orchestrator
    Orchestrator --> CPO
    Orchestrator --> CTO
```
"""

_CANNED_ROSTER = """\
```json
{
    "hired_agents": [
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "assigned_standards": ["python_guidelines.md"]
        }
    ]
}
```
"""

_CANNED_ROLE = """\
# Role: Python Backend Developer

You are the **Python Backend Developer** of an elite software engineering company. You implement the CLI entry point and orchestrator logic for the agenticorg project.

## Context

You receive architecture specifications and implement Python backend services including the CLI module and pipeline orchestration.

## Output Format

Produce Python source files following PEP 8 with full type annotations and Google-style docstrings. Each file must include comprehensive unit tests.

## Strict Rules

- Follow PEP 8 and use type annotations on all function signatures.
- Use Google-style docstrings for all public modules, classes, and functions.
- Prefer Python standard library modules over third-party packages.
- Do NOT include any text outside of the code blocks. No preamble, no sign-off.
- Under NO circumstances omit type annotations or docstrings.
"""


def _make_mock_llm() -> MagicMock:
    """Create a mock LLM backend that returns canned responses for all phases."""
    mock = MagicMock()
    mock.invoke = MagicMock(side_effect=[_CANNED_PRD, _CANNED_ARCH, _CANNED_ROSTER, _CANNED_ROLE])
    return mock


def test_full_pipeline(tmp_path: Path) -> None:
    """End-to-end: mock LLM + auto-approve → artifacts written."""
    # Write a minimal vision file.
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")

    # Initialise a git repo so commit_state works.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0

    # Verify V0.1 artifacts were written.
    company = tmp_path / ".company"
    assert (company / "artifacts" / "prd.md").is_file()
    assert (company / "artifacts" / "architecture.json").is_file()
    assert (company / "artifacts" / "architecture.md").is_file()

    # Verify architecture JSON is valid.
    arch = json.loads((company / "artifacts" / "architecture.json").read_text())
    assert arch["project_name"] == "agenticorg"

    # Verify V0.2 hiring artifacts were written.
    assert (company / "artifacts" / "roster.json").is_file()
    assert (company / "artifacts" / "roster.md").is_file()

    roster = json.loads((company / "artifacts" / "roster.json").read_text())
    assert len(roster["hired_agents"]) == 1
    assert roster["hired_agents"][0]["title"] == "Python Backend Developer"

    # Verify generated role file was written.
    assert (company / "roles" / "python_backend_developer.md").is_file()
    role_content = (company / "roles" / "python_backend_developer.md").read_text()
    assert "# Role: Python Backend Developer" in role_content

    # Verify pipeline state was written.
    from asw.company import read_pipeline_state

    state = read_pipeline_state(tmp_path)
    assert state is not None
    for phase in ("prd", "architecture", "roster", "roles"):
        assert phase in state["completed_phases"]


# ── Resume / restart tests ──────────────────────────────────────────────


def _setup_git_repo(tmp_path: Path) -> None:
    """Initialise a minimal git repo."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True, capture_output=True)


def test_is_phase_done_returns_false_when_no_state() -> None:
    """_is_phase_done returns False with no state."""
    assert _is_phase_done(None, "prd", []) is False


def test_is_phase_done_returns_false_when_phase_missing() -> None:
    """_is_phase_done returns False when phase not in completed_phases."""
    state = {"completed_phases": {}}
    assert _is_phase_done(state, "prd", []) is False


def test_is_phase_done_returns_false_when_artifact_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when artifact file doesn't exist."""
    state = {"completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}}}
    missing = tmp_path / "nonexistent.md"
    assert _is_phase_done(state, "prd", [missing]) is False


def test_is_phase_done_returns_true(tmp_path: Path) -> None:
    """_is_phase_done returns True when phase completed and artifacts exist."""
    state = {"completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}}}
    artifact = tmp_path / "prd.md"
    artifact.write_text("content")
    assert _is_phase_done(state, "prd", [artifact]) is True


def test_resume_skips_completed_phases(tmp_path: Path) -> None:
    """Pipeline skips phases that are already completed with artifacts on disk."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    # Pre-populate .company/ with all artifacts and a state file.
    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(json.dumps({"project_name": "test"}), encoding="utf-8")
    (company / "artifacts" / "architecture.md").write_text("# Arch", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_CANNED_ROSTER.split("```json\n")[1].split("\n```")[0])
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {
            "prd": {"timestamp": "2026-01-01T00:00:00Z"},
            "architecture": {"timestamp": "2026-01-01T00:00:00Z"},
            "roster": {"timestamp": "2026-01-01T00:00:00Z"},
            "roles": {"timestamp": "2026-01-01T00:00:00Z"},
        },
    }
    write_pipeline_state(tmp_path, state)

    mock_llm = MagicMock()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # LLM should NOT have been called – all phases were skipped.
    mock_llm.invoke.assert_not_called()


def test_resume_reruns_missing_artifact(tmp_path: Path) -> None:
    """If state says 'prd' done but prd.md is missing, phase reruns."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    # State says prd done, but NO prd.md on disk.
    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}},
    }
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # LLM was called because prd.md was missing, triggering all downstream phases.
    assert mock_llm.invoke.call_count == 4  # All phases ran.
    assert (company / "artifacts" / "prd.md").is_file()


def test_restart_flag_wipes_company(tmp_path: Path) -> None:
    """--restart deletes .company/ and runs all phases fresh."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    # Pre-populate state as if all phases completed.
    init_company(tmp_path)
    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {
            "prd": {"timestamp": "2026-01-01T00:00:00Z"},
            "architecture": {"timestamp": "2026-01-01T00:00:00Z"},
            "roster": {"timestamp": "2026-01-01T00:00:00Z"},
            "roles": {"timestamp": "2026-01-01T00:00:00Z"},
        },
    }
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, restart=True)

    assert result == 0
    # All 4 LLM calls should have been made (no skipping).
    assert mock_llm.invoke.call_count == 4


def test_vision_changed_continue(tmp_path: Path) -> None:
    """When vision changes and user chooses 'continue', pipeline resumes."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    # Complete PRD with old vision hash.
    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = {
        "version": "0.2",
        "vision_sha256": "old_hash_that_wont_match",
        "completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}},
    }
    write_pipeline_state(tmp_path, state)

    # Now change vision content (hash will differ from stored).
    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _make_mock_llm()
    # Provide only arch/roster/role responses since prd is skipped.
    mock_llm.invoke = MagicMock(side_effect=[_CANNED_ARCH, _CANNED_ROSTER, _CANNED_ROLE])

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
        patch("asw.orchestrator._prompt_vision_changed", return_value="continue"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # PRD should have been skipped (user chose continue), remaining 3 phases ran.
    assert mock_llm.invoke.call_count == 3


def test_vision_changed_restart(tmp_path: Path) -> None:
    """When vision changes and user chooses 'restart', pipeline runs from scratch."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = {
        "version": "0.2",
        "vision_sha256": "old_hash_that_wont_match",
        "completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}},
    }
    write_pipeline_state(tmp_path, state)

    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=("a", None)),
        patch("asw.orchestrator._prompt_vision_changed", return_value="restart"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # All 4 phases should have run (state was cleared).
    assert mock_llm.invoke.call_count == 4

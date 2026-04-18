"""Integration test for the orchestrator pipeline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from asw.orchestrator import _render_architecture_markdown, run_pipeline

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


def _make_mock_llm() -> MagicMock:
    """Create a mock LLM backend that returns canned PRD then Architecture."""
    mock = MagicMock()
    mock.invoke = MagicMock(side_effect=[_CANNED_PRD, _CANNED_ARCH])
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

    # Verify artifacts were written.
    company = tmp_path / ".company"
    assert (company / "artifacts" / "prd.md").is_file()
    assert (company / "artifacts" / "architecture.json").is_file()
    assert (company / "artifacts" / "architecture.md").is_file()

    # Verify architecture JSON is valid.
    arch = json.loads((company / "artifacts" / "architecture.json").read_text())
    assert arch["project_name"] == "agenticorg"

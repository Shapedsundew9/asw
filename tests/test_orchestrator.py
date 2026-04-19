"""Integration test for the orchestrator pipeline."""

# pylint: disable=too-many-lines

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from asw.company import init_company, new_pipeline_state, read_pipeline_state, snapshot_paths, write_pipeline_state
from asw.gates import FounderReviewResult
from asw.git import GitError
from asw.llm.errors import LLMInvocationError, TransientLLMError
from asw.orchestrator import (
    PipelineRunOptions,
    _agent_loop,
    _is_phase_done,
    _render_architecture_markdown,
    run_pipeline,
)

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


def test_agent_loop_retries_only_transient_backend_failures() -> None:
    """Transient backend failures should be retried."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=[TransientLLMError("busy", reason="busy"), "valid output"])

    result = _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    assert result == "valid output"
    assert agent.run.call_count == 2


def test_agent_loop_fails_fast_on_non_retryable_backend_error() -> None:
    """Non-transient backend failures should not be retried."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=LLMInvocationError("bad request", reason="non-transient-cli-error"))

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    mock_exit.assert_called_once_with(1)
    assert agent.run.call_count == 1


def test_agent_loop_fails_fast_on_lint_errors() -> None:
    """Mechanically invalid output should not be sent back for another LLM attempt."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="invalid output")

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: ["Missing section"], "PRD")

    mock_exit.assert_called_once_with(1)
    assert agent.run.call_count == 1


def test_agent_loop_fails_fast_on_lint_errors_and_saves_failed_artifact(tmp_path: Path) -> None:
    """Lint failures should be written to .company/artifacts/failed/ for inspection."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="invalid output")
    company = init_company(tmp_path)
    agent.role_file = company / "roles" / "cpo.md"

    with (
        patch("asw.orchestrator.sys.exit", side_effect=SystemExit(1)) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _agent_loop(agent, {"vision": "demo"}, lambda _: ["Missing section"], "PRD")

    mock_exit.assert_called_once_with(1)
    failed_files = list((company / "artifacts" / "failed").glob("prd_attempt1_*.md"))
    assert len(failed_files) == 1
    content = failed_files[0].read_text(encoding="utf-8")
    assert "Missing section" in content
    assert "invalid output" in content


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

_CANNED_EXECUTION_PLAN = """\
```json
{
    "phases": [
        {
            "id": "phase_1",
            "name": "Local Validation",
            "objective": "Validate the core workflow locally before production hardening.",
            "scope": "Use local-only infrastructure and defer hosted operations.",
            "deliverables": ["Core flow works locally", "Acceptance checks exist"],
            "exit_criteria": ["Founder can run the product locally", "Core checks pass"],
            "selected_team_roles": ["Development Lead", "DevOps Engineer", "Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "rationale": "This role is immediately needed to turn the approved plan into executable delivery structure."
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "rationale": "This role is immediately needed to keep tooling and environment setup repeatable."
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "Documentation Standards Lead",
            "summary": "Own tutorials, training material, and documentation quality control.",
            "when_needed": "Needed once the product surface evolves faster than the docs stay current."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Hosted Operations Platform",
            "rationale": "Deferred until the product needs hosted infrastructure, monitoring, and persistent storage."
        }
    ]
}
```
"""

_CANNED_ROSTER = """\
```json
{
    "hired_agents": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "mission": "Turn the approved execution plan into clear team delivery structure.",
            "scope": "Own design harmonisation, task sequencing, and delta review for the approved phase.",
            "key_deliverables": [
                "Publish phase design artifacts",
                "Review implementation deltas"
            ],
            "collaborators": ["Founder", "DevOps Engineer"],
            "assigned_standards": []
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "mission": "Make the approved implementation phase executable inside the project environment.",
            "scope": "Own environment setup, tooling installation, and operational guardrails for the phase.",
            "key_deliverables": [
                "Prepare setup steps",
                "Document environment changes"
            ],
            "collaborators": ["Development Lead", "Founder"],
            "assigned_standards": []
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "mission": "Deliver the first validated backend workflow for Phase 1.",
            "scope": "Own the CLI entry point, orchestration flow, and first persistence path for local validation.",
            "key_deliverables": [
                "Implement the first CLI workflow",
                "Write backend and orchestration tests"
            ],
            "collaborators": ["Founder", "Documentation Standards Lead"],
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

_APPROVE_REVIEW = FounderReviewResult(action="approve")

_CANNED_PRD_WITH_QUESTIONS = """\
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

## System Overview Diagram

```mermaid
graph TD
    A[Founder] --> B[CLI]
    B --> C[CPO Agent]
```

## Risks & Mitigations

- LLM hallucination → mechanical linting.

## Open Questions

- Founder questions will be captured in the CLI review flow and persisted here after review.

```json
{
  "founder_questions": [
    {
      "question": "Which database should we use?",
      "choices": ["PostgreSQL", "SQLite"]
    }
  ]
}
```
"""

_CANNED_ARCH_WITH_QUESTIONS = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "choices": ["Yes", "No"]}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

_CANNED_EXECUTION_PLAN_WITH_QUESTIONS = """\
```json
{
    "phases": [
        {
            "id": "phase_1",
            "name": "Local Validation",
            "objective": "Validate the core workflow locally before production hardening.",
            "scope": "Use local-only infrastructure and defer hosted operations.",
            "deliverables": ["Core flow works locally"],
            "exit_criteria": ["Founder can run the product locally"],
            "selected_team_roles": ["Development Lead", "DevOps Engineer", "Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Development Lead",
            "filename": "development_lead.md",
            "responsibility": "Coordinate phase design, task ownership, and implementation review.",
            "rationale": "This role is immediately needed to turn the approved plan into executable delivery structure."
        },
        {
            "title": "DevOps Engineer",
            "filename": "devops_engineer.md",
            "responsibility": "Prepare the delivery environment and required tooling.",
            "rationale": "This role is immediately needed to keep tooling and environment setup repeatable."
        },
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "Documentation Standards Lead",
            "summary": "Own tutorials, training material, and documentation quality control.",
            "when_needed": "Needed once the product surface evolves faster than the docs stay current."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Hosted Operations Platform",
            "rationale": "Deferred until the product needs hosted infrastructure, monitoring, and persistent storage."
        }
    ],
    "founder_questions": [
        {
            "question": "Should Phase 1 remain local-only?",
            "choices": ["Yes", "No"]
        }
    ]
}
```
"""


def _extract_json_block(content: str) -> str:
    """Extract the JSON body from a fenced JSON block."""
    return content.split("```json\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]


def _expected_role_output_paths(company: Path) -> list[Path]:
    """Return the role files expected from the canned roster."""
    return [
        company / "roles" / "development_lead.md",
        company / "roles" / "devops_engineer.md",
        company / "roles" / "python_backend_developer.md",
    ]


def _make_mock_llm() -> MagicMock:
    """Create a mock LLM backend that returns canned responses for all phases."""
    mock = MagicMock()
    mock.invoke = MagicMock(
        side_effect=[_CANNED_PRD, _CANNED_ARCH, _CANNED_EXECUTION_PLAN, _CANNED_ROSTER, _CANNED_ROLE]
    )
    return mock


def test_full_pipeline(tmp_path: Path) -> None:
    """End-to-end: mock LLM + auto-approve → artifacts written."""
    # Write a minimal vision file.
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")

    # Initialise a git repo so commit_state works.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
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
    assert (company / "artifacts" / "execution_plan.json").is_file()
    assert (company / "artifacts" / "execution_plan.md").is_file()
    assert (company / "artifacts" / "roster.json").is_file()
    assert (company / "artifacts" / "roster.md").is_file()

    execution_plan = json.loads((company / "artifacts" / "execution_plan.json").read_text())
    assert {entry["title"] for entry in execution_plan["selected_team"]} == {
        "Development Lead",
        "DevOps Engineer",
        "Python Backend Developer",
    }

    roster = json.loads((company / "artifacts" / "roster.json").read_text())
    assert len(roster["hired_agents"]) == 3
    backend_entry = next(entry for entry in roster["hired_agents"] if entry["title"] == "Python Backend Developer")
    assert backend_entry["mission"] == "Deliver the first validated backend workflow for Phase 1."

    # Verify generated role file was written.
    assert (company / "roles" / "development_lead.md").is_file()
    assert (company / "roles" / "devops_engineer.md").is_file()
    assert (company / "roles" / "python_backend_developer.md").is_file()
    role_content = (company / "roles" / "python_backend_developer.md").read_text()
    assert "# Role: Python Backend Developer" in role_content

    # Verify pipeline state was written.
    state = read_pipeline_state(tmp_path)
    assert state is not None
    for phase in ("prd", "architecture", "execution_plan", "roster", "roles"):
        assert phase in state["phases"]


def test_prd_founder_answers_are_applied_locally_without_extra_llm_call(tmp_path: Path) -> None:
    """Answering PRD founder questions should not trigger an extra Gemini call."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[_CANNED_PRD_WITH_QUESTIONS, _CANNED_ARCH, _CANNED_EXECUTION_PLAN, _CANNED_ROSTER, _CANNED_ROLE]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 5

    prd_content = (tmp_path / ".company" / "artifacts" / "prd.md").read_text(encoding="utf-8")
    assert "- Answer: PostgreSQL" in prd_content
    assert '"answer": "PostgreSQL"' in prd_content


def test_founder_answers_across_phases_are_applied_locally(tmp_path: Path) -> None:
    """PRD, architecture, and execution-plan answers should all avoid extra LLM calls."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_ARCH_WITH_QUESTIONS,
            _CANNED_EXECUTION_PLAN_WITH_QUESTIONS,
            _CANNED_ROSTER,
            _CANNED_ROLE,
        ]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should we deploy locally first?", "answer": "Yes"}],
                ),
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should Phase 1 remain local-only?", "answer": "Yes"}],
                ),
                _APPROVE_REVIEW,
            ],
        ),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 5

    company = tmp_path / ".company" / "artifacts"
    assert '"answer": "Yes"' in (company / "architecture.json").read_text(encoding="utf-8")
    assert "Answer: Yes" in (company / "architecture.md").read_text(encoding="utf-8")
    assert '"answer": "Yes"' in (company / "execution_plan.json").read_text(encoding="utf-8")
    assert "Answer: Yes" in (company / "execution_plan.md").read_text(encoding="utf-8")


def test_request_more_questions_reruns_with_current_artifact_and_answers(tmp_path: Path) -> None:
    """Explicit follow-up question requests should rerun with artifact and answer context."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
        ]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Which database should we use?", "answer": "PostgreSQL"}],
                ),
                FounderReviewResult(
                    action="request_more_questions",
                    feedback="Ask one more round focused on deployment assumptions.",
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 6

    second_user_prompt = mock_llm.invoke.call_args_list[1].args[1]
    assert "### CURRENT_PRD" in second_user_prompt
    assert "### FOUNDER_ANSWERS" in second_user_prompt
    assert "PostgreSQL" in second_user_prompt


def test_architecture_request_more_questions_escalates_until_new_question(tmp_path: Path) -> None:
    """Architecture follow-up requests should not silently return with no new question."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    arch_without_new_question = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "answer": "Yes"}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

    arch_with_new_question = """\
```json
{
    "project_name": "agenticorg",
    "tech_stack": {"language": "Python", "version": "3.14", "frameworks": [], "tools": ["argparse"]},
    "components": [{"name": "CLI", "responsibility": "Entry point", "interfaces": ["start"]}],
    "data_models": [{"name": "Vision", "fields": [{"name": "content", "type": "str"}]}],
    "api_contracts": [{"endpoint": "/start", "method": "CLI", "description": "Start pipeline"}],
    "deployment": {"platform": "DevContainer", "strategy": "local", "requirements": ["Docker"]},
    "founder_questions": [
        {"question": "Should we deploy locally first?", "answer": "Yes"},
        {"question": "Do we need authentication in Phase 1?", "choices": ["Yes", "No"]}
    ]
}
```

```mermaid
graph TD
    CLI --> Orchestrator
```
"""

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_PRD,
            _CANNED_ARCH_WITH_QUESTIONS,
            arch_without_new_question,
            arch_with_new_question,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
        ]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch(
            "asw.orchestrator.founder_review",
            side_effect=[
                _APPROVE_REVIEW,
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Should we deploy locally first?", "answer": "Yes"}],
                ),
                FounderReviewResult(
                    action="request_more_questions",
                    feedback="Ask one more unresolved architecture question.",
                ),
                FounderReviewResult(
                    action="answer_questions",
                    answers=[{"question": "Do we need authentication in Phase 1?", "answer": "No"}],
                ),
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 7
    architecture_json = (tmp_path / ".company" / "artifacts" / "architecture.json").read_text(encoding="utf-8")
    assert '"question": "Do we need authentication in Phase 1?"' in architecture_json
    assert '"answer": "No"' in architecture_json


# ── Resume / restart tests ──────────────────────────────────────────────


def _setup_git_repo(tmp_path: Path) -> None:
    """Initialise a minimal git repo."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


def _build_state(
    workdir: Path,
    phase_specs: dict[str, tuple[list[Path], list[Path]]],
    *,
    completed_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    """Build a pipeline state document from input/output path snapshots."""
    state = new_pipeline_state()
    for phase, (inputs, outputs) in phase_specs.items():
        input_snapshot = snapshot_paths(workdir, inputs)
        output_snapshot = snapshot_paths(workdir, outputs)
        state["tracked_files"].update(input_snapshot)
        state["tracked_files"].update(output_snapshot)
        state["phases"][phase] = {
            "completed_at": completed_at,
            "inputs": input_snapshot,
            "outputs": output_snapshot,
        }
    return state


def test_is_phase_done_returns_false_when_no_state() -> None:
    """_is_phase_done returns False with no state."""
    assert _is_phase_done(None, "prd", [], workdir=Path(".")) is False


def test_is_phase_done_returns_false_when_phase_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when phase is absent from state."""
    state = new_pipeline_state()
    assert _is_phase_done(state, "prd", [], workdir=tmp_path) is False


def test_is_phase_done_returns_false_when_artifact_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when artifact file doesn't exist."""
    missing = tmp_path / "nonexistent.md"
    state = _build_state(tmp_path, {"prd": ([tmp_path / "vision.md"], [missing])})
    assert _is_phase_done(state, "prd", [missing], workdir=tmp_path) is False


def test_is_phase_done_returns_true(tmp_path: Path) -> None:
    """_is_phase_done returns True when phase completed and artifacts exist."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n")
    artifact = tmp_path / "prd.md"
    artifact.write_text("content")
    state = _build_state(tmp_path, {"prd": ([vision], [artifact])})
    assert _is_phase_done(state, "prd", [artifact], workdir=tmp_path) is True


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
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN), encoding="utf-8"
    )
    (company / "artifacts" / "execution_plan.md").write_text("# Execution Plan", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_extract_json_block(_CANNED_ROSTER), encoding="utf-8")
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", company / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    company / "roles" / "vpe.md",
                    company / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "roles" / "hiring_manager.md",
                    company / "standards" / "python_guidelines.md",
                    company / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    company / "roles" / "role_writer.md",
                    company / "templates" / "role_template.md",
                    company / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = MagicMock()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
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
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # LLM was called because prd.md was missing, triggering all downstream phases.
    assert mock_llm.invoke.call_count == 5  # All phases ran.
    assert (company / "artifacts" / "prd.md").is_file()


def test_prd_commit_failure_is_retried_without_rerunning_prd(tmp_path: Path) -> None:
    """A failed PRD commit should be retried on rerun without spending another PRD LLM call."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch(
            "asw.orchestrator.commit_state",
            side_effect=[GitError("commit failed"), "", "", "", ""],
        ),
    ):
        first_result = run_pipeline(vision_path=vision, workdir=tmp_path)
        assert first_result == 1

        state = read_pipeline_state(tmp_path)
        assert state is not None
        assert "prd" in state["phases"]
        assert "commit:prd-generation" not in state["phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 5

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:prd-generation" in state["phases"]


def test_hiring_commit_failure_is_retried_without_rerunning_roles(tmp_path: Path) -> None:
    """A failed final hiring commit should be retried on rerun without new LLM calls."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch(
            "asw.orchestrator.commit_state",
            side_effect=["", "", "", GitError("commit failed"), ""],
        ),
    ):
        first_result = run_pipeline(vision_path=vision, workdir=tmp_path)
        assert first_result == 1

        state = read_pipeline_state(tmp_path)
        assert state is not None
        assert "roles" in state["phases"]
        assert "commit:hiring" not in state["phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 5

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:hiring" in state["phases"]


def test_resume_reruns_missing_role_file(tmp_path: Path) -> None:
    """If a generated role file is missing, role generation should rerun."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    (company / "artifacts" / "architecture.json").write_text(json.dumps({"project_name": "test"}), encoding="utf-8")
    (company / "artifacts" / "architecture.md").write_text("# Arch", encoding="utf-8")
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN), encoding="utf-8"
    )
    (company / "artifacts" / "execution_plan.md").write_text("# Execution Plan", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_extract_json_block(_CANNED_ROSTER), encoding="utf-8")
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", company / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    company / "roles" / "vpe.md",
                    company / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "roles" / "hiring_manager.md",
                    company / "standards" / "python_guidelines.md",
                    company / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    company / "roles" / "role_writer.md",
                    company / "templates" / "role_template.md",
                    company / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(return_value=_CANNED_ROLE)

    with patch("asw.orchestrator.get_backend", return_value=mock_llm):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(no_commit=True))

    assert result == 0
    assert mock_llm.invoke.call_count == 1
    assert (company / "roles" / "python_backend_developer.md").is_file()


def test_restart_flag_wipes_company(tmp_path: Path) -> None:
    """--restart deletes .company/ and runs all phases fresh."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    # Pre-populate state as if all phases completed.
    company = init_company(tmp_path)
    state = _build_state(
        tmp_path,
        {
            "prd": ([vision, tmp_path / ".company" / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"]),
            "architecture": (
                [vision, company / "artifacts" / "prd.md", tmp_path / ".company" / "roles" / "cto.md"],
                [company / "artifacts" / "architecture.json", company / "artifacts" / "architecture.md"],
            ),
            "execution_plan": (
                [
                    vision,
                    company / "artifacts" / "prd.md",
                    company / "artifacts" / "architecture.json",
                    tmp_path / ".company" / "roles" / "vpe.md",
                    tmp_path / ".company" / "templates" / "execution_plan_template.md",
                ],
                [company / "artifacts" / "execution_plan.json", company / "artifacts" / "execution_plan.md"],
            ),
            "roster": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    tmp_path / ".company" / "roles" / "hiring_manager.md",
                    tmp_path / ".company" / "standards" / "python_guidelines.md",
                    tmp_path / ".company" / "standards" / "ui_guidelines.md",
                ],
                [company / "artifacts" / "roster.json", company / "artifacts" / "roster.md"],
            ),
            "roles": (
                [
                    company / "artifacts" / "architecture.json",
                    company / "artifacts" / "execution_plan.json",
                    company / "artifacts" / "roster.json",
                    tmp_path / ".company" / "roles" / "role_writer.md",
                    tmp_path / ".company" / "templates" / "role_template.md",
                    tmp_path / ".company" / "standards" / "python_guidelines.md",
                ],
                _expected_role_output_paths(company),
            ),
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(restart=True))

    assert result == 0
    # All 5 LLM calls should have been made (no skipping).
    assert mock_llm.invoke.call_count == 5


def test_tracked_input_change_continue(tmp_path: Path) -> None:
    """Changed tracked inputs can be continued with explicitly."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = _build_state(
        tmp_path, {"prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"])}
    )
    write_pipeline_state(tmp_path, state)

    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _make_mock_llm()
    mock_llm.invoke = MagicMock(side_effect=[_CANNED_ARCH, _CANNED_EXECUTION_PLAN, _CANNED_ROSTER, _CANNED_ROLE])

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("builtins.input", return_value="c"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 4


def test_tracked_input_change_restart(tmp_path: Path) -> None:
    """Changed tracked inputs can trigger a full restart from the prompt."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision v1\n\nOriginal vision.\n")
    _setup_git_repo(tmp_path)

    company = init_company(tmp_path)
    (company / "artifacts" / "prd.md").write_text(_CANNED_PRD, encoding="utf-8")
    state = _build_state(
        tmp_path, {"prd": ([vision, company / "roles" / "cpo.md"], [company / "artifacts" / "prd.md"])}
    )
    write_pipeline_state(tmp_path, state)

    vision.write_text("# Vision v2\n\nUpdated vision.\n")

    mock_llm = _make_mock_llm()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("builtins.input", return_value="s"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 5

"""Integration test for the orchestrator pipeline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from asw.company import hash_file, init_company, read_pipeline_state, write_pipeline_state
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
            "selected_team_roles": ["Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "DevOps Engineer",
            "summary": "Own deployment automation and runtime operations.",
            "when_needed": "Needed once the product moves beyond a local-only validation workflow."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Production DevOps",
            "rationale": "Deferred until the product needs hosted infrastructure and persistent storage."
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

1. Which database should we use?
   - Choices: ["PostgreSQL", "SQLite"]

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
            "selected_team_roles": ["Python Backend Developer"]
        }
    ],
    "selected_team": [
        {
            "title": "Python Backend Developer",
            "filename": "python_backend_developer.md",
            "responsibility": "Implement CLI entry point and orchestrator logic.",
            "rationale": "This role is immediately needed to build the first validated product slice."
        }
    ],
    "generic_role_catalog": [
        {
            "title": "DevOps Engineer",
            "summary": "Own deployment automation and runtime operations.",
            "when_needed": "Needed once the product moves beyond a local-only validation workflow."
        }
    ],
    "deferred_roles_or_capabilities": [
        {
            "name": "Production DevOps",
            "rationale": "Deferred until the product needs hosted infrastructure and persistent storage."
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
    assert execution_plan["selected_team"][0]["title"] == "Python Backend Developer"

    roster = json.loads((company / "artifacts" / "roster.json").read_text())
    assert len(roster["hired_agents"]) == 1
    assert roster["hired_agents"][0]["title"] == "Python Backend Developer"
    assert roster["hired_agents"][0]["mission"] == "Deliver the first validated backend workflow for Phase 1."

    # Verify generated role file was written.
    assert (company / "roles" / "python_backend_developer.md").is_file()
    role_content = (company / "roles" / "python_backend_developer.md").read_text()
    assert "# Role: Python Backend Developer" in role_content

    # Verify pipeline state was written.
    state = read_pipeline_state(tmp_path)
    assert state is not None
    for phase in ("prd", "architecture", "execution_plan", "roster", "roles"):
        assert phase in state["completed_phases"]


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


def test_is_phase_done_returns_false_when_no_state() -> None:
    """_is_phase_done returns False with no state."""
    assert _is_phase_done(None, "prd", []) is False


def test_is_phase_done_returns_false_when_phase_missing() -> None:
    """_is_phase_done returns False when phase not in completed_phases."""
    state: dict = {"completed_phases": {}}
    assert _is_phase_done(state, "prd", []) is False


def test_is_phase_done_returns_false_when_artifact_missing(tmp_path: Path) -> None:
    """_is_phase_done returns False when artifact file doesn't exist."""
    state: dict = {"completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}}}
    missing = tmp_path / "nonexistent.md"
    assert _is_phase_done(state, "prd", [missing]) is False


def test_is_phase_done_returns_true(tmp_path: Path) -> None:
    """_is_phase_done returns True when phase completed and artifacts exist."""
    state: dict = {"completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}}}
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
    (company / "artifacts" / "execution_plan.json").write_text(
        _extract_json_block(_CANNED_EXECUTION_PLAN), encoding="utf-8"
    )
    (company / "artifacts" / "execution_plan.md").write_text("# Execution Plan", encoding="utf-8")
    (company / "artifacts" / "roster.json").write_text(_extract_json_block(_CANNED_ROSTER), encoding="utf-8")
    (company / "artifacts" / "roster.md").write_text("# Roster", encoding="utf-8")
    (company / "roles" / "python_backend_developer.md").write_text(_CANNED_ROLE, encoding="utf-8")

    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {
            "prd": {"timestamp": "2026-01-01T00:00:00Z"},
            "architecture": {"timestamp": "2026-01-01T00:00:00Z"},
            "execution_plan": {"timestamp": "2026-01-01T00:00:00Z"},
            "roster": {"timestamp": "2026-01-01T00:00:00Z"},
            "roles": {"timestamp": "2026-01-01T00:00:00Z"},
        },
    }
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
    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {"prd": {"timestamp": "2026-01-01T00:00:00Z"}},
    }
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
        assert "prd" in state["completed_phases"]
        assert "commit:prd-generation" not in state["completed_phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 5

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:prd-generation" in state["completed_phases"]


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
        assert "roles" in state["completed_phases"]
        assert "commit:hiring" not in state["completed_phases"]

        second_result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert second_result == 0
    assert mock_llm.invoke.call_count == 5

    state = read_pipeline_state(tmp_path)
    assert state is not None
    assert "commit:hiring" in state["completed_phases"]


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
    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {
            "prd": {"timestamp": "2026-01-01T00:00:00Z"},
            "architecture": {"timestamp": "2026-01-01T00:00:00Z"},
            "execution_plan": {"timestamp": "2026-01-01T00:00:00Z"},
            "roster": {"timestamp": "2026-01-01T00:00:00Z"},
            "roles": {"timestamp": "2026-01-01T00:00:00Z"},
        },
    }
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
    init_company(tmp_path)
    state = {
        "version": "0.2",
        "vision_sha256": hash_file(vision),
        "completed_phases": {
            "prd": {"timestamp": "2026-01-01T00:00:00Z"},
            "architecture": {"timestamp": "2026-01-01T00:00:00Z"},
            "execution_plan": {"timestamp": "2026-01-01T00:00:00Z"},
            "roster": {"timestamp": "2026-01-01T00:00:00Z"},
            "roles": {"timestamp": "2026-01-01T00:00:00Z"},
        },
    }
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
    mock_llm.invoke = MagicMock(side_effect=[_CANNED_ARCH, _CANNED_EXECUTION_PLAN, _CANNED_ROSTER, _CANNED_ROLE])

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator._prompt_vision_changed", return_value="continue"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # PRD should have been skipped (user chose continue), remaining 4 phases ran.
    assert mock_llm.invoke.call_count == 4


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
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator._prompt_vision_changed", return_value="restart"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # All 5 phases should have run (state was cleared).
    assert mock_llm.invoke.call_count == 5

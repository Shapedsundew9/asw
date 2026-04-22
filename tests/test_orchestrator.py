"""Integration test for the orchestrator pipeline."""

# pylint: disable=too-many-lines

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from asw.company import init_company, new_pipeline_state, read_pipeline_state, snapshot_paths, write_pipeline_state
from asw.gates import ExecutionApprovalResult, FounderReviewResult
from asw.git import GitError
from asw.llm.errors import LLMInvocationError, TransientLLMError
from asw.orchestrator import (
    PipelineRunOptions,
    _agent_loop,
    _init_pipeline_state,
    _is_phase_done,
    _render_architecture_markdown,
    run_pipeline,
)
from asw.phase_preparation import PhaseArtifactPaths, build_phase_artifact_paths
from asw.validation_contract import validation_contract_paths

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


def test_init_pipeline_state_bootstraps_validation_contract(tmp_path: Path) -> None:
    """Pipeline initialization should create the validation contract artifacts once."""
    state, company = _init_pipeline_state(tmp_path)

    contract_json_path, contract_md_path = validation_contract_paths(company)

    assert state == read_pipeline_state(tmp_path)
    assert contract_json_path.is_file()
    assert contract_md_path.is_file()

    first_json = contract_json_path.read_text(encoding="utf-8")
    first_markdown = contract_md_path.read_text(encoding="utf-8")

    second_state, second_company = _init_pipeline_state(tmp_path)

    assert second_company == company
    assert second_state == state
    assert contract_json_path.read_text(encoding="utf-8") == first_json
    assert contract_md_path.read_text(encoding="utf-8") == first_markdown


def test_agent_loop_retries_only_transient_backend_failures() -> None:
    """Transient backend failures should be retried."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=[TransientLLMError("busy", reason="busy"), "valid output"])

    result = _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    assert result == "valid output"
    assert agent.run.call_count == 2


def test_agent_loop_uses_role_aware_status_without_first_attempt_label(capsys: pytest.CaptureFixture[str]) -> None:
    """The initial agent run should show concise status text without attempt-one noise."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(return_value="valid output")

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        result = _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    captured = capsys.readouterr()
    assert result == "valid output"
    assert "attempt 1" not in captured.out.lower()
    assert "may take up to 5 min" not in captured.out.lower()
    assert "via gemini cli" not in captured.out.lower()
    assert "CPO drafting the PRD" in captured.out


def test_agent_loop_shows_feedback_stage_status(capsys: pytest.CaptureFixture[str]) -> None:
    """Feedback runs should describe the review work rather than the raw agent label."""
    agent = MagicMock()
    agent.name = "DevOps Engineer Feedback"
    agent.run = MagicMock(return_value="valid output")

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        _agent_loop(
            agent,
            {"phase_design_draft": "demo"},
            lambda _: [],
            "phase_1 - Local Validation Feedback: DevOps Engineer",
        )

    captured = capsys.readouterr()
    assert "DevOps Engineer reviewing the design for phase_1 - Local Validation" in captured.out


def test_agent_loop_shows_retry_label_only_when_retrying(capsys: pytest.CaptureFixture[str]) -> None:
    """Retry output should appear only after a transient failure triggers another attempt."""
    agent = MagicMock()
    agent.name = "CPO"
    agent.run = MagicMock(side_effect=[TransientLLMError("busy", reason="busy"), "valid output"])

    with patch("asw.orchestrator._supports_live_status", return_value=False):
        _agent_loop(agent, {"vision": "demo"}, lambda _: [], "PRD")

    captured = capsys.readouterr()
    assert "attempt 1" not in captured.out.lower()
    assert "Retry 2/3: CPO drafting the PRD" in captured.out


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

_CANNED_PHASE_DESIGN_DRAFT = """\
# Phase Design: Local Validation

## Phase Summary
- Deliver the first validated local workflow without expanding into hosted operations.

## Task Mapping
```json
{
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare the local validation environment",
            "owner": "DevOps Engineer",
            "objective": "Make the validated local toolchain available inside the repository workspace.",
            "depends_on": [],
            "deliverables": ["Guarded setup proposal and script"],
            "acceptance_criteria": ["Required tooling is available without tracked repo mutations"]
        },
        {
            "id": "implement_workflow",
            "title": "Implement the local validation workflow",
            "owner": "Python Backend Developer",
            "objective": "Build the first validated local path for the approved scope.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Workflow code changes", "Backend tests"],
            "acceptance_criteria": ["Founder can run the local workflow end to end"]
        },
        {
            "id": "coordinate_readiness",
            "title": "Coordinate implementation readiness",
            "owner": "Development Lead",
            "objective": "Keep the phase sequence, ownership, and completion boundaries explicit.",
            "depends_on": ["prepare_environment", "implement_workflow"],
            "deliverables": ["Harmonized phase design"],
            "acceptance_criteria": ["Task ownership and sequencing are explicit for the full phase"]
        }
    ]
}
```

## Required Tooling
- Python virtual environment
- pytest

## Sequencing Notes
- DevOps Engineer prepares the environment before implementation starts.
- Python Backend Developer implements the approved local workflow after environment prep.
- Development Lead keeps sequencing and acceptance boundaries aligned across the phase.
"""

_CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD = """\
# Phase Feedback: Development Lead

## Assessment
- The draft keeps the phase limited to local validation and assigns clear ownership.

## Dependencies
- None.

## Tooling Needs
- None.

## Risks
- Keep environment preparation ahead of implementation so later tasks do not start on incomplete tooling.
"""

_CANNED_PHASE_FEEDBACK_DEVOPS = """\
# Phase Feedback: DevOps Engineer

## Assessment
- The draft gives DevOps a clear first task and keeps the environment scope narrow.

## Dependencies
- None.

## Tooling Needs
- Python virtual environment
- pytest

## Risks
- The setup script should avoid mutating tracked repository files in the self-hosted repo.
"""

_CANNED_PHASE_FEEDBACK_BACKEND = """\
# Phase Feedback: Python Backend Developer

## Assessment
- The draft gives the backend role a concrete implementation target and a clear dependency on environment prep.

## Dependencies
- Environment preparation must finish before implementation begins.

## Tooling Needs
- pytest

## Risks
- The backend task should stay focused on the approved local workflow rather than deferred platform work.
"""

_CANNED_PHASE_DESIGN_FINAL = """\
# Phase Design: Local Validation

## Phase Summary
- Deliver the first validated local workflow with explicit ownership, safe environment prep, and no hosted-scope expansion.

## Task Mapping
```json
{
    "tasks": [
        {
            "id": "prepare_environment",
            "title": "Prepare the local validation environment",
            "owner": "DevOps Engineer",
            "objective": "Make the validated local toolchain available inside the repository workspace.",
            "depends_on": [],
            "deliverables": ["Guarded setup proposal and script"],
            "acceptance_criteria": ["Required tooling is available without tracked repo mutations"]
        },
        {
            "id": "implement_workflow",
            "title": "Implement the local validation workflow",
            "owner": "Python Backend Developer",
            "objective": "Build the first validated local path for the approved scope.",
            "depends_on": ["prepare_environment"],
            "deliverables": ["Workflow code changes", "Backend tests"],
            "acceptance_criteria": ["Founder can run the local workflow end to end"]
        },
        {
            "id": "coordinate_readiness",
            "title": "Coordinate implementation readiness",
            "owner": "Development Lead",
            "objective": "Keep the phase sequence, ownership, and completion boundaries explicit.",
            "depends_on": ["prepare_environment", "implement_workflow"],
            "deliverables": ["Harmonized phase design"],
            "acceptance_criteria": ["Task ownership and sequencing are explicit for the full phase"]
        }
    ]
}
```

## Required Tooling
- Python virtual environment
- pytest

## Sequencing Notes
- Environment preparation is Founder-gated and must complete before implementation begins.
- Backend implementation stays within the approved local-validation boundary.
- Development Lead maintains the approved sequence and acceptance boundaries.
"""

_CANNED_DEVOPS_PROPOSAL = """\
# DevOps Setup Proposal: Local Validation

## Execution Summary
Prepare the local validation environment by ensuring the repository virtual environment exists and repository Python dependencies are installed without changing tracked source files.

## Safety Notes
- The script only creates or updates the local `.venv` environment.
- The script does not invoke git or overwrite tracked repository files.

## Repo Impact
- Will create or update the local virtual environment and install Python dependencies into it.
- Will not modify tracked files under `src/`, `docs/`, `tests/`, or existing `.devcontainer` bootstrap files.

## Setup Script
```bash
#!/usr/bin/env bash
set -euo pipefail
trap 'echo "DevOps phase setup failed at line $LINENO while running: $BASH_COMMAND" >&2' ERR

if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```
"""

_APPROVE_REVIEW = FounderReviewResult(action="approve")
_APPROVE_EXECUTION = ExecutionApprovalResult(action="approve")
_SUCCESSFUL_DEVOPS_EXECUTION = subprocess.CompletedProcess(
    args=["bash", ".devcontainer/phase_01_setup.sh"],
    returncode=0,
    stdout="setup ok\n",
    stderr="",
)


def _phase_preparation_responses() -> list[str]:
    """Return canned LLM responses for the phase-preparation slice."""
    return [
        _CANNED_PHASE_DESIGN_DRAFT,
        _CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD,
        _CANNED_PHASE_FEEDBACK_DEVOPS,
        _CANNED_PHASE_FEEDBACK_BACKEND,
        _CANNED_PHASE_DESIGN_FINAL,
        _CANNED_DEVOPS_PROPOSAL,
    ]


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
        side_effect=[
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
        ]
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION) as mock_approval,
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION) as mock_execute,
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    mock_approval.assert_not_called()
    bash_calls = [call for call in mock_execute.call_args_list if call.args and call.args[0][0] == "bash"]
    assert not bash_calls

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
    for phase in (
        "prd",
        "architecture",
        "execution_plan",
        "roster",
        "roles",
        "phase-loop:phase_1:design",
        "phase-loop:phase_1:devops-proposal",
        "phase-loop:phase_1:devops-execution",
    ):
        assert phase in state["phases"]
    execution_phase = state["phases"]["phase-loop:phase_1:devops-execution"]
    assert execution_phase["metadata"]["status"] == "deferred"
    assert execution_phase["metadata"]["reason"] == (
        "Phase setup execution is deferred until the implementation loops are available."
    )
    phase_paths = build_phase_artifact_paths(company, 0)
    assert phase_paths.draft_path.is_file()
    assert phase_paths.final_path.is_file()
    assert phase_paths.proposal_path.is_file()
    assert phase_paths.summary_path.is_file()
    assert phase_paths.script_path.is_file()


def test_prd_founder_answers_are_applied_locally_without_extra_llm_call(tmp_path: Path) -> None:
    """Answering PRD founder questions should not trigger an extra Gemini call."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_PRD_WITH_QUESTIONS,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
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
                _APPROVE_REVIEW,
                _APPROVE_REVIEW,
            ],
        ),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 11

    prd_content = (tmp_path / ".company" / "artifacts" / "prd.md").read_text(encoding="utf-8")
    assert "- Answer: PostgreSQL" in prd_content
    assert '"answer": "PostgreSQL"' in prd_content


def test_devops_execution_revision_requires_reapproved_proposal(tmp_path: Path) -> None:
    """A Founder revision request should regenerate the proposal before execution runs."""
    vision = tmp_path / "vision.md"
    vision.write_text("# Vision\n\nBuild a CLI tool.\n")
    _setup_git_repo(tmp_path)

    mock_llm = MagicMock()
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_PRD,
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
            _CANNED_DEVOPS_PROPOSAL,
        ]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch(
            "asw.orchestrator.founder_approve_devops_execution",
            side_effect=[
                ExecutionApprovalResult(action="revise", feedback="Tighten the safety summary."),
                _APPROVE_EXECUTION,
            ],
        ),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION) as mock_execute,
    ):
        result = run_pipeline(
            vision_path=vision,
            workdir=tmp_path,
            options=PipelineRunOptions(execute_phase_setups=True),
        )

    assert result == 0
    assert mock_llm.invoke.call_count == 12
    bash_calls = [call for call in mock_execute.call_args_list if call.args and call.args[0][0] == "bash"]
    assert len(bash_calls) == 1
    assert "Tighten the safety summary." in mock_llm.invoke.call_args_list[-1].args[1]


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
            *_phase_preparation_responses(),
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 11

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
            *_phase_preparation_responses(),
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 12

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
            *_phase_preparation_responses(),
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 13
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


def _write_phase_preparation_artifacts(company: Path) -> PhaseArtifactPaths:
    """Write canned phase-preparation artifacts for the first phase."""
    paths = build_phase_artifact_paths(company, 0)
    paths.draft_path.parent.mkdir(parents=True, exist_ok=True)
    paths.script_path.parent.mkdir(parents=True, exist_ok=True)
    paths.draft_path.write_text(_CANNED_PHASE_DESIGN_DRAFT, encoding="utf-8")
    paths.feedback_path("Development Lead").write_text(_CANNED_PHASE_FEEDBACK_DEVELOPMENT_LEAD, encoding="utf-8")
    paths.feedback_path("DevOps Engineer").write_text(_CANNED_PHASE_FEEDBACK_DEVOPS, encoding="utf-8")
    paths.feedback_path("Python Backend Developer").write_text(_CANNED_PHASE_FEEDBACK_BACKEND, encoding="utf-8")
    paths.final_path.write_text(_CANNED_PHASE_DESIGN_FINAL, encoding="utf-8")
    paths.proposal_path.write_text(_CANNED_DEVOPS_PROPOSAL, encoding="utf-8")
    paths.summary_path.write_text(
        "# DevOps Setup Summary\n\n- Generated script path: `.devcontainer/phase_01_setup.sh`\n",
        encoding="utf-8",
    )
    paths.script_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ntrap 'echo \"failed\" >&2' ERR\necho ready\n",
        encoding="utf-8",
    )
    return paths


def _phase_preparation_phase_specs(
    workdir: Path,
    company: Path,
    vision: Path,
) -> dict[str, tuple[list[Path], list[Path]]]:
    """Return state specs for the first phase-preparation slice."""
    paths = _write_phase_preparation_artifacts(company)
    design_inputs = [
        vision,
        company / "artifacts" / "prd.md",
        company / "artifacts" / "architecture.json",
        company / "artifacts" / "execution_plan.json",
        company / "artifacts" / "roster.json",
        company / "roles" / "development_lead.md",
        company / "roles" / "phase_feedback_reviewer.md",
        company / "roles" / "development_lead.md",
        company / "roles" / "devops_engineer.md",
        company / "roles" / "python_backend_developer.md",
    ]
    proposal_inputs = [
        paths.final_path,
        company / "roles" / "devops_engineer.md",
        workdir / ".devcontainer" / "post-create.sh",
        workdir / ".devcontainer" / "post-start.sh",
        workdir / ".devcontainer" / "devcontainer.json",
    ]
    return {
        "phase-loop:phase_1:design": (
            design_inputs,
            [
                paths.draft_path,
                paths.feedback_path("Development Lead"),
                paths.feedback_path("DevOps Engineer"),
                paths.feedback_path("Python Backend Developer"),
                paths.final_path,
            ],
        ),
        "phase-loop:phase_1:devops-proposal": (
            proposal_inputs,
            [paths.proposal_path, paths.summary_path, paths.script_path],
        ),
        "phase-loop:phase_1:devops-execution": ([paths.proposal_path, paths.summary_path, paths.script_path], []),
    }


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
    phase_prep_specs = _phase_preparation_phase_specs(tmp_path, company, vision)

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
            **phase_prep_specs,
        },
    )
    write_pipeline_state(tmp_path, state)

    mock_llm = MagicMock()

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    # LLM was called because prd.md was missing, triggering all downstream phases.
    assert mock_llm.invoke.call_count == 11  # All phases ran.
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
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
    assert mock_llm.invoke.call_count == 11

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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
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
    assert mock_llm.invoke.call_count == 11

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
    mock_llm.invoke = MagicMock(side_effect=[_CANNED_ROLE, *_phase_preparation_responses()])

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(no_commit=True))

    assert result == 0
    assert mock_llm.invoke.call_count == 7
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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path, options=PipelineRunOptions(restart=True))

    assert result == 0
    # All planning and phase-preparation LLM calls should have been made (no skipping).
    assert mock_llm.invoke.call_count == 11


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
    mock_llm.invoke = MagicMock(
        side_effect=[
            _CANNED_ARCH,
            _CANNED_EXECUTION_PLAN,
            _CANNED_ROSTER,
            _CANNED_ROLE,
            *_phase_preparation_responses(),
        ]
    )

    with (
        patch("asw.orchestrator.get_backend", return_value=mock_llm),
        patch("asw.orchestrator.founder_review", return_value=_APPROVE_REVIEW),
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch("builtins.input", return_value="c"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 10


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
        patch("asw.orchestrator.founder_approve_devops_execution", return_value=_APPROVE_EXECUTION),
        patch("asw.orchestrator.subprocess.run", return_value=_SUCCESSFUL_DEVOPS_EXECUTION),
        patch("builtins.input", return_value="s"),
    ):
        result = run_pipeline(vision_path=vision, workdir=tmp_path)

    assert result == 0
    assert mock_llm.invoke.call_count == 11
